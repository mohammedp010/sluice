from sluice.auth.keys import PREFIX, KeyStore, hash_key


def test_create_lookup_and_raw_key_not_stored():
    s = KeyStore(":memory:")
    raw, k = s.create("demo", team="t", rpm=5)
    assert raw.startswith(PREFIX) and s.lookup(raw).id == k.id and k.rpm == 5
    dump = " ".join(str(tuple(r)) for r in s._db.execute("SELECT * FROM keys"))
    assert raw not in dump and hash_key(raw) in dump


def test_unknown_and_revoked_keys_rejected():
    s = KeyStore(":memory:")
    raw, k = s.create("demo")
    assert s.lookup("sk-sluice-wrong") is None
    s.revoke(k.id)
    assert s.lookup(raw) is None


def test_model_allowlist():
    s = KeyStore(":memory:")
    _, k = s.create("a", allowed_models=["m1"])
    _, any_ = s.create("b")
    assert k.allows("m1") and not k.allows("m2") and any_.allows("whatever")
