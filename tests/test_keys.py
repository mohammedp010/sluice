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


def test_pre_phase8_database_is_migrated(tmp_path):
    import sqlite3

    from sluice.auth.keys import KeyStore, hash_key
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE keys (id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_hash TEXT UNIQUE NOT NULL, key_hint TEXT NOT NULL, name TEXT NOT NULL,
        team TEXT NOT NULL DEFAULT '', app TEXT NOT NULL DEFAULT '',
        allowed_models TEXT NOT NULL DEFAULT '["*"]', rpm INTEGER NOT NULL, tpm INTEGER NOT NULL,
        daily_budget_usd REAL, monthly_budget_usd REAL, revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("INSERT INTO keys (key_hash, key_hint, name, rpm, tpm) VALUES (?, 'abcd', 'old', 5, 50)",
                (hash_key("sk-sluice-old"),))
    con.commit()
    con.close()
    store = KeyStore(str(db))
    old = store.lookup("sk-sluice-old")
    assert old.name == "old" and old.allowed_tools == [] and not old.allows_tool("x__y")
    _, new = store.create("new", allowed_tools=["files__read_*"])
    assert new.allows_tool("files__read_file") and not new.allows_tool("files__write_file")
