"""Categorization eval: runs the real product path (ingest_csv -> LangGraph run) on a labelled dataset.

    python -m eval.categorization [--dataset data/synthetic] [--no-cache] [--prompt-version cat-v2]
                                  [--dev | --limit N --out DIR] [--repeats N]

--dev: fixed seeded stratified subset (~300 rows, every merchant x persona present) for iterating;
results go to eval/results/dev/ and are not committed. Full runs are the committed baselines.

Dataset dir: ground_truth.json with personas.<key>.labels.<external_id>.{category, acceptable_labels?},
plus <key>.csv per persona (or the csv named in personas.json). A hand-labelled real sample uses the
same layout with one persona.

Every run uses a fresh eval DB (empty merchant cache), never the app DB. Results go to
<out>/categorization[_nocache].json and .md (latest, served by the API) plus a copy suffixed with
the prompt version, so earlier baselines stay committed. All numbers are computed here, never typed.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import re
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.agents.categorizer import PROMPT_VERSION, VERSIONS, merchant_in_descriptor, merchant_key
from backend.db import connect, init_db
from backend.graph import create_run, execute_run
from backend.ingest import ingest_csv
from backend.llm.client import get_model
from eval.metrics import (Rec, calibration, confusions, lenient_accuracy, macro_f1, per_category, source_split,
                          strict_accuracy)

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
PRICING = ROOT / "eval" / "pricing.json"
SAMPLE_SEED = 0  # --limit / --dev samples; fixed so runs are comparable
DEV_TARGET = 300
N_EXAMPLE_MISSES = 10


def load_dataset(path: Path) -> dict[str, dict]:
    truth = json.loads((path / "ground_truth.json").read_text())
    meta_path = path / "personas.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    out = {}
    for key, p in truth["personas"].items():
        m = meta.get(key, {})
        out[key] = {"labels": p["labels"], "csv": (path / m.get("csv", f"{key}.csv")).read_text(),
                    "opening_balance_cents": m.get("opening_balance_cents", 0)}
    return out | {"_seed": truth.get("seed")}


def _subset_csv(text: str, keep: set[str]) -> str:
    rows = list(csv.DictReader(io.StringIO(text)))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(r for r in rows if r["external_id"] in keep)
    return buf.getvalue()


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        # eval/results is excluded: an earlier run's output file is not a code change
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "backend", "eval", ":!eval/results"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    pricing = json.loads(PRICING.read_text())
    price = pricing["models"].get(model)
    if price is None:
        return {"usd": None, "note": f"no price for {model} in eval/pricing.json"}
    usd = prompt_tokens / 1e6 * price["input"] + completion_tokens / 1e6 * price["output"]
    return {"usd": round(usd, 5), "note": pricing["note"], "prices_as_of": pricing["as_of"]}


def dev_subset(data: dict[str, dict], target: int = DEV_TARGET) -> set[tuple[str, str]]:
    """Stratified by (persona, merchant): every group gets >= 1 row, the rest proportional
    (largest remainder), rows picked with a fixed seed. Falls back to category if no merchant label."""
    groups: dict[tuple[str, str], list[str]] = {}
    for k in sorted(data):
        for e, lab in sorted(data[k]["labels"].items()):
            groups.setdefault((k, lab.get("merchant") or lab["category"]), []).append(e)
    total = sum(len(v) for v in groups.values())
    quota = {g: target * len(v) / total for g, v in groups.items()}
    alloc = {g: max(1, math.floor(q)) for g, q in quota.items()}
    for g in sorted(groups, key=lambda g: (-(quota[g] - math.floor(quota[g])), g)):
        if sum(alloc.values()) >= target:
            break
        if alloc[g] < len(groups[g]):
            alloc[g] += 1
    rng = random.Random(SAMPLE_SEED)
    return {(g[0], e) for g in sorted(groups) for e in rng.sample(groups[g], alloc[g])}


def run_eval(dataset: Path, *, use_cache: bool, limit: int | None, db_path: Path, dev: bool = False,
             version: str = PROMPT_VERSION) -> dict:
    data = load_dataset(dataset)
    seed = data.pop("_seed")
    if limit or dev:
        pool = sorted((k, e) for k, d in data.items() for e in d["labels"])
        keep = dev_subset(data) if dev else set(random.Random(SAMPLE_SEED).sample(pool, limit))
        for k, d in data.items():
            ids = {e for kk, e in keep if kk == k}
            d["labels"] = {e: lab for e, lab in d["labels"].items() if e in ids}
            d["csv"] = _subset_csv(d["csv"], ids) if ids else ""
        data = {k: d for k, d in data.items() if d["labels"]}

    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)  # fresh DB = empty merchant cache
    conn = connect(db_path)
    init_db(conn)

    model = get_model("categorizer")
    started = time.monotonic()
    run_ids = {}
    for key in sorted(data):  # fixed order: cache hit rate depends on it
        d = data[key]
        batch = ingest_csv(conn, f"{key}.csv", d["csv"], name=key, persona=key,
                           opening_balance_cents=d["opening_balance_cents"])
        run_ids[key] = create_run(conn, batch["batch_id"])
        execute_run(run_ids[key], db_path=str(db_path), use_cache=use_cache, categorizer_version=version)
        status = conn.execute("SELECT status, error FROM runs WHERE id = ?", (run_ids[key],)).fetchone()
        if status["status"] != "done":
            raise SystemExit(f"run for {key} failed: {status['error']}")
    wall_s = round(time.monotonic() - started, 1)

    recs, by_persona, rows_out = [], defaultdict(list), []
    for key, run_id in run_ids.items():
        got = {r["external_id"]: r for r in conn.execute(
            "SELECT t.external_id, t.raw_description, t.amount_cents, c.category, c.confidence, c.rationale, "
            "c.from_cache FROM transactions t JOIN users u ON u.id = t.user_id AND u.persona = ? "
            "LEFT JOIN categorizations c ON c.txn_id = t.id AND c.run_id = ?", (key, run_id))}
        for eid, lab in data[key]["labels"].items():
            g = got[eid]
            rec = Rec(lab["category"], frozenset([lab["category"], *lab.get("acceptable_labels", [])]),
                      g["category"], g["confidence"], None if g["from_cache"] is None else bool(g["from_cache"]))
            recs.append(rec)
            by_persona[key].append(rec)
            rows_out.append({"persona": key, "external_id": eid, "description": g["raw_description"],
                             "amount_cents": g["amount_cents"], "merchant": lab.get("merchant"), "truth": rec.truth,
                             "acceptable": sorted(rec.acceptable), "pred": rec.pred, "confidence": rec.confidence,
                             "from_cache": rec.from_cache, "rationale": g["rationale"]})

    ev = conn.execute(
        "SELECT COUNT(*) AS batches, COALESCE(SUM(prompt_tokens), 0) AS pt, COALESCE(SUM(completion_tokens), 0) AS ct, "
        "COALESCE(SUM(attempt - 1), 0) AS retry_attempts, SUM(status = 'error') AS failed_batches, "
        "SUM(summary LIKE '%MESH FALLBACK%') AS fallbacks FROM agent_events "
        f"WHERE node = 'categorize.batch' AND run_id IN ({','.join('?' * len(run_ids))})",
        list(run_ids.values())).fetchone()

    events = conn.execute(
        f"SELECT node, summary FROM agent_events WHERE node IN ('categorize.batch', 'categorize') "
        f"AND run_id IN ({','.join('?' * len(run_ids))})", list(run_ids.values())).fetchall()
    echo = [tuple(map(int, m.groups())) for e in events if (m := re.search(r"echo_mismatch=(\d+)/(\d+)", e["summary"]))]
    conf_stats = [tuple(map(int, m.groups())) for e in events
                  if (m := re.search(r"confirm_agreed=(\d+), confirm_disagreed=(\d+)", e["summary"]))]
    echo_rej, echo_chk = sum(r for r, _ in echo), sum(c for _, c in echo)

    # Proxy for answers attached to the wrong item, usable for every prompt version: the rationale
    # is asked to name the merchant, so a rationale with no distinctive word from the item's own
    # descriptor suggests misalignment. Noisy (a rationale may paraphrase); compare versions on it.
    labelled = [r for r in rows_out if r["pred"] is not None]
    proxy = [r for r in labelled if not merchant_in_descriptor(r["rationale"], r["description"], min_token=4)]
    proxy_llm = [r for r in proxy if not r["from_cache"]]
    n_llm = sum(1 for r in labelled if not r["from_cache"])

    # Keys whose rows carry more than one true label: where a cache would hide context.
    key_labels = defaultdict(set)
    for row in rows_out:
        k = merchant_key(row["description"], row["amount_cents"])
        if k:
            key_labels[k].add(row["truth"])
    conflicting = sorted(k for k, s in key_labels.items() if len(s) > 1)

    scores = per_category(recs)
    conf = confusions(recs)
    split = source_split(recs)
    misses = [r for r in rows_out if r["pred"] != r["truth"]]
    examples = random.Random(SAMPLE_SEED).sample(misses, min(N_EXAMPLE_MISSES, len(misses)))
    conn.execute("INSERT INTO eval_runs (git_sha, dataset, seed, models, metrics, results_path) VALUES (?, ?, ?, ?, ?, ?)",
                 (_git_sha(), dataset.name, seed, json.dumps({"categorizer": model}),
                  json.dumps({"strict": strict_accuracy(recs)}), "eval/results"))
    conn.commit()
    conn.close()

    return {
        "summary": {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_sha": _git_sha(),
            "dataset": dataset.name + (" (dev subset)" if dev else f" (sample {limit})" if limit else ""),
            "seed": seed,
            "models": {"categorizer": model},
            "categorization": {
                "accuracy": strict_accuracy(recs),
                "lenient_accuracy": lenient_accuracy(recs),
                "macro_f1": macro_f1(scores),
                "top_confusions": [{k: c[k] for k in ("true", "predicted", "count")} for c in conf[:5]],
                "n": len(recs),
                "uncategorized": sum(r.pred is None for r in recs),
                "cache_hit_rate": split["cache_hit_rate"],
                "per_category": scores,
                "per_persona": {k: strict_accuracy(v) for k, v in by_persona.items()},
            },
        },
        "details": {
            "mode": "cache" if use_cache else "no_cache",
            "prompt_version": version,
            "subset": "dev" if dev else (f"limit {limit}" if limit else "full"),
            "misalignment": {
                "echo_rejected": echo_rej if VERSIONS[version].echo_check else None,
                "echo_checked": echo_chk if VERSIONS[version].echo_check else None,
                "echo_rejection_rate": round(echo_rej / echo_chk, 4) if echo_chk else None,
                "rationale_proxy_rate": round(len(proxy) / len(labelled), 4) if labelled else None,
                "rationale_proxy_rate_llm_rows": round(len(proxy_llm) / n_llm, 4) if n_llm else None,
                "rationale_proxy_examples": [{k: r[k] for k in ("description", "pred", "truth", "rationale")}
                                             for r in proxy_llm[:15]],
            },
            "cache_confirmation": ({"agreed": sum(a for a, _ in conf_stats), "disagreed": sum(d for _, d in conf_stats)}
                                   if VERSIONS[version].confirm_before_cache and use_cache else None),
            "per_persona": {k: {"n": len(v), "strict": strict_accuracy(v), "lenient": lenient_accuracy(v)}
                            for k, v in by_persona.items()},
            "source_split": split,
            "confusions": conf,
            "calibration": calibration(recs),
            "lenient_merchants": sorted({r["merchant"] for r in rows_out if len(r["acceptable"]) > 1}),
            "conflicting_cache_keys": conflicting,
            "usage": {"llm_batches": ev["batches"], "prompt_tokens": ev["pt"], "completion_tokens": ev["ct"],
                      "retry_attempts": ev["retry_attempts"], "batches_with_failures": ev["failed_batches"] or 0,
                      "mesh_fallbacks": ev["fallbacks"] or 0, "wall_seconds": wall_s,
                      "estimated_cost": estimate_cost(model, ev["pt"], ev["ct"])},
            "example_misses": examples,
            "misses": misses,
        },
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None, places: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{places}f}"


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def _echo(m: dict) -> str:
    if m["echo_checked"] is None:
        return "n/a (this version has no echo check)"
    return f"{m['echo_rejected']} of {m['echo_checked']} answers ({_pct(m['echo_rejection_rate'])})"


def _confirm(c: dict | None) -> str:
    return "n/a" if c is None else f"{c['agreed']} keys agreed (cached), {c['disagreed']} disagreed (not cached)"


def to_markdown(res: dict) -> str:
    s, d = res["summary"], res["details"]
    c, u = s["categorization"], d["usage"]
    cost = u["estimated_cost"]
    lines = [
        f"# Categorization eval ({d['mode']})",
        "",
        f"_Generated by `eval/categorization.py` at {s['run_at']} (git {s['git_sha']}). Do not edit by hand._",
        "",
        f"Dataset **{s['dataset']}** (seed {s['seed']}), model `{s['models']['categorizer']}`, prompt `{d['prompt_version']}`, "
        f"n = {c['n']}, uncategorized = {c['uncategorized']}.",
        "",
        "| Metric | Value |", "|---|---|",
        f"| Strict accuracy | {_pct(c['accuracy'])} |",
        f"| Lenient accuracy (acceptable_labels for {', '.join(d['lenient_merchants']) or 'none'}) | {_pct(c['lenient_accuracy'])} |",
        f"| Macro-F1 (strict) | {c['macro_f1']:.3f} |",
        f"| Cache hit rate | {_pct(c['cache_hit_rate'])} |",
        f"| Accuracy, LLM-labelled rows (n={d['source_split']['from_llm']['n']}) | {_pct(d['source_split']['from_llm']['strict'])} |",
        f"| Accuracy, cache-labelled rows (n={d['source_split']['from_cache']['n']}) | {_pct(d['source_split']['from_cache']['strict'])} |",
        f"| LLM batches / retry attempts / Mesh fallbacks | {u['llm_batches']} / {u['retry_attempts']} / {u['mesh_fallbacks']} |",
        f"| Tokens (prompt / completion) | {u['prompt_tokens']:,} / {u['completion_tokens']:,} |",
        f"| Estimated cost | {_money(cost['usd'])} |",
        f"| Wall time | {u['wall_seconds']} s |",
        "",
        "## Per persona", "", "| Persona | n | Strict | Lenient |", "|---|---|---|---|",
        *[f"| {k} | {v['n']} | {_pct(v['strict'])} | {_pct(v['lenient'])} |" for k, v in d["per_persona"].items()],
        "",
        "## Per category (strict)", "", "| Category | Precision | Recall | F1 | Support |", "|---|---|---|---|---|",
        *[f"| {k} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1']:.3f} | {v['support']} |"
          for k, v in c["per_category"].items()],
        "",
        "## Top confusion pairs", "", "| True | Predicted | Count | Forgiven by lenient |", "|---|---|---|---|",
        *[f"| {x['true']} | {x['predicted']} | {x['count']} | {x['lenient_ok']} |" for x in d["confusions"][:10]],
        "",
        "## Calibration (stated confidence vs observed accuracy)", "", "| Confidence | n | Accuracy | Mean confidence |",
        "|---|---|---|---|",
        *[f"| {b['bucket']} | {b['n']} | {_pct(b['accuracy'])} | {_num(b['mean_confidence'])} |"
          for b in d["calibration"]],
        "",
        f"Cache keys whose rows carry more than one true label: {len(d['conflicting_cache_keys'])}.",
        "",
        "## Misalignment (answer attached to the wrong item)", "",
        f"- Echo-check rejections (cat-v2+ only): {_echo(d['misalignment'])}",
        f"- Rationale proxy (rationale shares no distinctive word with the item's descriptor): "
        f"{_pct(d['misalignment']['rationale_proxy_rate'])} of labelled rows, "
        f"{_pct(d['misalignment']['rationale_proxy_rate_llm_rows'])} of LLM-labelled rows",
        f"- Cache confirmation (second independent look): {_confirm(d.get('cache_confirmation'))}",
        "",
        f"## {len(d['example_misses'])} example misses (seeded sample of {len(d['misses'])})", "",
        "| Descriptor | Amount | True | Predicted | Conf | Cache | Rationale |", "|---|---|---|---|---|---|---|",
        *[f"| `{m['description']}` | {m['amount_cents'] / 100:.2f} | {m['truth']} | {m['pred']} | "
          f"{_num(m['confidence'], 2)} | {m['from_cache']} | {m['rationale']} |"
          for m in d["example_misses"]],
        "",
        f"Cost note: {cost.get('note', '')}",
    ]
    return "\n".join(lines) + "\n"


def _headline(res: dict) -> dict:
    c, d = res["summary"]["categorization"], res["details"]
    return {"strict": c["accuracy"], "lenient": c["lenient_accuracy"], "macro_f1": c["macro_f1"],
            "cache_hit_rate": c["cache_hit_rate"], "uncategorized": c["uncategorized"],
            "echo_rejection_rate": d["misalignment"]["echo_rejection_rate"],
            "prompt_tokens": d["usage"]["prompt_tokens"], "completion_tokens": d["usage"]["completion_tokens"],
            "cost_usd": d["usage"]["estimated_cost"]["usd"], "wall_seconds": d["usage"]["wall_seconds"]}


def _write(out: Path, names: list[str], res: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        (out / f"{name}.json").write_text(json.dumps(res, indent=2) + "\n")
        (out / f"{name}.md").write_text(to_markdown(res))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic")
    p.add_argument("--no-cache", action="store_true", help="every transaction gets its own LLM look")
    p.add_argument("--prompt-version", default=PROMPT_VERSION, choices=sorted(VERSIONS))
    p.add_argument("--dev", action="store_true", help="fixed stratified ~300-row subset for iterating")
    p.add_argument("--limit", type=int, help="seeded sample of N transactions (dry run)")
    p.add_argument("--repeats", type=int, default=1, help="run N times; report mean/min/max")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.dev and args.limit:
        raise SystemExit("use --dev or --limit, not both")
    out = args.out or (RESULTS / "dev" if args.dev else RESULTS)
    if args.limit and out.resolve() == RESULTS.resolve():
        raise SystemExit("--limit runs are dry runs: pass --out somewhere other than eval/results")

    base = "categorization" + ("_dev" if args.dev else "") + ("_nocache" if args.no_cache else "")
    if args.dataset.resolve() != (ROOT / "data" / "synthetic").resolve():
        base += f"_{args.dataset.name}"
    versioned = f"{base}_{args.prompt_version}"
    db = ROOT / "data" / f"eval{'_nocache' if args.no_cache else ''}.db"

    runs = []
    for i in range(args.repeats):
        res = run_eval(args.dataset, use_cache=not args.no_cache, limit=args.limit, db_path=db,
                       dev=args.dev, version=args.prompt_version)
        runs.append(res)
        h = _headline(res)
        print(f"{versioned} run {i + 1}/{args.repeats}: n={res['summary']['categorization']['n']} "
              f"strict={_pct(h['strict'])} lenient={_pct(h['lenient'])} macroF1={h['macro_f1']:.3f} "
              f"cache_hits={_pct(h['cache_hit_rate'])} uncategorized={h['uncategorized']} "
              f"echo_rejections={_pct(h['echo_rejection_rate'])} | tokens {h['prompt_tokens']:,} in / "
              f"{h['completion_tokens']:,} out, est ${h['cost_usd']}, {h['wall_seconds']} s")

    if args.repeats > 1:
        heads = [_headline(r) for r in runs]
        agg = {k: {"mean": round(statistics.mean(h[k] for h in heads), 4), "min": min(h[k] for h in heads),
                   "max": max(h[k] for h in heads)}
               for k in ("strict", "lenient", "macro_f1", "cache_hit_rate", "cost_usd")}
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{versioned}_repeats.json").write_text(json.dumps(
            {"runs": heads, "aggregate": agg, "summary": runs[-1]["summary"]}, indent=2) + "\n")
        print("aggregate:", json.dumps(agg))
    names = [versioned] if (args.dev or args.limit) else [base, versioned]
    _write(out, names, runs[-1])
    print(f"-> {', '.join(str(out / n) + '.json' for n in names)}")


if __name__ == "__main__":
    main()
