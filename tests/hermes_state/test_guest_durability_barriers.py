"""Guest ledger connections must inherit the configured database.synchronous.

apply_durability_barriers() is the guest-connection entry point (secondary
state.db users that must NOT touch journal mode). The configured
``database.synchronous`` level normally rides on apply_database_pragmas()
during the owner's journal-mode setup — a path guests deliberately skip — so
the guest entry point applies it directly.
"""

import logging
import sqlite3

import pytest

import hermes_state
from hermes_state_repair import apply_durability_barriers


def _config(monkeypatch, database_section):
    import hermes_cli.config as config_mod

    cfg = {"database": database_section}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda *a, **k: cfg)
    return cfg


def test_guest_barriers_apply_configured_synchronous(monkeypatch, tmp_path):
    _config(monkeypatch, {"synchronous": "FULL"})
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=1")
        apply_durability_barriers(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        # And the journal mode was NOT touched — that is the whole contract.
        assert (
            str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            == "delete"
        )
    finally:
        conn.close()


def test_guest_barriers_leave_synchronous_alone_when_unset(monkeypatch, tmp_path):
    _config(monkeypatch, {})
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=1")
        apply_durability_barriers(conn)
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_guest_barriers_survive_config_failure(monkeypatch, tmp_path):
    import hermes_cli.config as config_mod

    def _boom(*a, **k):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        # Must not raise; best-effort like every other pragma path.
        apply_durability_barriers(conn)
    finally:
        conn.close()


def _guest_synchronous(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        apply_durability_barriers(conn)
        return conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()


def test_guest_opens_under_an_empty_scope_expand_the_config_once(monkeypatch, tmp_path, caplog):
    """A multiplexed ledger sweep opens state.db under an empty secret scope, between ordinary
    scoped loads of the same profile's config. A full load per guest open re-expanded every
    ``${env:}`` ref there and logged one placeholder warning per ref and open (#148624)."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.config import get_config_path, load_config_readonly

    monkeypatch.delenv("GUEST_TEST_KEY", raising=False)
    get_config_path().write_text(
        "database:\n  synchronous: FULL\n"
        "mcp_servers:\n  x:\n    url: http://x\n"
        "    headers:\n      Authorization: Bearer ${env:GUEST_TEST_KEY}\n")
    caplog.set_level(logging.WARNING, logger="hermes_cli.config")
    for _ in range(5):
        token = set_secret_scope({"GUEST_TEST_KEY": "own-value"})
        try:
            load_config_readonly()
        finally:
            reset_secret_scope(token)
        token = set_secret_scope({})
        try:
            assert _guest_synchronous(tmp_path / "state.db") == 2
        finally:
            reset_secret_scope(token)
    misses = [r for r in caplog.records if "GUEST_TEST_KEY is not set" in r.getMessage()]
    assert len(misses) == 1


def test_guest_synchronous_follows_a_config_edit(tmp_path):
    from hermes_cli.config import get_config_path

    config = get_config_path()
    config.write_text("database:\n  synchronous: FULL\n")
    assert _guest_synchronous(tmp_path / "state.db") == 2
    config.write_text("database:\n  synchronous: EXTRA\n")
    assert _guest_synchronous(tmp_path / "state.db") == 3
