"""`crawlbtc trace <address>` - follow the money out from an address.

Builds a directed address-flow graph outward from a starting address to a
bounded depth, with per-edge value estimated by the haircut model (each
recipient's share weighted by the sender's contribution to that
transaction). Reports the origin's incoming funders and outgoing
recipients, detects flows that loop back to the origin, and writes:

  <addr>_trace.html   self-contained interactive graph (send-able, offline)
  <addr>_trace.xlsx   nodes / edges / origin in / origin out / summary
  <addr>_trace.json   the raw graph

Everything is read-only and uses indexed lookups. High-degree "hub"
addresses (exchanges etc.) are detected and NOT expanded, so the graph
cannot explode. All caps are reported so nothing is silently dropped.
"""

import datetime
import json
import os
import sys
import time
from collections import deque

import psycopg

from .trace_template import TEMPLATE

SATS = 100_000_000

# An address with more than this many outputs is treated as a hub
# (exchange/service/mixer) and not expanded further.
HUB_UTXO_THRESHOLD = 20_000


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _utxo_count(cur, address):
    cur.execute("""
        SELECT COUNT(*) FROM blockchain.transaction_io
        WHERE address = %s AND io_type = 'out';
    """, (address,))
    return cur.fetchone()[0]


def _outgoing_edges(cur, address, fanout, max_utxos=2000):
    """Top `fanout` recipients of value sent FROM `address`, haircut-weighted.

    Only the `max_utxos` largest outputs of the address are followed, which
    bounds the query on high-activity (many-small-UTXO) addresses regardless
    of total transaction count.
    """
    cur.execute("""
        WITH u_utxos AS (
            SELECT txid, idx, amount
              FROM blockchain.transaction_io
             WHERE address = %s AND io_type = 'out'
             ORDER BY amount DESC
             LIMIT %s
        ),
        spent AS (
            SELECT s.spending_txid, SUM(u.amount) AS u_in
              FROM u_utxos u
              JOIN blockchain.spends s
                ON s.prev_txid = u.txid AND s.prev_vout = u.idx
             GROUP BY s.spending_txid
        ),
        tx_in_counts AS (
            -- We only ever test n_inputs = 1 (direct vs co-mingled), so the
            -- exact input count is irrelevant past 2. Capping with LIMIT 2
            -- means a huge consolidation/sweep tx (thousands of inputs) reads
            -- 2 spends rows instead of all of them - this is what kept the
            -- outgoing query from finishing on high-activity addresses.
            SELECT sp.spending_txid,
                   (SELECT count(*) FROM (
                        SELECT 1 FROM blockchain.spends s2
                         WHERE s2.spending_txid = sp.spending_txid
                         LIMIT 2) _c) AS n_inputs
              FROM spent sp
        ),
        tx_out AS (
            SELECT sp.spending_txid, o.address AS to_addr, SUM(o.amount) AS v_out
              FROM spent sp
              JOIN blockchain.transaction_io o
                ON o.txid = sp.spending_txid AND o.io_type = 'out'
             WHERE o.address IS NOT NULL
             GROUP BY sp.spending_txid, o.address
        )
        SELECT t.to_addr,
               SUM(t.v_out * (sp.u_in::numeric / NULLIF(tx.total_in, 0)))::bigint AS est_value,
               COUNT(DISTINCT t.spending_txid) AS tx_count,
               MIN(tx.received_time) AS first_seen,
               MAX(tx.received_time) AS last_seen,
               bool_and(tic.n_inputs = 1) AS all_single_input
          FROM tx_out t
          JOIN spent sp ON sp.spending_txid = t.spending_txid
          JOIN tx_in_counts tic ON tic.spending_txid = t.spending_txid
          JOIN blockchain.transactions tx ON tx.txid = t.spending_txid
         GROUP BY t.to_addr
         ORDER BY est_value DESC NULLS LAST
         LIMIT %s;
    """, (address, max_utxos, fanout))
    return cur.fetchall()


