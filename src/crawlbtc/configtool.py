"""`crawlbtc config` - locate, back up, and restore the configuration
files crawlbtc depends on.

Three moving parts hold configuration this tool relies on:
  1. the crawlbtc env file (RPC + Postgres credentials, tuning knobs)
  2. bitcoin.conf (the node's RPC server settings)
  3. PostgreSQL server + role settings (postgresql.conf, pg_hba.conf,
     and per-role GUCs like idle_in_transaction_session_timeout)

`config show` reports where each lives and whether it is present.
`config backup` snapshots them into a timestamped directory.
`config restore` copies the env file / bitcoin.conf back (dry-run unless
--force) and prints the SQL to re-apply role settings.
"""

import datetime
import json
import os
import shutil
import stat
import sys

import psycopg

from . import __version__
from .core.config import resolve_env_file

# Postgres settings worth snapshotting for tuning/audit.
_PG_SETTINGS = [
    "config_file", "hba_file", "ident_file", "data_directory",
    "max_connections", "superuser_reserved_connections",
    "shared_buffers", "work_mem", "maintenance_work_mem",
    "effective_cache_size", "synchronous_commit",
    "idle_in_transaction_session_timeout", "statement_timeout",
]

_SECRET_KEYS = ("PASSWORD", "SECRET", "TOKEN")
_ENV_KEYS = ("RPC_HOST", "RPC_PORT", "RPC_USER", "RPC_PASSWORD",
             "PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD",
             "LOG_LEVEL", "POWER", "NUM_WORKERS", "PROCESSES",
             "RPC_CONCURRENCY", "DB_MAX_CONN", "DB_WRITE_CONCURRENCY",
             "JOB_BATCH_SIZE", "DB_POOL_TIMEOUT", "START_DELAY",
             "PG_SYNCHRONOUS_COMMIT", "CRAWLBTC_ENV_FILE")


def _redact(key: str, value: str) -> str:
    if value and any(s in key.upper() for s in _SECRET_KEYS):
        return value[:2] + "***" if len(value) > 2 else "***"
    return value


