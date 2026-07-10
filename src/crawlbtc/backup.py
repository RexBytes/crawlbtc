"""`crawlbtc backup` - consistent, evidence-grade logical backup + manifest.

Runs ONE `pg_dump` over the whole blockchain schema in directory format
with parallel jobs, which uses a single synchronized snapshot across all
workers - so every table is captured at the same instant (unlike separate
per-table dumps, which can disagree). Alongside the dump it writes a
provenance manifest (chain tip height+hash, per-table row estimates,
crawlbtc + pg_dump versions, and SHA-256 checksums of every dump file) so
a restored dataset can be proven identical to what a report relied on.

`backup`            create a dump under --out
`backup verify DIR` re-check a dump's files against its manifest
"""

import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys
import urllib.request

import orjson
import psycopg

from . import __version__

# Derived data excluded by default (rebuilt by `crawlbtc build-balances`).
_DEFAULT_EXCLUDE = ["address_balances"]


def _now():
    return datetime.datetime.now()


def _pg_env():
    env = os.environ.copy()
    if os.getenv("PG_PASSWORD"):
        env["PGPASSWORD"] = os.getenv("PG_PASSWORD")
    return env


def _pg_conn_args(user_override=None):
    return [
        "-h", os.getenv("PG_HOST", "localhost"),
        "-p", os.getenv("PG_PORT", "5432"),
        "-U", user_override or os.getenv("PG_USER", ""),
        "-d", os.getenv("PG_DB", "postgres"),
    ]


def _pg_dump_version():
    try:
        out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception as e:
        return f"unknown ({e})"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            yield os.path.join(dirpath, n)