def _incoming_edges(cur, address, fanout, max_utxos=2000):
    """Top `fanout` funders that paid value INTO `address` (bounded)."""
    cur.execute("""
        WITH u_recv AS (
            SELECT DISTINCT txid
              FROM blockchain.transaction_io
             WHERE address = %s AND io_type = 'out'
             LIMIT %s
        ),
        recv_in_counts AS (
            SELECT r.txid, COUNT(*) AS n_inputs
              FROM u_recv r
              JOIN blockchain.spends s ON s.spending_txid = r.txid
             GROUP BY r.txid
        )
        SELECT i.address AS from_addr,
               SUM(i.amount)::bigint AS value_in,
               COUNT(DISTINCT i.txid) AS tx_count,
               MIN(tx.received_time) AS first_seen,
               MAX(tx.received_time) AS last_seen,
               bool_and(COALESCE(ric.n_inputs, 1) = 1) AS all_single_input
          FROM blockchain.transaction_io i
          JOIN u_recv r ON r.txid = i.txid
          JOIN blockchain.transactions tx ON tx.txid = i.txid
          LEFT JOIN recv_in_counts ric ON ric.txid = i.txid
         WHERE i.io_type = 'in' AND i.address IS NOT NULL
         GROUP BY i.address
         ORDER BY value_in DESC NULLS LAST
         LIMIT %s;
    """, (address, max_utxos, fanout))
    return cur.fetchall()


def _entity_lookup(cur, addresses):
    """Return {address: (entity_name, category, source)} for known entities.

    Prefers sanctioned > mixer > everything else when an address has tags
    from multiple sources. Returns {} if the entity_tags table is absent.
    """
    if not addresses:
        return {}
    try:
        cur.execute("""
            SELECT DISTINCT ON (address) address, entity_name, category, source
            FROM blockchain.entity_tags
            WHERE address = ANY(%s)
            ORDER BY address,
                     CASE category WHEN 'sanctioned' THEN 0 WHEN 'mixer' THEN 1
                                   WHEN 'exchange' THEN 2 ELSE 3 END,
                     confidence DESC NULLS LAST;
        """, (list(addresses),))
        return {a: (name, cat, src) for a, name, cat, src in cur.fetchall()}
    except (psycopg.errors.UndefinedTable, psycopg.errors.QueryCanceled):
        return {}


def _addr_totals(cur, address):
    """Total received / sent (sats) and utxo count for an address."""
    cur.execute("""
        SELECT COALESCE(SUM(amount) FILTER (WHERE io_type='out'), 0),
               COALESCE(SUM(amount) FILTER (WHERE io_type='in'), 0)
          FROM blockchain.transaction_io
         WHERE address = %s;
    """, (address,))
    recv, sent = cur.fetchone()
    return int(recv), int(sent)


def _origin_cluster(cur, origin, tx_cap=2000, addr_cap=8000):
    """Addresses probably owned by the SAME entity as `origin`.

    Common-input-ownership heuristic: addresses co-spent as inputs in the
    same transaction are almost certainly one wallet. One bounded round
    (direct co-inputs) - transitive expansion is intentionally NOT done, as
    it over-merges through CoinJoin/PayJoin. Always includes the origin.
    Bounded by tx_cap/addr_cap and the session statement_timeout.
    """
    try:
        cur.execute("""
            WITH origin_spends AS (
              SELECT DISTINCT s.spending_txid
                FROM blockchain.transaction_io o
                JOIN blockchain.spends s ON s.prev_txid = o.txid AND s.prev_vout = o.idx
               WHERE o.address = %s AND o.io_type = 'out'
               LIMIT %s
            )
            SELECT DISTINCT i.address
              FROM origin_spends os
              JOIN blockchain.transaction_io i
                ON i.txid = os.spending_txid AND i.io_type = 'in'
             WHERE i.address IS NOT NULL
             LIMIT %s;
        """, (origin, tx_cap, addr_cap))
        return {r[0] for r in cur.fetchall()} | {origin}
    except psycopg.errors.QueryCanceled:
        return {origin}


