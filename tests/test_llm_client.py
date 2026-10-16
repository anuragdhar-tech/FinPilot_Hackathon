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


def test_missing_env_file_message(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="no .env found, copy .env.example"):
        client.get_model("advisor")


def test_complete_returns_usage(monkeypatch):
    from types import SimpleNamespace

    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))],
        model="gpt-4o-mini-2024-07-18",
        usage=SimpleNamespace(prompt_tokens=86, completion_tokens=14, fallback_triggered=False),
    )
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp)))
    monkeypatch.setattr(client, "get_client", lambda: fake)
    monkeypatch.setenv("CATEGORIZER_MODEL", "openai/gpt-4o-mini")
    out = client.complete("categorizer", [{"role": "user", "content": "ping"}])
    assert (out.text, out.prompt_tokens, out.completion_tokens) == ("pong", 86, 14)
    assert out.model == "gpt-4o-mini-2024-07-18" and out.fallback_triggered is False


def test_client_has_a_timeout_and_few_retries(monkeypatch):
    monkeypatch.setenv("MESH_API_KEY", "test-key")
    client.get_client.cache_clear()
    c = client.get_client()
    assert c.timeout == client.TIMEOUT_S == 60 and c.max_retries == client.MAX_RETRIES <= 2
    client.get_client.cache_clear()


def test_timeout_becomes_llm_unavailable(monkeypatch):
    import httpx
    import openai
    from types import SimpleNamespace

    def create(**kw):
        raise openai.APITimeoutError(request=httpx.Request("POST", client.MESH_BASE_URL))
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    monkeypatch.setenv("ADVISOR_MODEL", "openai/gpt-4o")
    with pytest.raises(client.LLMUnavailable, match="advisor LLM call failed after"):
        client.complete("advisor", [{"role": "user", "content": "ping"}])
