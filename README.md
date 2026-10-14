# FinPilot

A proactive financial agent that forecasts, simulates, and advises, and checks its own advice. Built for Agent Arena 2026 (Track 02). Multi-agent LangGraph backend, every LLM call routed through Mesh API, with measured accuracy and an audit trail for every recommendation.

Full scope and API contract: [PLAN.md](PLAN.md).

## How to run

**Backend**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in MESH_API_KEY and the *_MODEL values
uvicorn backend.main:app --reload --port 8000
python scripts/mesh_smoke_test.py   # verify the Mesh connection
pytest
```

**Frontend**

```bash
cd frontend
npm install
cp .env.example .env.local  # VITE_USE_MOCKS=true for mocked API, false for the real backend
npm run dev
```

## Agents

_TODO: Categorizer, Analyzer, Forecaster, Advisor, Verifier, Coordinator, and how they cooperate._

## Design decisions

_TODO._

## Eval results

_TODO: numbers are produced by scripts into `eval/results/`, never written by hand._