def build_graph(cfg, origin, depth, fanout, max_nodes, direction="out", cluster=True,
                timeout=60, progress=True, max_utxos=2000, fiat=None):
    t0 = time.time()

    def tick(msg):
        if progress:
            print(f"  [{time.time() - t0:5.0f}s] {msg}", file=sys.stderr, flush=True)

    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = '{int(timeout)}s';")

        nodes = {}
        edges = []
        loops = []   # edges that return value to the origin's cluster (round-trips)

        if cluster:
            tick("finding origin's wallet cluster (common-input)...")
        cluster_addrs = _origin_cluster(cur, origin) if cluster else {origin}
        if cluster:
            tick(f"cluster: {len(cluster_addrs)} address(es) probably same owner")

        timeouts = [0]

        def ensure_node(addr, d, side):
            if addr not in nodes:
                try:
                    recv, sent = _addr_totals(cur, addr)
                except psycopg.errors.QueryCanceled:
                    timeouts[0] += 1
                    recv, sent = 0, 0
                nodes[addr] = {
                    "address": addr, "depth": abs(d), "side": side,
                    "level": 0 if addr == origin else (abs(d) if side == "out" else -abs(d)),
                    "is_origin": addr == origin,
                    "total_received_btc": recv / SATS,
                    "total_sent_btc": sent / SATS,
                    "same_owner": (addr in cluster_addrs and addr != origin),
                    "is_hub": False, "truncated": False,
                }
            return nodes[addr]

        ensure_node(origin, 0, "origin")

        def expand(dir_):
            tick(f"expanding {dir_}going flow, depth {depth} ...")
            q = deque([(origin, 0)])
            seen = set()
            while q:
                addr, d = q.popleft()
                if addr in seen or d >= depth:
                    continue
                seen.add(addr)
                tick(f"{dir_} L{d} | {len(nodes)} nodes, {len(edges)} flows, "
                     f"queue {len(q)} | {addr[:16]}…")
                # Any slow query on a busy address hits statement_timeout;
                # skip that node instead of aborting the whole trace.
                try:
                    if _utxo_count(cur, addr) > HUB_UTXO_THRESHOLD and addr != origin:
                        nodes[addr]["is_hub"] = True
                        continue
                    rows = (_outgoing_edges if dir_ == "out" else _incoming_edges)(
                        cur, addr, fanout, max_utxos)
                except psycopg.errors.QueryCanceled:
                    timeouts[0] += 1
                    nodes[addr]["timed_out"] = True
                    continue
                if len(rows) >= fanout:
                    nodes[addr]["truncated"] = True
                for other, est_value, tx_count, first_seen, last_seen, single_in in rows:
                    if other is None:
                        continue
                    ensure_node(other, d + 1, dir_)
                    frm, to = (addr, other) if dir_ == "out" else (other, addr)
                    returns_to_owner = (other in cluster_addrs)
                    edge = {
                        "from": frm, "to": to,
                        "value_btc": (est_value or 0) / SATS,
                        "tx_count": tx_count,
                        "first_seen": str(first_seen) if first_seen else None,
                        "last_seen": str(last_seen) if last_seen else None,
                        "dir": dir_,
                        "confidence": round(max(0.15, 0.8 ** d), 3),
                        "returns_to_owner": returns_to_owner,
                        # single-input spending tx => value attribution is a
                        # fact (sole funder); multi-input => co-mingled/estimated.
                        "attribution": "direct" if single_in else "co-mingled",
                        "certain": bool(single_in),
                    }
                    edges.append(edge)
                    if returns_to_owner:
                        loops.append(edge)
                    if len(nodes) >= max_nodes:
                        q.clear()
                        break
                    if d + 1 < depth and other not in cluster_addrs:
                        q.append((other, d + 1))

        if direction in ("out", "both"):
            expand("out")
        if direction in ("in", "both"):
            expand("in")

        # Origin's immediate funders, for the incoming-context panel.
        try:
            inc_rows = _incoming_edges(cur, origin, fanout, max_utxos)
        except psycopg.errors.QueryCanceled:
            timeouts[0] += 1
            inc_rows = []
        origin_incoming = [{
            "address": fr, "value_btc": (v or 0) / SATS, "tx_count": tc,
            "first_seen": str(fs) if fs else None, "last_seen": str(ls) if ls else None,
            "same_owner": fr in cluster_addrs,
            "attribution": "direct" if single_in else "co-mingled",
        } for fr, v, tc, fs, ls, single_in in inc_rows]

        # Known-entity labels.
        for addr, (name, cat, src) in _entity_lookup(cur, nodes.keys()).items():
            nodes[addr]["entity"] = name
            nodes[addr]["entity_category"] = cat
            nodes[addr]["entity_source"] = src

        # Fiat valuation at each flow's transaction date (optional).
        fiat_total = None
        if fiat:
            from .prices import price_map
            import datetime as _dt
            def _edate(e):
                fs = e.get("first_seen")
                if not fs:
                    return None
                try:
                    return _dt.date.fromisoformat(fs[:10])
                except ValueError:
                    return None
            pm = price_map(cur, fiat.upper(), [_edate(e) for e in edges])
            fiat_total = 0.0
            for e in edges:
                d = _edate(e)
                pr = pm.get(d) if d else None
                e["value_fiat"] = round(e["value_btc"] * pr, 2) if pr else None
                if e["value_fiat"]:
                    fiat_total += e["value_fiat"]
            tick(f"valued flows in {fiat.upper()} (prices for {len(pm)} dates found)")

        try:
            cur.execute("SELECT max(height) FROM blockchain.block_jobs;")
            tip_height = cur.fetchone()[0]
        except Exception:
            tip_height = None

        o = nodes[origin]
        related = [n for n in nodes.values() if n["same_owner"]]
        return {
            "origin": origin,
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tip": f"db height {tip_height}" if tip_height is not None else "unknown",
            "params": {"depth": depth, "fanout": fanout, "max_nodes": max_nodes,
                       "direction": direction, "clustering": cluster,
                       "fiat": fiat.upper() if fiat else None,
                       "hub_threshold": HUB_UTXO_THRESHOLD},
            "fiat_currency": fiat.upper() if fiat else None,
            "fiat_flow_total": round(fiat_total, 2) if fiat_total is not None else None,
            "origin_summary": {
                "total_received_btc": o["total_received_btc"],
                "total_sent_btc": o["total_sent_btc"],
                "distinct_funders_shown": len(origin_incoming),
                "related_wallets": len(related),
                "cluster_size": len(cluster_addrs),
                "round_trips_to_owner": len(loops),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "direct_flows": sum(1 for e in edges if e["certain"]),
                "comingled_flows": sum(1 for e in edges if not e["certain"]),
                "capped": len(nodes) >= max_nodes,
                "query_timeouts": timeouts[0],
            },
            "nodes": list(nodes.values()),
            "edges": edges,
            "origin_incoming": origin_incoming,
            "related_wallets": [n["address"] for n in related],
            "loops": loops,
        }


