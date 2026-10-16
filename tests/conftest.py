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


@pytest.fixture(autouse=True)
def no_real_advisor_or_whatif_llm(monkeypatch):
    """Graph runs in tests never reach Mesh: the Advisor drafts nothing unless a test scripts it."""
    import json

    import backend.graph as graph
    from backend.llm.client import Completion

    def empty_advisor(messages, **kwargs):
        return Completion(json.dumps({"recommendations": []}), "fake-advisor", 10, 5, False)

    def forbidden(messages, **kwargs):
        raise AssertionError("real LLM call in a test; pass a fake")

    monkeypatch.setattr(graph, "default_advisor_llm", empty_advisor)
    monkeypatch.setattr(graph, "default_parser_llm", forbidden)
    monkeypatch.setattr(graph, "default_explainer_llm", forbidden)
