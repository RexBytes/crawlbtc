"""Address extraction and classification, including Satoshi-era P2PK outputs.

Modern Bitcoin Core omits the `address` field for raw-pubkey (P2PK) outputs,
so the legacy pipeline silently dropped them. Every P2PK output has a
canonical P2PKH form (base58check of HASH160(pubkey)) - the "1..." address
that block explorers display for those coins - so we derive it here and the
coins land in transaction_io under that address.
"""

import hashlib
import re
from decimal import ROUND_DOWN, Decimal
from typing import Optional, Tuple

TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
HEX64_RE = TXID_RE
SATS_PER_BTC = Decimal(100_000_000)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def validate_txid(txid) -> bool:
    return isinstance(txid, str) and TXID_RE.match(txid) is not None


def to_sats(value) -> int:
    """Robust BTC -> sats conversion: floors, rejects NaN/Inf, clamps negatives to 0."""
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite value")
    if d <= 0:
        return 0
    return int((d * SATS_PER_BTC).to_integral_value(rounding=ROUND_DOWN))


# --- RIPEMD-160 (pure-Python fallback: OpenSSL 3 builds often drop it) ---

def _ripemd160_py(data: bytes) -> bytes:
    # RIPEMD-160 per the original spec (Dobbertin, Bosselaers, Preneel).
    r1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
          7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
          1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    r2 = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
          6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
          8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    s1 = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
          7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
          11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    s2 = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
          9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
          15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    k1 = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    k2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]

    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def f(j, x, y, z):
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = bytearray(data)
    bitlen = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += bitlen.to_bytes(8, "little")

    for off in range(0, len(msg), 64):
        x = [int.from_bytes(msg[off + 4 * i: off + 4 * i + 4], "little") for i in range(16)]
        a1, b1, c1, d1, e1 = h
        a2, b2, c2, d2, e2 = h
        for j in range(80):
            t = (rol((a1 + f(j, b1, c1, d1) + x[r1[j]] + k1[j // 16]) & 0xFFFFFFFF, s1[j]) + e1) & 0xFFFFFFFF
            a1, e1, d1, c1, b1 = e1, d1, rol(c1, 10), b1, t
            t = (rol((a2 + f(79 - j, b2, c2, d2) + x[r2[j]] + k2[j // 16]) & 0xFFFFFFFF, s2[j]) + e2) & 0xFFFFFFFF
            a2, e2, d2, c2, b2 = e2, d2, rol(c2, 10), b2, t
        t = (h[1] + c1 + d2) & 0xFFFFFFFF
        h = [t,
             (h[2] + d1 + e2) & 0xFFFFFFFF,
             (h[3] + e1 + a2) & 0xFFFFFFFF,
             (h[4] + a1 + b2) & 0xFFFFFFFF,
             (h[0] + b1 + c2) & 0xFFFFFFFF]
    return b"".join(v.to_bytes(4, "little") for v in h)


try:
    hashlib.new("ripemd160")

    def _ripemd160(data: bytes) -> bytes:
        return hashlib.new("ripemd160", data).digest()
except (ValueError, TypeError):
    _ripemd160 = _ripemd160_py


def hash160(data: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(data).digest())


def base58check_encode(version: int, payload: bytes) -> str:
    raw = bytes([version]) + payload
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    raw += checksum
    n = int.from_bytes(raw, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    # preserve leading zero bytes as '1's
    for b in raw:
        if b == 0:
            out.append("1")
        else:
            break
    return "".join(reversed(out))


_PUBKEY_HEX_RE = re.compile(r"^(02|03)[0-9a-fA-F]{64}$|^04[0-9a-fA-F]{128}$")


def pubkey_to_p2pkh_address(pubkey_hex: str) -> Optional[str]:
    """Derive the canonical base58 '1...' address from a hex public key."""
    if not isinstance(pubkey_hex, str) or not _PUBKEY_HEX_RE.match(pubkey_hex):
        return None
    return base58check_encode(0x00, hash160(bytes.fromhex(pubkey_hex)))


def pubkey_from_p2pk_script(script_hex: str) -> Optional[str]:
    """Extract the pubkey from a raw P2PK scriptPubKey: PUSH(33|65) <pubkey> OP_CHECKSIG."""
    if not isinstance(script_hex, str):
        return None
    s = script_hex.lower()
    if len(s) == 2 * 67 and s.startswith("41") and s.endswith("ac"):
        return s[2:-2]
    if len(s) == 2 * 35 and s.startswith("21") and s.endswith("ac"):
        return s[2:-2]
    return None


def get_address_type(address: Optional[str]) -> Optional[str]:
    if not address or not isinstance(address, str):
        return None
    if address.startswith("1"):
        return "p2pkh"
    if address.startswith("3"):
        return "p2sh"
    if address.startswith("bc1p"):
        return "taproot"
    if address.startswith("bc1q"):
        return "bech32"
    return "unknown"


_SCRIPT_TYPE_MAP = {
    "pubkeyhash": "p2pkh",
    "scripthash": "p2sh",
    "witness_v0_keyhash": "bech32",
    "witness_v0_scripthash": "bech32",
    "witness_v1_taproot": "taproot",
}


def normalize_address_type(script_type: Optional[str], address: Optional[str]) -> str:
    if script_type in _SCRIPT_TYPE_MAP:
        return _SCRIPT_TYPE_MAP[script_type]
    return get_address_type(address) or "unknown"


def extract_output_address(spk: Optional[dict], p2pk_type_label: str = "p2pk") -> Tuple[Optional[str], Optional[str]]:
    """Resolve (address, address_type) from a scriptPubKey dict.

    Handles the modern `address` field, the legacy `addresses` list, and
    derives P2PKH-form addresses for raw-pubkey (P2PK) scripts that Core
    reports without an address.
    """
    spk = spk or {}
    address = spk.get("address")
    if not isinstance(address, str):
        addrs = spk.get("addresses")
        address = addrs[0] if isinstance(addrs, list) and addrs and isinstance(addrs[0], str) else None

    script_type = spk.get("type")
    if isinstance(address, str):
        # Old Core versions reported a derived address for P2PK too.
        if script_type == "pubkey":
            return address, p2pk_type_label
        return address, normalize_address_type(script_type, address)

    if script_type == "pubkey":
        pubkey = pubkey_from_p2pk_script(spk.get("hex", ""))
        if pubkey is None:
            # asm form: "<pubkey> OP_CHECKSIG"
            asm = spk.get("asm", "")
            if isinstance(asm, str) and asm:
                pubkey = asm.split()[0]
        derived = pubkey_to_p2pkh_address(pubkey) if pubkey else None
        if derived:
            return derived, p2pk_type_label

    return None, None
