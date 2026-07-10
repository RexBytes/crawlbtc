"""`crawlbtc import-prices` - load a historical BTC price series for fiat
valuation at time-of-transaction.

Imports a CSV of date,price (daily). Columns are auto-detected:
  date/day/timestamp  -> the date (YYYY-MM-DD; timestamps are truncated)
  price/close/value   -> the BTC price in the chosen currency

Free sources: CoinGecko, Yahoo Finance, investing.com historical exports.
Keep the `source` label for provenance. `trace --fiat GBP` then values each
flow at the price on the transaction's date.
"""

import csv
import datetime
import os
import sys

import psycopg


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _require_table(cur):
    cur.execute("SELECT to_regclass('blockchain.btc_prices');")
    if cur.fetchone()[0] is None:
        print("blockchain.btc_prices does not exist - run `crawlbtc migrate` first "
              "(as the schema owner).", file=sys.stderr)
        sys.exit(1)


_DATE_KEYS = ("date", "day", "timestamp", "time", "snapped_at")
_PRICE_KEYS = ("price", "close", "value", "priceusd", "price_usd", "closing")


def _parse_date(s):
    s = s.strip().strip('"')
    if not s:
        return None
    datepart = s.split()[0].split("T")[0]           # drop any time component
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(datepart, fmt).date()
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:                                            # epoch seconds / millis
        v = float(s)
        if v > 1e11:
            v /= 1000.0
        return datetime.datetime.utcfromtimestamp(v).date()
    except ValueError:
        return None


def cmd_import_prices(args, cfg):
    if not args.csv:
        print("usage: crawlbtc import-prices --currency GBP --csv <file> [--source name]",
              file=sys.stderr)
        sys.exit(2)
    path = os.path.expanduser(args.csv)
    currency = (args.currency or "USD").upper()
    source = args.source or os.path.basename(path)

    with open(path, newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        print("empty CSV", file=sys.stderr)
        sys.exit(1)

    date_i = price_i = None
    start = 0
    if has_header:
        header = [h.strip().lower().replace(" ", "") for h in rows[0]]
        for i, h in enumerate(header):
            if date_i is None and any(k in h for k in _DATE_KEYS):
                date_i = i
            if price_i is None and any(h == k or k in h for k in _PRICE_KEYS):
                price_i = i
        start = 1
    if date_i is None or price_i is None:
        # positional fallback: col0=date, col1=price
        date_i, price_i = 0, 1
        print(f"note: columns not detected from header; assuming col{date_i}=date, "
              f"col{price_i}=price")

    parsed, skipped = [], 0
    for r in rows[start:]:
        if len(r) <= max(date_i, price_i):
            skipped += 1
            continue
        d = _parse_date(r[date_i])
        try:
            p = float(str(r[price_i]).replace(",", "").replace("$", "").replace("£", "").strip())
        except ValueError:
            p = None
        if d is None or p is None or p <= 0:
            skipped += 1
            continue
        parsed.append((d, currency, p, source))

    if not parsed:
        print("no valid rows parsed - check the CSV format", file=sys.stderr)
        sys.exit(1)

    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.executemany("""
            INSERT INTO blockchain.btc_prices (ts, currency, price, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ts, currency) DO UPDATE
              SET price = EXCLUDED.price, source = EXCLUDED.source, added_at = now();
        """, parsed)
        cur.execute("""
            SELECT MIN(ts), MAX(ts), COUNT(*) FROM blockchain.btc_prices WHERE currency=%s;
        """, (currency,))
        lo, hi, n = cur.fetchone()
    print(f"imported {len(parsed)} {currency} prices"
          + (f" ({skipped} rows skipped)" if skipped else ""))
    print(f"  {currency} series now spans {lo} .. {hi} ({n} days)")


def price_map(cur, currency, dates):
    """Return {date: price} for the given dates (price at-or-before each date)."""
    out = {}
    if not dates:
        return out
    for d in set(dates):
        if d is None:
            continue
        cur.execute("""
            SELECT price FROM blockchain.btc_prices
            WHERE currency = %s AND ts <= %s ORDER BY ts DESC LIMIT 1;
        """, (currency, d))
        row = cur.fetchone()
        if row:
            out[d] = float(row[0])
    return out
