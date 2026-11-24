import logging
import sys

import pytest

from selfbot import logging_setup
from selfbot.secrets import SecretsError, TokenStore, decrypt, encrypt, generate_master_key
from selfbot.storage.db import Database

from conftest import FAKE_TOKEN_A, FAKE_TOKEN_B


def test_fernet_roundtrip():
    key = generate_master_key()
    blob = encrypt("secret", backend="fernet", key=key)
    assert "secret" not in blob
    assert decrypt(blob, key=key) == "secret"
    with pytest.raises(SecretsError):
        decrypt(blob, key=generate_master_key())


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI = Windows")
def test_dpapi_roundtrip():
    blob = encrypt("secret", backend="dpapi")
    assert blob.startswith("dpapi:") and "secret" not in blob
    assert decrypt(blob) == "secret"


def test_token_store(tmp_path):
    key = generate_master_key()
    store = TokenStore(tmp_path / "t.json", backend="fernet", key=key)
    store.set("a", FAKE_TOKEN_A)
    store.set("b", FAKE_TOKEN_B)
    assert store.ids() == ["a", "b"]
    assert store.get("a") == FAKE_TOKEN_A
    assert store.find_duplicate(FAKE_TOKEN_B) == "b"
    assert store.find_duplicate("zzz") is None
    assert FAKE_TOKEN_A not in (tmp_path / "t.json").read_text()
    assert store.delete("a") and not store.delete("a")


def test_log_redaction(tmp_path):
    logger = logging_setup.setup_logging("t_redact", tmp_path, console=False, secrets=["supersecretvalue"])
    logger.info("token %s proxy http://user:pw@host:1/ auth supersecretvalue", FAKE_TOKEN_A)
    logger.info("headers {'authorization': 'abcdef123456'}")
    for h in logger.handlers:
        h.flush()
    text = (tmp_path / "t_redact.log").read_text()
    assert FAKE_TOKEN_A not in text and "supersecretvalue" not in text
    assert "user:pw" not in text and "abcdef123456" not in text
    logging.shutdown()


def test_db_sets_kv_audit(tmp_path):
    db = Database(tmp_path / "x.db")
    db.replace_set("stocks", [1, 2, 3])
    db.add_to_set("stocks", 4)
    db.remove_from_set("stocks", 1)
    assert db.get_set("stocks") == {2, 3, 4}
    db.set("keywords", ["a", "b"])
    assert db.get("keywords") == ["a", "b"] and db.get("nope", 5) == 5
    db.audit("42", "start", "x")
    assert db.audit_log(1)[0][1:3] == ("42", "start")
    f = tmp_path / "old.txt"
    f.write_text("10\n11\nbad\n")
    assert db.import_txt("old", f) == 2 and f.exists()
    db.close()
