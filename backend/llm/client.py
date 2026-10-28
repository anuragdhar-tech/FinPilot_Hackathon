"""The single place an LLM client is created. Everything goes through Mesh API."""
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, OpenAI

MESH_BASE_URL = "https://api.meshapi.ai/v1"
TIMEOUT_S = 60  # per request; a hung call once left a run in 'running' for hours
MAX_RETRIES = 2  # SDK retries on timeouts, connection errors, 429 and 5xx

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
    return OpenAI(base_url=MESH_BASE_URL, api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES)


def get_model(agent: str) -> str:
    """Model for an agent, from env: CATEGORIZER_MODEL, ADVISOR_MODEL, PARSER_MODEL."""
    var = f"{agent.upper()}_MODEL"
    model = os.environ.get(var)
    if not model:
        raise _missing(var)
    return model


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass(frozen=True)
class Completion:
    text: str
    model: str  # model that actually answered (Mesh may report a dated snapshot)
    prompt_tokens: int
    completion_tokens: int
    fallback_triggered: bool  # Mesh-specific usage field; True means Mesh swapped models
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)  # when called with tools=


class LLMUnavailable(RuntimeError):
    """Mesh could not be reached or timed out after the SDK's retries; callers should fail, not retry again."""


def complete(agent: str, messages: list[dict], **kwargs) -> Completion:
    """One chat completion for the given agent, with token usage for agent_events and eval cost."""
    try:
        resp = get_client().chat.completions.create(
            model=get_model(agent), messages=messages, **kwargs
        )
    except APIConnectionError as e:  # includes APITimeoutError
        raise LLMUnavailable(f"{agent} LLM call failed after {MAX_RETRIES} retries "
                             f"({TIMEOUT_S} s timeout each): {e}") from e
    usage = resp.usage
    msg = resp.choices[0].message
    return Completion(
        text=msg.content or "",
        model=resp.model or get_model(agent),
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        fallback_triggered=bool(getattr(usage, "fallback_triggered", False)),
        tool_calls=tuple(ToolCall(c.id, c.function.name, c.function.arguments)
                         for c in getattr(msg, "tool_calls", None) or ()),
    )


def chat(agent: str, messages: list[dict], **kwargs) -> str:
    """One chat completion for the given agent; returns the text."""
    return complete(agent, messages, **kwargs).text
