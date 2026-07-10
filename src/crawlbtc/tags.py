"""`crawlbtc tags` - manage the known-entity reference table.

Populate blockchain.entity_tags from several sources, each recorded in the
`source` column so a label's provenance is always known:
  import-ofac   the US Treasury OFAC SDN sanctioned crypto addresses
  load-builtin  the shipped starter list (exchanges, illustrative)
  import        a generic CSV (address,entity_name[,category]) under --source
  add           a single address by hand (source 'user' by default)
  list/count/remove

trace joins the graph against this table to flag exchanges, mixers and
sanctioned addresses. Everything here is captured by `crawlbtc backup`.
"""

import csv
import os
import re
import sys
import urllib.request
from importlib import resources

import psycopg

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
_VALID_CATEGORIES = {"exchange", "mixer", "gambling", "sanctioned",
                     "mining", "service", "darknet", "scam", "other"}


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _require_table(cur):
    cur.execute("SELECT to_regclass('blockchain.entity_tags');")
    if cur.fetchone()[0] is None:
        print("blockchain.entity_tags does not exist - run `crawlbtc migrate` "
              "first (as the schema owner).", file=sys.stderr)
        sys.exit(1)


def _upsert(cur, rows):
    """rows: iterable of (address, entity_name, category, source, confidence, reference)."""
    n = 0
    for address, name, category, source, conf, ref in rows:
        if not address:
            continue
        cur.execute("""
            INSERT INTO blockchain.entity_tags
                (address, entity_name, category, source, confidence, reference)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (address, source) DO UPDATE
               SET entity_name = EXCLUDED.entity_name,
                   category    = EXCLUDED.category,
                   confidence  = EXCLUDED.confidence,
                   reference   = EXCLUDED.reference,
                   added_at    = now();
        """, (address, name, category, source, conf, ref))
        n += 1
    return n


# ---------- OFAC SDN ----------

