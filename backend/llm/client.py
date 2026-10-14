"""The single place an LLM client is created. Everything goes through Mesh API."""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

MESH_BASE_URL = "https://api.meshapi.ai/v1"

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

load_dotenv(ENV_PATH)


def _missing(var: str) -> RuntimeError:
    if not ENV_PATH.exists():
        return RuntimeError("no .env found, copy .env.example to .env and fill it in")
    return RuntimeError(f"{var} is empty or missing in .env (see .env.example)")


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.environ.get("MESH_API_KEY")
    if not api_key:
        raise _missing("MESH_API_KEY")
    return OpenAI(base_url=MESH_BASE_URL, api_key=api_key)


def get_model(agent: str) -> str:
    """Model for an agent, from env: CATEGORIZER_MODEL, ANALYZER_MODEL, ADVISOR_MODEL, PARSER_MODEL."""
    var = f"{agent.upper()}_MODEL"
    model = os.environ.get(var)
    if not model:
        raise _missing(var)
    return model


def chat(agent: str, messages: list[dict], **kwargs) -> str:
    """One chat completion for the given agent; returns the text."""
    resp = get_client().chat.completions.create(
        model=get_model(agent), messages=messages, **kwargs
    )
    return resp.choices[0].message.content or ""
