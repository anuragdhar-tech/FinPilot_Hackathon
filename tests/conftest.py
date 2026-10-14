import pytest
from fastapi.testclient import TestClient

from backend.db import connect, init_db


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient on a throwaway app DB (FINPILOT_DB)."""
    monkeypatch.setenv("FINPILOT_DB", str(tmp_path / "app.db"))
    from backend.main import app

    with TestClient(app) as c:
        yield c