# ---------- outputs ----------

def write_json(graph, path):
    with open(path, "w") as f:
        json.dump(graph, f, indent=2)


def write_xlsx(graph, path, title="Bitcoin address trace"):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Summary"
    s = graph["origin_summary"]
    rows = [
        (title, ""),
        ("origin", graph["origin"]),
        ("generated", graph["generated"]),
        ("direction", graph["params"]["direction"]),
        ("depth", graph["params"]["depth"]),
        ("max fan-out per node", graph["params"]["fanout"]),
        ("", ""),
        ("total received (BTC)", round(s["total_received_btc"], 8)),
        ("total sent (BTC)", round(s["total_sent_btc"], 8)),
        ("funders shown", s["distinct_funders_shown"]),
        ("addresses in graph", s["node_count"]),
        ("flows (edges)", s["edge_count"]),
        ("related wallets (same owner)", s["related_wallets"]),
        ("origin cluster size", s["cluster_size"]),
        ("round-trips to owner", s["round_trips_to_owner"]),
        ("graph hit node cap", s["capped"]),
    ]
    for r in rows:
        ws.append(r)
    ws["A1"].font = bold

    ws = wb.create_sheet("Nodes")
    ws.append(["address", "level", "side", "is_origin", "same_owner", "entity",
               "category", "is_hub", "total_received_btc", "total_sent_btc"])
    for c in ws[1]:
        c.font = bold
    for n in sorted(graph["nodes"], key=lambda x: (x["level"], -x["total_received_btc"])):
        ws.append([n["address"], n["level"], n["side"], n["is_origin"], n["same_owner"],
                   n.get("entity", ""), n.get("entity_category", ""), n["is_hub"],
                   round(n["total_received_btc"], 8), round(n["total_sent_btc"], 8)])

    ws = wb.create_sheet("Flows (edges)")
    fiat_cur = graph.get("fiat_currency")
    hdr = ["from", "to", "dir", "attribution", "est_value_btc"]
    if fiat_cur:
        hdr.append(f"est_value_{fiat_cur}")
    hdr += ["tx_count", "confidence", "returns_to_owner", "first_seen", "last_seen"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = bold
    for e in sorted(graph["edges"], key=lambda x: -x["value_btc"]):
        row = [e["from"], e["to"], e["dir"], e["attribution"], round(e["value_btc"], 8)]
        if fiat_cur:
            row.append(e.get("value_fiat"))
        row += [e["tx_count"], e["confidence"], e["returns_to_owner"], e["first_seen"], e["last_seen"]]
        ws.append(row)

    ws = wb.create_sheet("Origin incoming")
    ws.append(["funder_address", "value_btc", "tx_count", "same_owner", "first_seen", "last_seen"])
    for c in ws[1]:
        c.font = bold
    for f in graph["origin_incoming"]:
        ws.append([f["address"], round(f["value_btc"], 8), f["tx_count"],
                   f.get("same_owner", False), f["first_seen"], f["last_seen"]])

    if graph["related_wallets"]:
        ws = wb.create_sheet("Related wallets")
        ws.append(["address (probably same owner as origin)"])
        ws["A1"].font = bold
        for a in graph["related_wallets"]:
            ws.append([a])

    wb.save(path)


def write_html(graph, path, title="Bitcoin address trace report"):
    """Render the full multi-tab report (see trace_template.TEMPLATE)."""
    def when(e):
        fs = e.get("first_seen")
        return fs[:7] if fs else ""
    data = {
        "title": title,
        "origin": graph["origin"],
        "generated": graph["generated"],
        "tip": graph.get("tip", "unknown"),
        "direction": graph["params"]["direction"],
        "explorer": "https://mempool.space",
        "nodes": [{"id": n["address"], "depth": n["depth"], "side": n["side"],
                   "origin": n["is_origin"], "hub": n["is_hub"], "owner": n["same_owner"],
                   "entity": n.get("entity"), "etype": n.get("entity_category"),
                   "recv": round(n["total_received_btc"], 6),
                   "sent": round(n["total_sent_btc"], 6)} for n in graph["nodes"]],
        "fiat": graph.get("fiat_currency"),
        "edges": [{"s": e["from"], "t": e["to"], "v": round(e["value_btc"], 6),
                   "n": e["tx_count"], "when": when(e), "dir": e["dir"],
                   "loop": e["returns_to_owner"], "cert": e["certain"],
                   "vf": e.get("value_fiat")}
                  for e in graph["edges"]],
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data))
    with open(path, "w") as f:
        f.write(html)


