import datetime

from crawlbtc.phases.extract import classify_block_features, parse_block

GENESIS_PK = ("04678afdb0fe5548271967f1a67130b7105cd6a828e03909a67962e0ea1f61de"
              "b649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5f")
GENESIS_ADDR = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"

COINBASE_TX = {
    "txid": "a" * 64,
    "vin": [{"coinbase": "04ffff001d0104"}],
    "vout": [{"value": 50.0, "n": 0, "scriptPubKey": {
        "type": "pubkey", "hex": "41" + GENESIS_PK + "ac",
        "asm": f"{GENESIS_PK} OP_CHECKSIG"}}],
}

SPEND_TX = {
    "txid": "d" * 64,
    "vin": [{"txid": "a" * 64, "vout": 0,
             "prevout": {"value": 50.0, "scriptPubKey": {
                 "type": "pubkey", "hex": "41" + GENESIS_PK + "ac"}}}],
    "vout": [
        {"value": 49.9, "n": 0, "scriptPubKey": {
            "type": "witness_v0_keyhash", "address": "bc1qtest"}},
        {"value": 0.0, "n": 1, "scriptPubKey": {
            "type": "nulldata", "asm": "OP_RETURN 6869"}},
    ],
}


def _block(txs):
    return {"hash": "f" * 64, "time": 1231006505, "tx": txs}


def test_parse_block_p2pk_coinbase():
    tx_rows, out_rows, in_rows, spends, features = parse_block(
        _block([COINBASE_TX]), "f" * 64, 0, True, "p2pk")
    assert tx_rows == [("a" * 64, "f" * 64, 1231006505, 0, 5_000_000_000)]
    assert out_rows == [("a" * 64, GENESIS_ADDR, "p2pk", 5_000_000_000, "out", 0)]
    assert in_rows == [] and spends == []
    assert features == "vout"


def test_parse_block_spend_with_prevouts():
    tx_rows, out_rows, in_rows, spends, features = parse_block(
        _block([SPEND_TX]), "f" * 64, 7, True, "p2pk")
    # totals: in from prevout, out skips OP_RETURN
    assert tx_rows == [("d" * 64, "f" * 64, 1231006505, 5_000_000_000, 4_990_000_000)]
    assert in_rows == [("d" * 64, GENESIS_ADDR, "p2pk", 5_000_000_000, "in", 0)]
    assert out_rows == [("d" * 64, "bc1qtest", "bech32", 4_990_000_000, "out", 0)]
    assert len(spends) == 1
    prev_txid, prev_vout, spending_txid, spending_vin, height, block_hash, ts = spends[0]
    assert (prev_txid, prev_vout, spending_txid, spending_vin, height) == \
        ("a" * 64, 0, "d" * 64, 0, 7)
    assert isinstance(ts, datetime.datetime) and ts.tzinfo is not None
    assert features == "both"


def test_parse_block_verbosity2_fallback():
    """Without prevouts: spends still recorded, vin rows deferred, total_in 0."""
    tx_rows, out_rows, in_rows, spends, features = parse_block(
        _block([SPEND_TX]), "f" * 64, 7, False, "p2pk")
    assert in_rows == []
    assert len(spends) == 1
    assert tx_rows[0][3] == 0  # total_in not computed here


def test_classify_spendable_but_addressless():
    # legacy semantics: a value>0 non-OP_RETURN output that produced no
    # address rows (e.g. nonstandard script) classifies as op_return_only
    tx = {
        "txid": "e" * 64,
        "vin": [{"coinbase": "aa"}],
        "vout": [{"value": 1.0, "scriptPubKey": {"type": "nonstandard", "asm": "OP_2DUP"}}],
    }
    assert classify_block_features(False, False, [tx]) == "op_return_only"


def test_classify_coinbase_only():
    cb = {"txid": "e" * 64, "vin": [{"coinbase": "aa"}],
          "vout": [{"value": 0.0, "scriptPubKey": {"type": "nonstandard", "asm": ""}}]}
    assert classify_block_features(False, False, [cb]) == "coinbase_only"
