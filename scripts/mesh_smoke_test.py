"""Tiny chat completion through backend/llm/client.py.

Usage: python scripts/mesh_smoke_test.py [agent]   (agent defaults to advisor)

On any failure, prints the full error and exits non-zero. It does not retry with
other model names or providers.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm.client import MESH_BASE_URL, chat, get_model  # noqa: E402


def main() -> int:
    agent = sys.argv[1] if len(sys.argv) > 1 else "advisor"
    try:
        model = get_model(agent)
        print(f"base_url: {MESH_BASE_URL}")
        print(f"model:    {model}")
        text = chat(agent, [{"role": "user", "content": "Reply with exactly: pong"}], max_tokens=10)
        print(f"response: {text!r}")
        return 0
    except Exception:
        print("SMOKE TEST FAILED. Full error:", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
