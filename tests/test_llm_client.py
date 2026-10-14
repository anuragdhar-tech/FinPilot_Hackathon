import pytest

from backend.llm import client


def test_base_url_is_mesh():
    assert client.MESH_BASE_URL == "https://api.meshapi.ai/v1"


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("MESH_API_KEY", raising=False)
    client.get_client.cache_clear()
    with pytest.raises(RuntimeError):
        client.get_client()


def test_client_uses_mesh(monkeypatch):
    monkeypatch.setenv("MESH_API_KEY", "test-key")
    client.get_client.cache_clear()
    assert str(client.get_client().base_url).startswith("https://api.meshapi.ai/v1")
    client.get_client.cache_clear()


def test_missing_model_raises(monkeypatch):
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    with pytest.raises(RuntimeError):
        client.get_model("advisor")
