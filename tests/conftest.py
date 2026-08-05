from __future__ import annotations
import pytest

from wrkmatch.db import init_db


@pytest.fixture
def db_conn(tmp_path):
    """Fresh sqlite3 connection backed by a per-test tmp file, with schema applied."""
    conn = init_db(tmp_path / "wrkmatch_test.db")
    yield conn
    conn.close()