def _row_estimates(cfg):
    try:
        with psycopg.connect(cfg.db_conninfo, autocommit=True, connect_timeout=10) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT c.relname, c.reltuples::bigint
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'blockchain' AND c.relkind = 'r'
                 ORDER BY c.relname;
            """)
            # reltuples is -1 for a table that has never been ANALYZEd.
            return {r[0]: (int(r[1]) if r[1] is not None and r[1] >= 0 else "unknown (run ANALYZE)")
                    for r in cur.fetchall()}
    except Exception as e:
        return {"_error": str(e)}


def _db_max_height(cfg):
    try:
        with psycopg.connect(cfg.db_conninfo, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute("SELECT max(height) FROM blockchain.block_jobs;").fetchone()
            return row[0]
    except Exception:
        return None


def _active_jobs(cfg):
    try:
        with psycopg.connect(cfg.db_conninfo, autocommit=True, connect_timeout=10) as conn:
            row = conn.execute("""
                SELECT COUNT(*) FROM blockchain.block_jobs
                 WHERE vout_status='in_progress' OR vin_status='in_progress'
                    OR address_status='in_progress';
            """).fetchone()
            return row[0]
    except Exception:
        return None


def _node_tip(cfg):
    """Best-effort node tip (height, hash) to anchor the backup. None on failure."""
    try:
        user = cfg.rpc_user
        pw = cfg.rpc_password

        def call(method, params):
            data = orjson.dumps({"jsonrpc": "1.0", "id": "backup", "method": method, "params": params})
            req = urllib.request.Request(cfg.rpc_url, data=data,
                                         headers={"Content-Type": "application/json"})
            tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
            with urllib.request.urlopen(req, timeout=15) as r:
                return orjson.loads(r.read())["result"]

        tip = call("getblockcount", [])
        return {"height": tip, "hash": call("getblockhash", [tip])}
    except Exception:
        return None


def _height_hash(cfg, height):
    if height is None:
        return None
    try:
        user, pw = cfg.rpc_user, cfg.rpc_password
        data = orjson.dumps({"jsonrpc": "1.0", "id": "backup", "method": "getblockhash",
                             "params": [height]})
        req = urllib.request.Request(cfg.rpc_url, data=data,
                                     headers={"Content-Type": "application/json"})
        tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
        with urllib.request.urlopen(req, timeout=15) as r:
            return orjson.loads(r.read())["result"]
    except Exception:
        return None


def run_backup(cfg, args):
    out = os.path.abspath(os.path.expanduser(args.path or os.getcwd()))
    stamp = _now().strftime("%Y-%m-%d_%H%M%S")
    dump_dir = os.path.join(out, f"blockchain_{stamp}")
    if os.path.exists(dump_dir):
        print(f"target already exists: {dump_dir}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(out, exist_ok=True)

    exclude = list(_DEFAULT_EXCLUDE)
    if args.include_derived:
        exclude = []

    active = _active_jobs(cfg)
    if active:
        print(f"note: {active:,} blocks are in_progress (crawler running). The dump is "
              "still internally consistent (single snapshot); it just captures a mid-crawl "
              "state. For a quiescent baseline, back up when idle.")

    cmd = ["pg_dump", *_pg_conn_args(args.pg_user),
           "--schema=blockchain", "--format=directory",
           f"--jobs={args.jobs}", f"--compress={args.compress}",
           "--no-owner", "--no-privileges", "-f", dump_dir]
    for t in exclude:
        cmd += [f"--exclude-table=blockchain.{t}"]

    print(f"dumping blockchain schema -> {dump_dir}")
    print(f"  (parallel jobs={args.jobs}, compress={args.compress}, "
          f"excluded={exclude or 'none'})")
    started = _now()
    try:
        subprocess.run(cmd, env=_pg_env(), check=True)
    except subprocess.CalledProcessError as e:
        print(f"pg_dump failed (exit {e.returncode})", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("pg_dump not found on PATH - install postgresql client tools", file=sys.stderr)
        sys.exit(1)
    finished = _now()

    # Provenance
    db_max = _db_max_height(cfg)
    manifest = {
        "created": started.isoformat(),
        "finished": finished.isoformat(),
        "crawlbtc_version": __version__,
        "pg_dump_version": _pg_dump_version(),
        "database": os.getenv("PG_DB"),
        "schema": "blockchain",
        "format": "directory",
        "excluded_tables": exclude,
        "chain_tip": {
            "db_max_height": db_max,
            "db_max_block_hash": _height_hash(cfg, db_max),
            "node": _node_tip(cfg),
        },
        "row_estimates": _row_estimates(cfg),
        "files": [],
    }

    total_bytes = 0
    do_checksum = not args.no_checksum
    if do_checksum:
        print("computing SHA-256 checksums (large dumps take a while; --no-checksum to skip)...")
    for path in _walk_files(dump_dir):
        size = os.path.getsize(path)
        total_bytes += size
        entry = {"path": os.path.relpath(path, dump_dir), "bytes": size}
        if do_checksum:
            entry["sha256"] = _sha256(path)
        manifest["files"].append(entry)
    manifest["total_bytes"] = total_bytes
    manifest["checksummed"] = do_checksum

    with open(os.path.join(dump_dir, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    _write_manifest_txt(dump_dir, manifest)

    gb = total_bytes / (1024 ** 3)
    print(f"\nbackup complete: {dump_dir}")
    print(f"  size: {gb:.2f} GiB across {len(manifest['files'])} files")
    if db_max is not None:
        print(f"  chain tip pinned at db height {db_max:,}"
              + (f" ({manifest['chain_tip']['db_max_block_hash'][:16]}...)"
                 if manifest['chain_tip']['db_max_block_hash'] else ""))
    print(f"  manifest: {os.path.join(dump_dir, 'MANIFEST.json')}")
    print("\nrestore with:")
    print(f"  createdb -U pgadmin blockchain_restore")
    print(f"  pg_restore -U pgadmin -d blockchain_restore --jobs={args.jobs} {dump_dir}")
    print("verify integrity later with:")
    print(f"  crawlbtc backup verify {dump_dir}")


def _write_manifest_txt(dump_dir, m):
    lines = [
        "crawlbtc backup manifest",
        f"created:          {m['created']}",
        f"finished:         {m['finished']}",
        f"crawlbtc version: {m['crawlbtc_version']}",
        f"pg_dump:          {m['pg_dump_version']}",
        f"database/schema:  {m['database']} / {m['schema']}",
        f"excluded tables:  {', '.join(m['excluded_tables']) or 'none'}",
        f"checksummed:      {m['checksummed']}",
        f"total bytes:      {m['total_bytes']:,}",
        "",
        "chain tip:",
        f"  db max height:  {m['chain_tip']['db_max_height']}",
        f"  db block hash:  {m['chain_tip']['db_max_block_hash']}",
        f"  node:           {m['chain_tip']['node']}",
        "",
        "row estimates (from pg_class.reltuples - approximate):",
    ]
    for t, n in m["row_estimates"].items():
        lines.append(f"  {t:<20} {n:>18,}" if isinstance(n, int) else f"  {t}: {n}")
    with open(os.path.join(dump_dir, "MANIFEST.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")


def run_verify(cfg, args):
    dump_dir = os.path.abspath(os.path.expanduser(args.path or ""))
    manifest_path = os.path.join(dump_dir, "MANIFEST.json")
    if not os.path.isfile(manifest_path):
        print(f"no MANIFEST.json in {dump_dir}", file=sys.stderr)
        sys.exit(2)
    with open(manifest_path) as f:
        manifest = json.load(f)
    if not manifest.get("checksummed"):
        print("this backup was created with --no-checksum; cannot verify contents "
              "(only presence/size).")
    ok, bad, missing = 0, 0, 0
    for entry in manifest["files"]:
        path = os.path.join(dump_dir, entry["path"])
        if not os.path.isfile(path):
            print(f"MISSING  {entry['path']}")
            missing += 1
            continue
        if os.path.getsize(path) != entry["bytes"]:
            print(f"SIZE     {entry['path']} (expected {entry['bytes']}, got {os.path.getsize(path)})")
            bad += 1
            continue
        if entry.get("sha256"):
            if _sha256(path) != entry["sha256"]:
                print(f"CHECKSUM {entry['path']}")
                bad += 1
                continue
        ok += 1
    print(f"\nverified: {ok} ok, {bad} corrupt, {missing} missing "
          f"(of {len(manifest['files'])} files)")
    if bad or missing:
        sys.exit(1)
    print("backup integrity OK")


def cmd_backup(args, cfg):
    # Flush prints line-by-line so a nohup/redirected log is live, not
    # buffered until the (hours-later) process exit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    action = getattr(args, "action", None) or "create"
    # Allow `crawlbtc backup <path>` (path given as the first positional).
    if action not in ("create", "verify"):
        if getattr(args, "path", None) is None:
            args.path = action
        action = "create"
    if action == "create":
        run_backup(cfg, args)
    else:
        run_verify(cfg, args)