def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def find_bitcoin_conf(explicit=None):
    """Return candidate bitcoin.conf paths (existing first)."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.getenv("BITCOIN_CONF"):
        candidates.append(os.getenv("BITCOIN_CONF"))
    if os.getenv("BITCOIN_DATADIR"):
        candidates.append(os.path.join(os.getenv("BITCOIN_DATADIR"), "bitcoin.conf"))
    candidates.append(os.path.expanduser("~/.bitcoin/bitcoin.conf"))
    candidates.append(os.path.expanduser("~/Library/Application Support/Bitcoin/bitcoin.conf"))
    out, seen = [], set()
    for c in candidates:
        c = os.path.abspath(os.path.expanduser(c))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _pg_snapshot(cfg):
    """Best-effort Postgres settings + role GUCs. Returns dict or None."""
    try:
        with psycopg.connect(cfg.db_conninfo, autocommit=True, connect_timeout=10) as conn:
            cur = conn.cursor()
            settings = {}
            for name in _PG_SETTINGS:
                try:
                    cur.execute(f"SHOW {name};")
                    settings[name] = cur.fetchone()[0]
                except Exception:
                    settings[name] = None
            cur.execute("SELECT rolname, rolconfig FROM pg_roles WHERE rolconfig IS NOT NULL ORDER BY rolname;")
            roles = {r[0]: list(r[1]) for r in cur.fetchall()}
            return {"settings": settings, "role_config": roles}
    except Exception as e:
        return {"error": str(e)}


def _role_config_sql(role_config: dict) -> str:
    lines = ["-- Re-apply per-role settings (run as a superuser / role owner)."]
    for role, entries in role_config.items():
        for entry in entries:
            if "=" in entry:
                k, v = entry.split("=", 1)
                lines.append(f"ALTER ROLE {role} SET {k} = '{v}';")
    return "\n".join(lines) + "\n"


# --- show ---

def run_config_show(cfg, args):
    print(f"crawlbtc {__version__} - configuration sources\n")

    # 1. env file
    env_path = resolve_env_file(getattr(args, "env_file", None))
    print("[1] crawlbtc env file")
    if env_path and os.path.isfile(env_path):
        print(f"    path : {env_path}  (present)")
    elif env_path:
        print(f"    path : {env_path}  (MISSING)")
    else:
        print("    path : none found (using process environment only)")
    print(f"    hint : override with --env-file or CRAWLBTC_ENV_FILE")
    print("    effective values (secrets redacted):")
    for k in _ENV_KEYS:
        v = os.getenv(k)
        if v is not None:
            print(f"      {k}={_redact(k, v)}")

    # 2. bitcoin.conf
    print("\n[2] bitcoin.conf (node RPC settings)")
    for p in find_bitcoin_conf(getattr(args, "bitcoin_conf", None)):
        print(f"    {'present' if os.path.isfile(p) else 'absent '}  {p}")
    print(f"    RPC endpoint in use: {cfg.rpc_url}")

    # 3. postgres
    print("\n[3] PostgreSQL server + role settings")
    snap = _pg_snapshot(cfg)
    if snap.get("error"):
        print(f"    (database unreachable: {snap['error']})")
    else:
        for name in ("config_file", "hba_file", "data_directory"):
            print(f"    {name}: {snap['settings'].get(name)}")
        print("    key settings:")
        for name in _PG_SETTINGS:
            if name not in ("config_file", "hba_file", "ident_file", "data_directory"):
                print(f"      {name} = {snap['settings'].get(name)}")
        if snap["role_config"]:
            print("    per-role overrides:")
            for role, entries in snap["role_config"].items():
                print(f"      {role}: {', '.join(entries)}")

    # 4. package SQL
    print("\n[4] crawlbtc packaged SQL (schema + migrations)")
    from importlib import resources
    sql_dir = resources.files("crawlbtc") / "sql"
    print(f"    {sql_dir}")
    print("\nBack these up with:  crawlbtc config backup")


# --- backup ---

def _copy_secret(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)  # 0600 - contains credentials


def run_config_backup(cfg, args):
    out = args.path or os.path.join(os.getcwd(), f"crawlbtc-config-backup-{_now_stamp()}")
    out = os.path.abspath(os.path.expanduser(out))
    os.makedirs(out, exist_ok=True)
    manifest = {"created": datetime.datetime.now().isoformat(),
                "crawlbtc_version": __version__, "items": {}}

    # env file
    env_path = resolve_env_file(getattr(args, "env_file", None))
    if env_path and os.path.isfile(env_path):
        dst = os.path.join(out, "env", os.path.basename(env_path))
        _copy_secret(env_path, dst)
        manifest["items"]["env_file"] = {"original": env_path,
                                         "stored": os.path.relpath(dst, out)}
        print(f"backed up env file: {env_path}")
    else:
        print("env file: none found, skipped")

    # bitcoin.conf (all that exist)
    manifest["items"]["bitcoin_conf"] = []
    for p in find_bitcoin_conf(getattr(args, "bitcoin_conf", None)):
        if os.path.isfile(p):
            dst = os.path.join(out, "bitcoin", p.strip("/").replace("/", "__"))
            _copy_secret(p, dst)  # may contain rpcpassword
            manifest["items"]["bitcoin_conf"].append(
                {"original": p, "stored": os.path.relpath(dst, out)})
            print(f"backed up bitcoin.conf: {p}")

    # postgres snapshot (read-only)
    snap = _pg_snapshot(cfg)
    pg_dir = os.path.join(out, "postgres")
    os.makedirs(pg_dir, exist_ok=True)
    with open(os.path.join(pg_dir, "settings.json"), "w") as f:
        json.dump(snap, f, indent=2)
    if not snap.get("error"):
        with open(os.path.join(pg_dir, "role_settings.sql"), "w") as f:
            f.write(_role_config_sql(snap.get("role_config", {})))
        # Try copying postgresql.conf / pg_hba.conf if readable (often root-owned)
        for key in ("config_file", "hba_file"):
            src = snap["settings"].get(key)
            if src and os.path.isfile(src) and os.access(src, os.R_OK):
                try:
                    shutil.copy2(src, os.path.join(pg_dir, os.path.basename(src)))
                    print(f"backed up {os.path.basename(src)}")
                except Exception as e:
                    print(f"could not copy {src}: {e}")
            elif src:
                print(f"note: {src} not readable by this user - snapshot only")
        print("captured postgres settings + role GUCs")
    else:
        print(f"postgres snapshot skipped: {snap['error']}")

    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nbackup written to: {out}")
    print("(contains credentials - keep it private)")


# --- restore ---

def run_config_restore(cfg, args):
    src = args.path
    if not src:
        print("usage: crawlbtc config restore <backup-dir> [--force]", file=sys.stderr)
        sys.exit(2)
    src = os.path.abspath(os.path.expanduser(src))
    manifest_path = os.path.join(src, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"no manifest.json in {src}", file=sys.stderr)
        sys.exit(2)
    with open(manifest_path) as f:
        manifest = json.load(f)

    dry = not args.force
    print(f"restore from: {src}")
    print(f"crawlbtc version at backup: {manifest.get('crawlbtc_version')}")
    print("MODE: dry-run (pass --force to write files)\n" if dry else "MODE: WRITING FILES\n")

    def restore_file(original, stored):
        stored_abs = os.path.join(src, stored)
        if not os.path.isfile(stored_abs):
            print(f"  skip (missing in backup): {stored}")
            return
        print(f"  {stored}  ->  {original}")
        if dry:
            return
        if os.path.exists(original):
            bak = f"{original}.bak-{_now_stamp()}"
            shutil.copy2(original, bak)
            print(f"    existing file preserved at {bak}")
        os.makedirs(os.path.dirname(original), exist_ok=True)
        shutil.copy2(stored_abs, original)
        try:
            os.chmod(original, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    item = manifest["items"].get("env_file")
    if item:
        print("env file:")
        restore_file(item["original"], item["stored"])

    bconf = manifest["items"].get("bitcoin_conf") or []
    if bconf:
        print("bitcoin.conf:")
        for it in bconf:
            restore_file(it["original"], it["stored"])

    role_sql = os.path.join(src, "postgres", "role_settings.sql")
    if os.path.isfile(role_sql):
        print("\npostgres role settings are NOT auto-applied. To re-apply, run as admin:")
        print(f"  psql -h {os.getenv('PG_HOST','')} -U <admin> -d <db> -f {role_sql}")

    if dry:
        print("\n(nothing written - re-run with --force to apply)")


def cmd_config(args, cfg):
    action = getattr(args, "action", "show") or "show"
    if action == "show":
        run_config_show(cfg, args)
    elif action == "backup":
        run_config_backup(cfg, args)
    elif action == "restore":
        run_config_restore(cfg, args)
    else:
        print(f"unknown config action: {action}", file=sys.stderr)
        sys.exit(2)
