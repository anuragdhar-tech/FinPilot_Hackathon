"""Analyzer + Forecaster eval on the planted synthetic data (ground truth is read only here, in eval code).

    python -m eval.analysis [--dataset data/synthetic] [--reuse-db] [--forecast-version fc-v2]

Default: fresh data/eval.db, real product path per persona (ingest_csv -> LangGraph run: ingest ->
categorize (LLM) -> analyze -> forecast). --reuse-db skips ingest and categorization and reuses the
transactions and categorizations already in data/eval.db (no LLM calls), for iterating on detectors.

Patterns: the Analyzer is called on each persona at its as_of_month and scored per type against the
planted facts. Matching rules:
- recurring: detected series <-> planted series, one-to-one, Jaccard of txn sets >= 0.5
- price_change: matched series, same first date at the new price
- unused: matched series flagged possibly_unused vs planted unused
- spike / duplicate / new_merchant: flagged txn is in a planted anomaly of the same kind
- trend: same category and direction
A real but unplanted finding counts as a false positive; every FP and FN is listed.

Forecast: rolling-origin backtest. For every month with >= 4 months of history and a following month,
the Analyzer and Forecaster run with as_of = that month and predict next-month spend; compared with
last month's spend and the 3-month trailing mean. Per-category actuals use the app's own (LLM) labels.

Results: eval/results/patterns.{json,md}, forecast.{json,md} (latest) and forecast_<version>.{json,md}
(kept per forecast method version, so earlier baselines stay committed). All numbers computed here, never typed.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.agents.analyzer import Detected, analyze
from backend.agents.forecaster import METHOD_VERSION, VARIABLE_MONTHS, VERSIONS, forecast
from backend.db import connect, init_db
from backend.graph import create_run, execute_run
from backend.ingest import ingest_csv
from backend.ledger import add_months, load_txns, month_end, months_between
from eval.categorization import _git_sha

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
DB = ROOT / "data" / "eval.db"
MIN_JACCARD = 0.5
MIN_HISTORY_MONTHS = 4
MAJOR_SHARE = 0.05
PATTERN_TYPES = ("recurring", "price_change", "unused", "spike", "duplicate", "new_merchant", "trend")
ANOMALY_KINDS = ("spike", "duplicate", "new_merchant")


def load_truth(dataset: Path) -> tuple[dict, dict]:
    truth = json.loads((dataset / "ground_truth.json").read_text())
    meta = json.loads((dataset / "personas.json").read_text())
    return truth, meta


# ---- database ----

def prepare_db(dataset: Path, db_path: Path, reuse: bool) -> dict[str, int]:
    """persona -> user_id in the eval DB."""
    truth, meta = load_truth(dataset)
    if not reuse:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    conn = connect(db_path)
    init_db(conn)
    users = {}
    for key in sorted(truth["personas"]):
        if reuse:
            row = conn.execute("SELECT id FROM users WHERE persona = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if row is None:
                raise SystemExit(f"--reuse-db: no user for persona {key} in {db_path}; run without --reuse-db")
            missing = conn.execute("SELECT COUNT(*) FROM transactions t WHERE t.user_id = ? AND NOT EXISTS "
                                   "(SELECT 1 FROM categorizations c WHERE c.txn_id = t.id)", (row["id"],)).fetchone()[0]
            if missing:
                raise SystemExit(f"--reuse-db: {missing} uncategorized transactions for {key}; run without --reuse-db")
            users[key] = row["id"]
        else:
            m = meta[key]
            batch = ingest_csv(conn, m["csv"], (dataset / m["csv"]).read_text(), name=key, persona=key,
                               opening_balance_cents=m["opening_balance_cents"])
            run_id = create_run(conn, batch["batch_id"])
            execute_run(run_id, db_path=str(db_path))
            status = conn.execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
            if status["status"] != "done":
                raise SystemExit(f"run for {key} failed: {status['error']}")
            users[key] = batch["user_id"]
    conn.close()
    return users


# ---- pattern scoring (pure) ----

def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _match_series(det: list[Detected], gt: list[dict], ext: dict[int, str]) -> dict[int, int]:
    """detected index -> planted index, greedy one-to-one by Jaccard."""
    pairs = sorted(((_jaccard({ext[t] for t in d.evidence}, set(g["txn_ids"])), i, j)
                    for i, d in enumerate(det) for j, g in enumerate(gt)), reverse=True)
    out, used = {}, set()
    for jac, i, j in pairs:
        if jac >= MIN_JACCARD and i not in out and j not in used:
            out[i] = j
            used.add(j)
    return out


def score_persona(persona: str, patterns: list[Detected], ext: dict[int, str], truth: dict) -> dict[str, dict]:
    """tp/fp/fn item lists per pattern type for one persona, plus controls."""
    res = {t: {"tp": [], "fp": [], "fn": []} for t in PATTERN_TYPES}
    rec = [p for p in patterns if p.type == "recurring"]
    gt_rec = truth["recurring"]
    match = _match_series(rec, gt_rec, ext)
    matched_gt = set(match.values())

    def item(p: Detected, **kw) -> dict:
        return {"persona": persona, "title": p.title} | kw

    for i, p in enumerate(rec):
        g = gt_rec[match[i]] if i in match else None
        res["recurring"]["tp" if g else "fp"].append(item(p, planted=g["merchant"] if g else None))
        if p.params["price_changed"]:
            ok = g is not None and g["price_change"] and g["price_change"]["effective"] == p.params["price_change_date"]
            res["price_change"]["tp" if ok else "fp"].append(item(p, planted=g["merchant"] if g else None))
        if p.params["possibly_unused"]:
            ok = g is not None and g["unused"] is True
            res["unused"]["tp" if ok else "fp"].append(item(p, planted=g["merchant"] if g else None))
    tp_change = {x["planted"] for x in res["price_change"]["tp"]}
    tp_unused = {x["planted"] for x in res["unused"]["tp"]}
    for j, g in enumerate(gt_rec):
        miss = {"persona": persona, "merchant": g["merchant"], "amount_cents": g["amount_cents"]}
        if j not in matched_gt:
            res["recurring"]["fn"].append(miss)
        if g["price_change"] and g["merchant"] not in tp_change:
            res["price_change"]["fn"].append(miss | {"price_change": g["price_change"]})
        if g["unused"] and g["merchant"] not in tp_unused:
            res["unused"]["fn"].append(miss | {"last_parent_spend": g["last_parent_spend"]})

    controls = []
    for j, g in enumerate(gt_rec):
        if g["membership_of"] and g["unused"] is False:
            i = next((i for i, jj in match.items() if jj == j), None)
            controls.append({"persona": persona, "check": f"used membership {g['merchant']} not flagged unused",
                             "passed": i is None or not rec[i].params["possibly_unused"],
                             "detected": i is not None})

    for kind in ANOMALY_KINDS:
        planted = [a for a in truth["anomalies"] if a["kind"] == kind]
        hit = set()
        for p in patterns:
            if p.type != "anomaly" or p.params["kind"] != kind:
                continue
            j = next((j for j, a in enumerate(planted) if ext[p.params["txn_id"]] in a["txn_ids"] and j not in hit), None)
            if j is None:
                res[kind]["fp"].append(item(p, date=p.params["date"]))
            else:
                hit.add(j)
                res[kind]["tp"].append(item(p, planted=planted[j]["merchant"]))
        res[kind]["fn"] += [{"persona": persona, "merchant": a["merchant"], "date": a["date"],
                             "amount_cents": a["amount_cents"]} for j, a in enumerate(planted) if j not in hit]

    gt_trends = {(t["category"], t["direction"]) for t in truth["trends"]}
    found = set()
    for p in patterns:
        if p.type == "trend":
            key = (p.params["category"], "rising" if p.params["slope_cents_per_month"] > 0 else "falling")
            res["trend"]["tp" if key in gt_trends else "fp"].append(item(p, mk_p=p.params["mk_p"]))
            found.add(key)
    res["trend"]["fn"] += [{"persona": persona, "category": c, "direction": d} for c, d in gt_trends - found]
    if not any(c == "food_delivery" for c, _ in gt_trends):
        controls.append({"persona": persona, "check": "flat food_delivery not flagged as a trend",
                         "passed": not any(c == "food_delivery" for c, _ in found)})
    return {"types": res, "controls": controls}


def _pr(tp: int, fp: int, fn: int) -> dict:
    return {"precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None, "tp": tp, "fp": fp, "fn": fn}


def summarize_patterns(per_persona: dict[str, dict]) -> tuple[dict, dict]:
    merged = {t: {"tp": [], "fp": [], "fn": []} for t in PATTERN_TYPES}
    for r in per_persona.values():
        for t, d in r["types"].items():
            for k in d:
                merged[t][k] += d[k]
    scores = {t: _pr(len(d["tp"]), len(d["fp"]), len(d["fn"])) for t, d in merged.items()}
    pooled = _pr(*(sum(scores[k][x] for k in ANOMALY_KINDS) for x in ("tp", "fp", "fn")))
    return scores, {"merged": merged, "anomaly_pooled": pooled}


# ---- forecast backtest ----

def _mape(pairs: list[tuple[float, float]]) -> float | None:
    vals = [abs(f - a) / a for f, a in pairs if a > 0]
    return round(sum(vals) / len(vals), 4) if vals else None


def backtest_persona(conn, persona: str, uid: int, opening: int, last_month: str,
                     version: str = METHOD_VERSION) -> list[dict]:
    full = load_txns(conn, uid, last_month)
    months = months_between(full[0].month, last_month)
    spend: dict[str, dict[str, int]] = {m: defaultdict(int) for m in months}
    for t in full:
        if t.amount_cents < 0:
            spend[t.month][t.category or "uncategorized"] += -t.amount_cents
    total = {m: sum(spend[m].values()) for m in months}
    all_spend = defaultdict(int)
    for m in months:
        for c, v in spend[m].items():
            all_spend[c] += v
    majors = sorted(c for c, v in all_spend.items() if v >= MAJOR_SHARE * sum(all_spend.values()))

    records = []
    for i in range(MIN_HISTORY_MONTHS - 1, len(months) - 1):
        origin, target = months[i], months[i + 1]
        hist = load_txns(conn, uid, origin)  # the loader enforces as_of
        pats = analyze(hist, origin).patterns
        fc = forecast(hist, pats, opening, origin, horizon=1, version=version).points[0]
        fc_nt = forecast(hist, pats, opening, origin, horizon=1, trend_adjust=False, version=version).points[0]
        window = months[max(0, i + 1 - VARIABLE_MONTHS): i + 1]
        actual_balance = opening + sum(t.amount_cents for t in full if t.day <= month_end(target))
        records.append({
            "persona": persona, "origin": origin, "target": target,
            "actual": total[target], "engine": fc["spend_cents"], "no_trend": fc_nt["spend_cents"],
            "naive_last": total[origin], "naive_mean": round(sum(total[m] for m in window) / len(window)),
            "balance_p10": fc["balance_p10"], "balance_p90": fc["balance_p90"], "actual_balance": actual_balance,
            "categories": {c: {"actual": spend[target].get(c, 0), "engine": fc["by_category"].get(c, 0),
                               "naive_last": spend[origin].get(c, 0),
                               "naive_mean": round(sum(spend[m].get(c, 0) for m in window) / len(window))}
                           for c in majors},
        })
    return records


def summarize_forecast(records: list[dict], version: str | None = None) -> dict:
    def m(key: str, rs=records) -> float | None:
        return _mape([(r[key], r["actual"]) for r in rs])

    def bias(key: str) -> int | None:
        return round(sum(r[key] - r["actual"] for r in records) / len(records)) if records else None

    per_cat = defaultdict(list)
    for r in records:
        for c, v in r["categories"].items():
            per_cat[c].append(v)
    return {
        "mape": m("engine"), "naive_mape": m("naive_last"), "naive_mean_mape": m("naive_mean"),
        "no_trend_mape": m("no_trend"), "mean_error_cents": bias("engine"),
        "baseline_mean_error_cents": {k: bias(k) for k in ("no_trend", "naive_last", "naive_mean")},
        "method_version": version,
        "coverage_80": round(sum(r["balance_p10"] <= r["actual_balance"] <= r["balance_p90"] for r in records)
                             / len(records), 4) if records else None,
        "per_category": {c: {"mape": _mape([(v["engine"], v["actual"]) for v in vs]),
                             "naive_mape": _mape([(v["naive_last"], v["actual"]) for v in vs]),
                             "naive_mean_mape": _mape([(v["naive_mean"], v["actual"]) for v in vs]),
                             "n": sum(v["actual"] > 0 for v in vs)} for c, vs in sorted(per_cat.items())},
        "origins": len(records), "horizon": 1,
    }


# ---- run ----

def run(dataset: Path, db_path: Path, reuse: bool, version: str = METHOD_VERSION) -> tuple[dict, dict]:
    truth, meta = load_truth(dataset)
    started = time.monotonic()
    users = prepare_db(dataset, db_path, reuse)
    conn = connect(db_path)
    models = sorted({r[0] for r in conn.execute("SELECT DISTINCT model FROM categorizations")})
    per_persona, records, detected_counts = {}, [], {}
    for key, uid in users.items():
        as_of = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (uid,)).fetchone()[0]
        txns = load_txns(conn, uid, as_of)
        ext = {r[0]: r[1] for r in conn.execute("SELECT id, external_id FROM transactions WHERE user_id = ?", (uid,))}
        res = analyze(txns, as_of)
        detected_counts[key] = res.counts
        per_persona[key] = score_persona(key, res.patterns, ext, truth["personas"][key])
        records += backtest_persona(conn, key, uid, meta[key]["opening_balance_cents"], as_of, version)
    conn.close()

    head = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_sha": _git_sha(),
            "dataset": dataset.name, "seed": truth.get("seed"), "models": {"categorizer": ", ".join(models)}}
    scores, extra = summarize_patterns(per_persona)
    patterns_res = {
        "summary": head | {
            "recurring": {k: scores["recurring"][k] for k in ("precision", "recall")},
            "unused": {k: scores["unused"][k] for k in ("precision", "recall")},
            "anomaly": {k: extra["anomaly_pooled"][k] for k in ("precision", "recall")},
            "pattern_types": scores,
        },
        "details": {"mode": "reuse_db" if reuse else "fresh", "detected_counts": detected_counts,
                    "controls": [c for r in per_persona.values() for c in r["controls"]],
                    "items": extra["merged"], "wall_seconds": round(time.monotonic() - started, 1)},
    }
    forecast_res = {"summary": head | {"forecast": summarize_forecast(records, version)},
                    "details": {"mode": "reuse_db" if reuse else "fresh",
                                "per_persona": {k: summarize_forecast([r for r in records if r["persona"] == k], version)
                                                for k in users},
                                "records": records}}
    return patterns_res, forecast_res


# ---- markdown ----

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _usd(cents: int | None) -> str:
    return "n/a" if cents is None else f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.0f}"


def patterns_md(res: dict) -> str:
    s, d = res["summary"], res["details"]
    lines = [
        "# Pattern detection eval", "",
        f"_Generated by `eval/analysis.py` at {s['run_at']} (git {s['git_sha']}). Do not edit by hand._", "",
        f"Dataset **{s['dataset']}** (seed {s['seed']}), categories from `{s['models']['categorizer']}`, "
        f"mode {d['mode']}. Scored at each persona's as_of_month.", "",
        "| Type | Precision | Recall | TP | FP | FN |", "|---|---|---|---|---|---|",
        *[f"| {t} | {_pct(v['precision'])} | {_pct(v['recall'])} | {v['tp']} | {v['fp']} | {v['fn']} |"
          for t, v in s["pattern_types"].items()],
        f"| anomaly (pooled) | {_pct(s['anomaly']['precision'])} | {_pct(s['anomaly']['recall'])} | | | |", "",
        "## Controls", "", "| Persona | Check | Passed |", "|---|---|---|",
        *[f"| {c['persona']} | {c['check']} | {c['passed']} |" for c in d["controls"]], "",
        "## False positives", "", "| Type | Persona | Detected |", "|---|---|---|",
        *[f"| {t} | {x['persona']} | {x['title']} |" for t, v in d["items"].items() for x in v["fp"]], "",
        "## Misses", "", "| Type | Persona | Planted |", "|---|---|---|",
        *[f"| {t} | {x['persona']} | {x.get('merchant') or x.get('category')} "
          f"{x.get('date') or x.get('direction') or ''} |" for t, v in d["items"].items() for x in v["fn"]],
    ]
    return "\n".join(lines) + "\n"


def forecast_md(res: dict) -> str:
    s, d = res["summary"], res["details"]
    f = s["forecast"]
    lines = [
        "# Forecast backtest (rolling origin, next-month spend)", "",
        f"_Generated by `eval/analysis.py` at {s['run_at']} (git {s['git_sha']}). Do not edit by hand._", "",
        f"Dataset **{s['dataset']}** (seed {s['seed']}), forecast method `{f['method_version']}`, {f['origins']} "
        f"forecasts (personas x origin months), horizon {f['horizon']} month. Per-category actuals use the app's "
        f"own LLM labels.", "",
        "| Scope | Engine | Engine, no trend line | Naive: last month | Naive: 3-mo mean | 80% band coverage |",
        "|---|---|---|---|---|---|",
        f"| All personas | {_pct(f['mape'])} | {_pct(f['no_trend_mape'])} | {_pct(f['naive_mape'])} | "
        f"{_pct(f['naive_mean_mape'])} | {_pct(f['coverage_80'])} |",
        *[f"| {k} | {_pct(v['mape'])} | {_pct(v['no_trend_mape'])} | {_pct(v['naive_mape'])} | "
          f"{_pct(v['naive_mean_mape'])} | {_pct(v['coverage_80'])} |" for k, v in d["per_persona"].items()], "",
        "Mean error, forecast minus actual (negative = under-forecast), all personas:", "",
        "| Engine | Engine, no trend line | Naive: last month | Naive: 3-mo mean |", "|---|---|---|---|",
        f"| {_usd(f['mean_error_cents'])} | {_usd(f['baseline_mean_error_cents']['no_trend'])} | "
        f"{_usd(f['baseline_mean_error_cents']['naive_last'])} | {_usd(f['baseline_mean_error_cents']['naive_mean'])} |",
        "",
        "## Per major category (MAPE, pooled over personas where the category is >= 5% of spend)", "",
        "| Category | n | Engine | Naive: last month | Naive: 3-mo mean |", "|---|---|---|---|---|",
        *[f"| {c} | {v['n']} | {_pct(v['mape'])} | {_pct(v['naive_mape'])} | {_pct(v['naive_mean_mape'])} |"
          for c, v in f["per_category"].items()], "",
        "Caveats: few forecasts from 3 synthetic personas; the generator builds data the way the engine models it "
        "(recurring charges plus noise), which flatters the engine; categories were labelled once over all months.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic")
    p.add_argument("--reuse-db", action="store_true", help="reuse categorizations in data/eval.db (no LLM calls)")
    p.add_argument("--forecast-version", default=METHOD_VERSION, choices=VERSIONS)
    p.add_argument("--out", type=Path, default=RESULTS)
    args = p.parse_args()
    pat, fc = run(args.dataset, DB, args.reuse_db, args.forecast_version)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, res, md in (("patterns", pat, patterns_md), ("forecast", fc, forecast_md),
                          (f"forecast_{args.forecast_version}", fc, forecast_md)):
        (args.out / f"{name}.json").write_text(json.dumps(res, indent=2) + "\n")
        (args.out / f"{name}.md").write_text(md(res))
    print(patterns_md(pat))
    print(forecast_md(fc))


if __name__ == "__main__":
    main()
