"""`crawlbtc encrypt-keys` / `decrypt-keys` - protect watch_addresses secrets.

Removes plaintext private keys from the database by encrypting the
private_key_wif / private_key_hex columns in place under a passphrase. This is
app-level and stdlib-only (no new dependency): scrypt derives the key, an
HMAC-SHA256 keystream in counter mode encrypts, and an encrypt-then-MAC HMAC
authenticates. Tokens are self-describing and versioned:

    cbtc1:<b64 salt>:<b64 nonce>:<b64 ciphertext>:<b64 tag>

The passphrase is never stored. Losing it means losing the keys - that is the
point (a stolen DB dump reveals nothing), but back the passphrase up somewhere
safe. Encryption is idempotent (already-encrypted values are skipped), so the
command is safe to re-run as new watch addresses are added.
"""

import base64
import getpass
import hashlib
import hmac
import os
import sys

import psycopg

_PREFIX = "cbtc1"
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _b64e(b):
    return base64.b64encode(b).decode()


def _b64d(s):
    return base64.b64decode(s)


def _derive(passphrase, salt):
    dk = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, **_SCRYPT)
    return dk[:32], dk[32:]          # enc_key, mac_key


def _keystream(enc_key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(_PREFIX + ":")


def encrypt(plaintext, passphrase):
    """Return a self-describing token for a plaintext string."""
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = _derive(passphrase, salt)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(enc_key, nonce, len(pt))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return f"{_PREFIX}:{_b64e(salt)}:{_b64e(nonce)}:{_b64e(ct)}:{_b64e(tag)}"


def decrypt(token, passphrase):
    """Recover the plaintext; raises ValueError on a wrong passphrase/tamper."""
    parts = token.split(":")
    if len(parts) != 5 or parts[0] != _PREFIX:
        raise ValueError("not a crawlbtc key token")
    _, salt_b, nonce_b, ct_b, tag_b = parts
    salt, nonce, ct, tag = _b64d(salt_b), _b64d(nonce_b), _b64d(ct_b), _b64d(tag_b)
    enc_key, mac_key = _derive(passphrase, salt)
    expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("wrong passphrase or corrupted token")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    return pt.decode("utf-8")


_KEY_COLUMNS = ("private_key_wif", "private_key_hex")


def _get_passphrase(args, confirm=False):
    p = getattr(args, "passphrase", None) or os.getenv("CRAWLBTC_KEY_PASSPHRASE")
    if p:
        return p
    if not sys.stdin.isatty():
        print("no passphrase: set CRAWLBTC_KEY_PASSPHRASE or pass --passphrase.",
              file=sys.stderr)
        sys.exit(2)
    p = getpass.getpass("key passphrase: ")
    if confirm and getpass.getpass("confirm passphrase: ") != p:
        print("passphrases did not match.", file=sys.stderr)
        sys.exit(1)
    if not p:
        print("empty passphrase refused.", file=sys.stderr)
        sys.exit(1)
    return p


def cmd_encrypt_keys(args, cfg):
    passphrase = _get_passphrase(args, confirm=True)
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cols = ", ".join(_KEY_COLUMNS)
        cur.execute(f"""
            SELECT address, {cols} FROM blockchain.watch_addresses
             WHERE private_key_wif IS NOT NULL OR private_key_hex IS NOT NULL;
        """)
        rows = cur.fetchall()
        changed = skipped = 0
        for address, *vals in rows:
            updates, params = [], []
            for col, v in zip(_KEY_COLUMNS, vals):
                if v is None or is_encrypted(v):
                    continue
                updates.append(f"{col} = %s")
                params.append(encrypt(v, passphrase))
            if not updates:
                skipped += 1
                continue
            params.append(address)
            cur.execute(f"UPDATE blockchain.watch_addresses SET {', '.join(updates)} "
                        f"WHERE address = %s;", params)
            changed += 1
        print(f"encrypted keys for {changed} address(es); {skipped} already encrypted/empty.")
        if changed:
            print("keep the passphrase safe - it is the only way to recover these keys.")


def cmd_decrypt_keys(args, cfg):
    passphrase = _get_passphrase(args)
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cols = ", ".join(_KEY_COLUMNS)
        if args.address:
            cur.execute(f"SELECT address, {cols} FROM blockchain.watch_addresses "
                        f"WHERE address = %s;", (args.address,))
        else:
            cur.execute(f"SELECT address, {cols} FROM blockchain.watch_addresses "
                        f"WHERE private_key_wif IS NOT NULL OR private_key_hex IS NOT NULL;")
        rows = cur.fetchall()
        if not rows:
            print("no matching watch addresses.", file=sys.stderr)
            return
        if args.write_back:
            _decrypt_write_back(cur, rows, passphrase)
            return
        # default: print recovered keys, do NOT put plaintext back in the DB
        for address, *vals in rows:
            printed = False
            for col, v in zip(_KEY_COLUMNS, vals):
                if v is None:
                    continue
                try:
                    plain = decrypt(v, passphrase) if is_encrypted(v) else v
                except ValueError as e:
                    print(f"{address} {col}: {e}", file=sys.stderr)
                    sys.exit(1)
                print(f"{address}  {col}  {plain}")
                printed = True
            if not printed:
                print(f"{address}  (no keys)")


def _decrypt_write_back(cur, rows, passphrase):
    changed = 0
    for address, *vals in rows:
        updates, params = [], []
        for col, v in zip(_KEY_COLUMNS, vals):
            if not is_encrypted(v):
                continue
            try:
                updates.append(f"{col} = %s")
                params.append(decrypt(v, passphrase))
            except ValueError as e:
                print(f"{address} {col}: {e}", file=sys.stderr)
                sys.exit(1)
        if updates:
            params.append(address)
            cur.execute(f"UPDATE blockchain.watch_addresses SET {', '.join(updates)} "
                        f"WHERE address = %s;", params)
            changed += 1
    print(f"decrypted keys back to plaintext for {changed} address(es). "
          f"the database now holds plaintext secrets again.")