def cmd_trace(args, cfg):
    origin = args.address.strip()
    out_dir = os.path.abspath(os.path.expanduser(args.out or os.getcwd()))
    os.makedirs(out_dir, exist_ok=True)

    # Warn about combinatorial explosion: worst case ~ fanout^depth nodes.
    worst = args.fanout ** args.depth
    if worst > args.max_nodes:
        print(f"note: depth {args.depth} x fanout {args.fanout} could reach ~{worst:,} "
              f"addresses but is capped at {args.max_nodes:,}. For deep traces lower "
              f"--fanout (e.g. --fanout 3 --depth {args.depth} = {3**args.depth:,}) so the "
              f"picture isn't truncated. High-degree hubs are pruned automatically.")

    print(f"tracing {origin} (direction={args.direction}, depth={args.depth}, "
          f"fanout={args.fanout}) ...")
    try:
        graph = build_graph(cfg, origin, args.depth, args.fanout, args.max_nodes,
                            direction=args.direction, cluster=not args.no_cluster,
                            timeout=args.timeout, max_utxos=args.max_utxos, fiat=args.fiat)
    except psycopg.errors.QueryCanceled:
        print(f"the origin's own query timed out ({args.timeout}s). The address is very "
              f"busy or the cache is cold (e.g. just after a backup). Retry (cache warms), "
              f"raise --timeout, or lower --fanout.", file=sys.stderr)
        sys.exit(1)

    s = graph["origin_summary"]
    print(f"  received {s['total_received_btc']:.8f} BTC, sent {s['total_sent_btc']:.8f} BTC")
    print(f"  graph: {s['node_count']} addresses, {s['edge_count']} flows "
          f"({s['direct_flows']} direct/certain, {s['comingled_flows']} co-mingled/estimated)")
    print(f"  related wallets (same owner): {s['related_wallets']} "
          f"(origin cluster {s['cluster_size']}), round-trips to owner: {s['round_trips_to_owner']}"
          + ("  [hit node cap]" if s["capped"] else ""))
    if graph.get("fiat_flow_total") is not None:
        print(f"  fiat: flows total ~{graph['fiat_flow_total']:,.2f} {graph['fiat_currency']} "
              f"(valued at each tx's date)")
    if s.get("query_timeouts"):
        print(f"  note: {s['query_timeouts']} node(s) skipped on query timeout "
              f"(busy/cold-cache); partial graph written. Retry to warm cache or raise --timeout.")

    title = args.report_title or "Bitcoin address trace report"
    base = os.path.join(out_dir, f"{origin}_trace")
    write_json(graph, base + ".json")
    write_html(graph, base + ".html", title=title)
    try:
        write_xlsx(graph, base + ".xlsx", title=title)
        xlsx_note = base + ".xlsx"
    except ImportError:
        xlsx_note = "(openpyxl not installed - skipped .xlsx; pip install openpyxl)"

    print("\nwrote:")
    print(f"  {base}.html   (open / send this)")
    print(f"  {xlsx_note}")
    print(f"  {base}.json")
