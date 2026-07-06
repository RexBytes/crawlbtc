import hashlib

import pytest

from crawlbtc.core.addresses import (
    _ripemd160_py,
    base58check_encode,
    extract_output_address,
    get_address_type,
    normalize_address_type,
    pubkey_from_p2pk_script,
    pubkey_to_p2pkh_address,
    to_sats,
    validate_txid,
)

GENESIS_PK = ("04678afdb0fe5548271967f1a67130b7105cd6a828e03909a67962e0ea1f61de"
              "b649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5f")
BLOCK1_PK = ("0496b538e853519c726a2c91e61ec11600ae1390813a627c66fb8be7947be63c"
             "52da7589379515d4e0a604f8141781e62294721166bf621e73a82cbf2342c858ee")


def test_ripemd160_vectors():
    assert _ripemd160_py(b"").hex() == "9c1185a5c5e9fc54612808977ee8f548b2258d31"
    assert _ripemd160_py(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    assert (_ripemd160_py(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq").hex()
            == "12a053384a9c0c88e405a06c27dcf49ada62eb2b")


def test_ripemd160_matches_openssl_when_available():
    try:
        ref = hashlib.new("ripemd160", b"x" * 500).digest()
    except ValueError:
        pytest.skip("openssl build lacks ripemd160")
    assert _ripemd160_py(b"x" * 500) == ref


def test_satoshi_era_p2pk_addresses():
    assert pubkey_to_p2pkh_address(GENESIS_PK) == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    assert pubkey_to_p2pkh_address(BLOCK1_PK) == "12c6DSiU4Rq3P4ZxziKxzrL5LmMBrzjrJX"


def test_pubkey_rejects_garbage():
    assert pubkey_to_p2pkh_address("zz") is None
    assert pubkey_to_p2pkh_address("") is None
    assert pubkey_to_p2pkh_address(None) is None
    assert pubkey_to_p2pkh_address("05" + "00" * 64) is None  # bad prefix


def test_pubkey_from_p2pk_script():
    assert pubkey_from_p2pk_script("41" + GENESIS_PK + "ac") == GENESIS_PK
    compressed = "02" + "11" * 32
    assert pubkey_from_p2pk_script("21" + compressed + "ac") == compressed
    assert pubkey_from_p2pk_script("76a914aa88ac") is None  # p2pkh script


def test_base58_leading_zeros():
    # HASH160 payloads starting with zero bytes must keep their '1' prefix chars
    assert base58check_encode(0x00, bytes(20)) == "1111111111111111111114oLvT2"


def test_extract_output_address_p2pk_derivation():
    spk = {"type": "pubkey", "hex": "41" + GENESIS_PK + "ac",
           "asm": f"{GENESIS_PK} OP_CHECKSIG"}
    addr, addr_type = extract_output_address(spk)
    assert addr == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    assert addr_type == "p2pk"
    # fallback label when the db enum lacks 'p2pk'
    addr, addr_type = extract_output_address(spk, p2pk_type_label="p2pkh")
    assert addr_type == "p2pkh"


def test_extract_output_address_modern_types():
    assert extract_output_address(
        {"type": "witness_v0_keyhash", "address": "bc1qxyz"}) == ("bc1qxyz", "bech32")
    assert extract_output_address(
        {"type": "witness_v1_taproot", "address": "bc1pxyz"}) == ("bc1pxyz", "taproot")
    assert extract_output_address(
        {"type": "pubkeyhash", "addresses": ["1abc"]}) == ("1abc", "p2pkh")
    assert extract_output_address({"type": "nulldata"}) == (None, None)
    assert extract_output_address({"type": "multisig"}) == (None, None)
    assert extract_output_address(None) == (None, None)


def test_to_sats():
    assert to_sats(50.0) == 5_000_000_000
    assert to_sats("0.00000001") == 1
    assert to_sats(0) == 0
    assert to_sats(-1) == 0
    assert to_sats(20.99999999) == 2_099_999_999
    with pytest.raises(ValueError):
        to_sats(float("nan"))


def test_validate_txid():
    assert validate_txid("a" * 64)
    assert not validate_txid("a" * 63)
    assert not validate_txid(None)
    assert not validate_txid("g" * 64)


def test_normalize_address_type():
    assert normalize_address_type("pubkeyhash", None) == "p2pkh"
    assert normalize_address_type("witness_v1_taproot", None) == "taproot"
    assert normalize_address_type(None, "3abc") == "p2sh"
    assert normalize_address_type(None, None) == "unknown"
    assert get_address_type("bc1qxyz") == "bech32"
