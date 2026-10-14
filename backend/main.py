from fastapi import FastAPI

app = FastAPI(title="FinPilot")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