def parse_ofac_sdn(data: bytes):
    """Extract crypto addresses from the OFAC SDN XML.

    Handles both the structured <idList><id> form (idType 'Digital Currency
    Address - XBT') and the free-text <remarks> form. Returns dicts with
    address, currency, name, program.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(data)
    loc = lambda e: e.tag.split("}")[-1]
    out, seen = [], set()

    for entry in root.iter():
        if loc(entry) != "sdnEntry":
            continue
        name_parts, program, remarks, ids = [], [], "", []
        for ch in entry:
            t = loc(ch)
            if t in ("lastName", "firstName") and ch.text:
                name_parts.append(ch.text)
            elif t == "remarks" and ch.text:
                remarks = ch.text
            elif t == "programList":
                program += [p.text for p in ch.iter() if loc(p) == "program" and p.text]
            elif t == "idList":
                for idel in ch.iter():
                    if loc(idel) != "id":
                        continue
                    idtype = idnum = None
                    for f in idel:
                        if loc(f) == "idType":
                            idtype = f.text
                        elif loc(f) == "idNumber":
                            idnum = f.text
                    if idtype and idnum and "Digital Currency Address" in idtype:
                        ids.append((idnum.strip(), idtype.split("-")[-1].strip()))
        for m in re.finditer(r"Digital Currency Address\s*-\s*(\w+)\s+([A-Za-z0-9]+)", remarks or ""):
            ids.append((m.group(2), m.group(1)))
        name = " ".join(name_parts) if name_parts else "OFAC SDN"
        prog = ", ".join(sorted(set(program)))
        for addr, cur_code in ids:
            if addr in seen:
                continue
            seen.add(addr)
            out.append({"address": addr, "currency": cur_code, "name": name, "program": prog})
    return out


def cmd_import_ofac(args, cfg):
    if args.file:
        with open(os.path.expanduser(args.file), "rb") as f:
            data = f.read()
    else:
        url = args.url or OFAC_SDN_URL
        print(f"fetching OFAC SDN list from {url} ...")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
        except Exception as e:
            print(f"could not fetch ({e}). Download sdn.xml manually and pass --file.",
                  file=sys.stderr)
            sys.exit(1)

    entries = parse_ofac_sdn(data)
    if not args.all:
        entries = [e for e in entries if e["currency"].upper() == "XBT"]
    rows = [(e["address"], f"{e['name']} [{e['program']}]".strip(), "sanctioned",
             "OFAC", 1.0, f"OFAC SDN Digital Currency Address - {e['currency']}")
            for e in entries]
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        n = _upsert(cur, rows)
    print(f"imported {n} OFAC sanctioned addresses"
          + ("" if args.all else " (Bitcoin/XBT only; --all for every chain)"))


def cmd_load_builtin(args, cfg):
    text = (resources.files("crawlbtc") / "data" / "entities_builtin.csv").read_text()
    rows = []
    for row in csv.DictReader(l for l in text.splitlines() if not l.startswith("#")):
        rows.append((row["address"], row["entity_name"], row["category"],
                     "builtin", float(row.get("confidence") or 0.7), row.get("reference")))
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        n = _upsert(cur, rows)
    print(f"loaded {n} builtin entity tags (source 'builtin' - verify before evidentiary use)")


def cmd_import(args, cfg):
    if not args.file or not args.source:
        print("usage: crawlbtc tags import --file <csv> --source <name> [--category <cat>]",
              file=sys.stderr)
        sys.exit(2)
    default_cat = args.category or "other"
    rows = []
    with open(os.path.expanduser(args.file), newline="") as f:
        reader = csv.reader(l for l in f if not l.startswith("#"))
        header = next(reader, None)
        has_header = header and header[0].lower() in ("address", "addr")
        if not has_header and header:
            reader = [header] + list(reader)
        for r in reader:
            if not r:
                continue
            addr = r[0].strip()
            name = r[1].strip() if len(r) > 1 else args.source
            cat = (r[2].strip() if len(r) > 2 else default_cat) or default_cat
            rows.append((addr, name, cat, args.source, args.confidence, None))
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        n = _upsert(cur, rows)
    print(f"imported {n} tags from {args.file} (source '{args.source}')")


def cmd_add(args, cfg):
    if len(args.rest) < 3:
        print("usage: crawlbtc tags add <address> <entity_name> <category> [--source user]",
              file=sys.stderr)
        sys.exit(2)
    address, name, category = args.rest[0], args.rest[1], args.rest[2]
    if category not in _VALID_CATEGORIES:
        print(f"category must be one of: {', '.join(sorted(_VALID_CATEGORIES))}", file=sys.stderr)
        sys.exit(2)
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        _upsert(cur, [(address, name, category, args.source or "user", args.confidence, None)])
    print(f"added {address} -> {name} ({category}, source '{args.source or 'user'}')")


def cmd_remove(args, cfg):
    if not args.rest:
        print("usage: crawlbtc tags remove <address> [--source <name>]", file=sys.stderr)
        sys.exit(2)
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        if args.source:
            cur.execute("DELETE FROM blockchain.entity_tags WHERE address=%s AND source=%s;",
                        (args.rest[0], args.source))
        else:
            cur.execute("DELETE FROM blockchain.entity_tags WHERE address=%s;", (args.rest[0],))
        print(f"removed {cur.rowcount} row(s)")


def cmd_list(args, cfg):
    conds, params = [], []
    if args.source:
        conds.append("source=%s")
        params.append(args.source)
    if args.category:
        conds.append("category=%s")
        params.append(args.category)
    if args.search:
        conds.append("(address ILIKE %s OR entity_name ILIKE %s)")
        params += [f"%{args.search}%", f"%{args.search}%"]
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.execute(f"""
            SELECT address, entity_name, category, source, confidence
            FROM blockchain.entity_tags {where}
            ORDER BY category, entity_name LIMIT %s;
        """, params + [args.limit])
        rows = cur.fetchall()
    for addr, name, cat, src, conf in rows:
        print(f"{addr:<44} {cat:<11} {src:<8} {conf if conf is not None else '':<4} {name}")
    print(f"({len(rows)} shown)")


def cmd_count(args, cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT address) FROM blockchain.entity_tags;")
        total, distinct = cur.fetchone()
        print(f"entity_tags: {total} rows, {distinct} distinct addresses")
        cur.execute("""
            SELECT source, category, COUNT(*) FROM blockchain.entity_tags
            GROUP BY source, category ORDER BY 3 DESC;
        """)
        for src, cat, n in cur.fetchall():
            print(f"  {src:<10} {cat:<12} {n}")


_ACTIONS = {
    "import-ofac": cmd_import_ofac, "load-builtin": cmd_load_builtin,
    "import": cmd_import, "add": cmd_add, "remove": cmd_remove,
    "list": cmd_list, "count": cmd_count,
}


def cmd_tags(args, cfg):
    fn = _ACTIONS.get(args.action)
    if fn is None:
        print(f"unknown tags action '{args.action}'. Actions: {', '.join(_ACTIONS)}", file=sys.stderr)
        sys.exit(2)
    fn(args, cfg)
