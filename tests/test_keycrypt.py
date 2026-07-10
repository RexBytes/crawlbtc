import pytest

from crawlbtc.keycrypt import decrypt, encrypt, is_encrypted


def test_roundtrip():
    secret = "L1aW4aubDFB7yfras2S1mHfm1eybxYtWCQaRPu2vg3AswT5Qwvdg"  # example WIF
    tok = encrypt(secret, "correct horse battery staple")
    assert is_encrypted(tok)
    assert tok != secret
    assert decrypt(tok, "correct horse battery staple") == secret


def test_wrong_passphrase_rejected():
    tok = encrypt("hunter2", "right")
    with pytest.raises(ValueError):
        decrypt(tok, "wrong")


def test_tamper_detected():
    tok = encrypt("hunter2", "pw")
    parts = tok.split(":")
    # flip a byte in the ciphertext section
    import base64
    ct = bytearray(base64.b64decode(parts[3]))
    ct[0] ^= 0x01
    parts[3] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(ValueError):
        decrypt(":".join(parts), "pw")


def test_unique_tokens_per_call():
    a = encrypt("same", "pw")
    b = encrypt("same", "pw")
    assert a != b                      # random salt+nonce
    assert decrypt(a, "pw") == decrypt(b, "pw") == "same"


def test_is_encrypted_plain():
    assert not is_encrypted("L1aW4aubDFB7yfras2S1mHfm1eyb")
    assert not is_encrypted(None)


def test_unicode_secret():
    s = "clé-privée-🔑"
    assert decrypt(encrypt(s, "pw"), "pw") == s
