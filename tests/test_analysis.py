from crawlbtc.analysis import (
    classify_shape,
    coin_days_destroyed,
    detect_change_output,
    transaction_entropy,
)

SATS = 100_000_000


def test_cdd_dormant_flag():
    r = coin_days_destroyed([{"value": SATS, "age_days": 365.25 * 5}])
    assert r["dormant"] is True
    assert r["oldest_input_years"] == 5.0
    # 1 BTC * ~1826 days
    assert r["coin_days_destroyed"] > 1800


def test_cdd_recent_not_dormant():
    r = coin_days_destroyed([{"value": SATS, "age_days": 10}])
    assert r["dormant"] is False


def test_change_self_change_wins():
    inputs = [{"address": "A", "value": 5 * SATS, "script_type": "bech32"}]
    outputs = [
        {"address": "B", "value": 3 * SATS, "script_type": "bech32", "is_fresh": True},
        {"address": "A", "value": 2 * SATS, "script_type": "bech32", "is_fresh": False},
    ]
    ch = detect_change_output(inputs, outputs)
    assert ch is not None
    assert ch["address"] == "A"  # pays back an input address


def test_change_round_vs_nonround():
    # payment is a round 1.00000000 BTC, change is a messy remainder
    inputs = [{"address": "A", "value": 150_000_000, "script_type": "bech32"}]
    outputs = [
        {"address": "PAY", "value": 100_000_000, "script_type": "p2pkh", "is_fresh": False},
        {"address": "CHG", "value": 49_873_210, "script_type": "bech32", "is_fresh": True},
    ]
    ch = detect_change_output(inputs, outputs)
    assert ch is not None
    assert ch["address"] == "CHG"


def test_change_none_for_coinjoin_like():
    inputs = [{"address": f"I{i}", "value": SATS, "script_type": "bech32"} for i in range(3)]
    outputs = [{"address": f"O{i}", "value": SATS, "script_type": "bech32", "is_fresh": True}
               for i in range(3)]
    assert detect_change_output(inputs, outputs) is None


def test_change_none_single_output():
    assert detect_change_output([{"address": "A", "value": SATS}],
                                [{"address": "B", "value": SATS}]) is None


def test_entropy_single_input_atomic():
    r = transaction_entropy([SATS], [SATS // 2, SATS // 2])
    assert r["atomic"] is True
    assert r["entropy_bits"] == 0.0


def test_entropy_ambiguous_split():
    # two inputs of 1 and 2, two outputs of 1 and 2 -> a 1<->1 / 2<->2 cut exists
    r = transaction_entropy([1 * SATS, 2 * SATS], [1 * SATS, 2 * SATS])
    assert r["cuts"] >= 1
    assert r["atomic"] is False
    assert r["entropy_bits"] > 0


def test_entropy_no_cut_atomic():
    # sums cannot be partitioned into equal-sum proper subsets
    r = transaction_entropy([3 * SATS], [1 * SATS, 2 * SATS])
    assert r["atomic"] is True


def test_entropy_too_large():
    r = transaction_entropy(list(range(1, 20)), list(range(1, 20)))
    assert r["cuts"] is None


def test_shape_coinjoin():
    ins = [SATS] * 5
    outs = [SATS] * 5
    flags = {f["flag"] for f in classify_shape(ins, outs)}
    assert "coinjoin" in flags


def test_shape_consolidation():
    flags = {f["flag"] for f in classify_shape([SATS] * 12, [12 * SATS])}
    assert "consolidation" in flags


def test_shape_fan_out():
    flags = {f["flag"] for f in classify_shape([100 * SATS], [SATS] * 12)}
    assert "fan_out" in flags


def test_shape_peel_chain():
    flags = {f["flag"] for f in classify_shape([100 * SATS], [95 * SATS, 5 * SATS])}
    assert "peel_chain_step" in flags
