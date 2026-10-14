"""The single place an LLM client is created. Everything goes through Mesh API."""
import os
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI

MESH_BASE_URL = "https://api.meshapi.ai/v1"

load_dotenv()


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    api_key = os.environ.get("MESH_API_KEY")
    if not api_key:
        raise RuntimeError("MESH_API_KEY is not set (see .env.example)")
    return OpenAI(base_url=MESH_BASE_URL, api_key=api_key)


def get_model(agent: str) -> str:
    """Model for an agent, from env: CATEGORIZER_MODEL, ANALYZER_MODEL, ADVISOR_MODEL, PARSER_MODEL."""
    var = f"{agent.upper()}_MODEL"
    model = os.environ.get(var)
    if not model:
        raise RuntimeError(f"{var} is not set (see .env.example)")
    return model


def chat(agent: str, messages: list[dict], **kwargs) -> str:
    """One chat completion for the given agent; returns the text."""
    resp = get_client().chat.completions.create(
        model=get_model(agent), messages=messages, **kwargs
    )
    return resp.choices[0].message.content or ""
