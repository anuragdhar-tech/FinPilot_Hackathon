"""Advisor <-> Verifier loop and what-if eval on the planted synthetic data (ground truth is read only here).

    python -m eval.advice [--repeats 3] [--prompt-version adv-v2] [--dataset data/synthetic] [--no-whatif-llm]

Reuses data/eval.db from `python -m eval.analysis` (transactions + LLM categorizations; no Categorizer calls):
each persona gets `--repeats` real main runs from the analyze node on (analyze -> forecast -> advise -> verify
-> persist), with the real Advisor LLM. Everything below is computed from the stored rows.

Loop:
- first-draft failure rate: lineages whose attempt-1 draft the Verifier rejected / all lineages; every failure
  is listed with the checks it failed
- grounded-number rate (first drafts): numbers in title + body that match a claim whose recompute passed /
  all numbers (accepted drafts are 100% by construction, reported only as a check)
- planted unused memberships: an accepted `cancel` whose trigger pattern matches the planted series (evidence
  Jaccard >= 0.5, as in eval/analysis.py), per run
- controls: no accepted `cancel` on a planted used membership; cancel precision; price-rise and trend recall
  are reported, not gated
What-if:
- deterministic (no LLM), per persona on its baseline: a one-off $X moves p50 and p10 by exactly -X from its
  month on and 0 before; a recurring change accumulates; the verdict flips at the computed threshold +-1 cent
- LLM: scripted phrasings (fixed before any output was seen) parsed to the expected scenario; the
  explanation's number-check pass rate

Results: eval/results/advice.{json,md} (latest) and advice_<prompt version>.{json,md} (kept per Advisor prompt
version, so earlier baselines stay committed). All numbers computed here, never typed.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.agents import whatif
from backend.agents.analyzer import Detected
from backend.agents.forecaster import inputs_from_params
from backend.agents.verifier import number_report
from backend.db import connect, init_db
from backend.graph import create_run, execute_run, run_whatif
from backend.ledger import add_months
from backend.llm.client import get_model
from backend.models import Claim, Scenario
from eval.analysis import _match_series, load_truth
from eval.categorization import _git_sha, estimate_cost

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
DB = ROOT / "data" / "eval.db"

# (text, expected items as (kind, amount_cents, start_month, months)); as_of 2026-09. Written before any run.
PARSE_CASES = [
    ("Can I afford a $2,000 trip next month?", [("one_off", -200000, "2026-10", None)]),
    ("What if I buy a $1,200 laptop in December?", [("one_off", -120000, "2026-12", None)]),
    ("I'm thinking of spending $500 on a concert in 2026-11", [("one_off", -50000, "2026-11", None)]),
    ("What if my rent goes up by $150 a month starting January?", [("recurring_change", -15000, "2027-01", None)]),
    ("What if I get a $300 monthly raise starting next month?", [("recurring_change", 30000, "2026-10", None)]),
    ("Can I afford a $40/month gym membership for 6 months starting next month?",
     [("recurring_change", -4000, "2026-10", 6)]),
    ("What happens if I get a $3,000 bonus in March?", [("one_off", 300000, "2027-03", None)]),
    ("Should I buy a $25,000 car next month?", [("one_off", -2500000, "2026-10", None)]),
]


def persona_users(conn) -> dict[str, int]:
    rows = conn.execute("SELECT persona, MAX(id) AS id FROM users WHERE persona IS NOT NULL GROUP BY persona").fetchall()
    users = {r["persona"]: r["id"] for r in rows}
    for key, uid in users.items():
        missing = conn.execute("SELECT COUNT(*) FROM transactions t WHERE t.user_id = ? AND NOT EXISTS "
                               "(SELECT 1 FROM categorizations c WHERE c.txn_id = t.id)", (uid,)).fetchone()[0]
        if missing:
            raise SystemExit(f"{missing} uncategorized transactions for {key}; run python -m eval.analysis first")
    return users


def run_loops(conn, users: dict[str, int], repeats: int, version: str) -> dict[str, list[str]]:
    runs: dict[str, list[str]] = defaultdict(list)
    for key in sorted(users):
        batch = conn.execute("SELECT id FROM batches WHERE user_id = ? ORDER BY uploaded_at DESC, rowid DESC LIMIT 1",
                             (users[key],)).fetchone()["id"]
        for _ in range(repeats):
            run_id = create_run(conn, batch)
            execute_run(run_id, db_path=str(DB), entry="analyze", advisor_version=version)
            st = conn.execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
            if st["status"] != "done":
                raise SystemExit(f"run for {key} failed: {st['error']}")
            runs[key].append(run_id)
            print(f"  {key}: run {run_id[:8]} done")
    return runs


# ---- loop scoring ----

def _failed_checks(audit) -> list[str]:
    return ([r["rule"] for r in json.loads(audit["rule_checks"]) if not r["ok"]]
            + [f"claim:{c['claim']['kind']}" for c in json.loads(audit["checks"]) if not c["ok"]])


def score_loop(conn, runs: dict[str, list[str]], truth: dict) -> dict:
    by_check: Counter = Counter()
    failures, per_persona, matching = [], {}, []
    n_num = n_num_ok = n_claims = n_claims_ok = 0
    acc_num = acc_num_ok = 0
    lineages = accepted = dropped = withdrawn = drafts = rejected = first_rejected = 0
    tokens = Counter()
    for key, run_ids in sorted(runs.items()):
        gt = truth["personas"][key]["recurring"]
        unused_gt = [j for j, g in enumerate(gt) if g["unused"] is True]
        used_gt = [j for j, g in enumerate(gt) if g["unused"] is False]
        price_gt = [j for j, g in enumerate(gt) if g["price_change"]]
        trend_cats = {t["category"] for t in truth["personas"][key]["trends"] if t["direction"] == "rising"}
        stats = Counter()
        for rep, run_id in enumerate(run_ids, 1):
            rows = conn.execute("SELECT r.*, a.verifier_verdict, a.reasons, a.checks, a.rule_checks, ru.as_of_month "
                                "FROM recommendations r JOIN audit_trail a ON a.rec_id = r.id JOIN runs ru ON "
                                "ru.id = r.run_id WHERE r.run_id = ? ORDER BY r.lineage_id, r.attempt",
                                (run_id,)).fetchall()
            for e in conn.execute("SELECT prompt_tokens, completion_tokens FROM agent_events WHERE run_id = ? "
                                  "AND node = 'advise'", (run_id,)):
                tokens["prompt"] += e["prompt_tokens"] or 0
                tokens["completion"] += e["completion_tokens"] or 0
            lineage: dict[str, list] = defaultdict(list)
            for r in rows:
                lineage[r["lineage_id"]].append(r)
                drafts += 1
                rejected += r["verifier_verdict"] == "reject"
                claims = [Claim.model_validate(c) for c in json.loads(r["claims"])]
                oks = [next((k["ok"] for k in json.loads(r["checks"]) if k["claim"] == c.model_dump()), False)
                       for c in claims]
                n, g = number_report(f"{r['title']}\n{r['body']}", claims, oks, r["as_of_month"])
                if r["attempt"] == 1:
                    n_num, n_num_ok = n_num + n, n_num_ok + g
                    n_claims, n_claims_ok = n_claims + len(oks), n_claims_ok + sum(oks)
                if r["status"] == "accepted":
                    acc_num, acc_num_ok = acc_num + n, acc_num_ok + g
            for lid, rs in lineage.items():
                lineages += 1
                first = rs[0]
                if first["verifier_verdict"] == "reject":
                    first_rejected += 1
                    checks = _failed_checks(first)
                    by_check.update(set(checks))
                    failures.append({"persona": key, "repeat": rep, "action": first["action"],
                                     "target": first["target"], "checks": checks,
                                     "reasons": json.loads(first["reasons"]),
                                     "outcome": ("accepted" if any(r["status"] == "accepted" for r in rs) else
                                                 "dropped" if rs[-1]["status"] == "dropped" else "withdrawn"),
                                     "attempts": len(rs)})
                if any(r["status"] == "accepted" for r in rs):
                    accepted += 1
                elif rs[-1]["status"] == "dropped":
                    dropped += 1
                else:
                    withdrawn += 1

            # planted facts vs accepted recommendations (trigger pattern matched by evidence)
            pats = conn.execute("SELECT * FROM patterns WHERE run_id = ? AND type = 'recurring' ORDER BY id",
                                (run_id,)).fetchall()
            ev = defaultdict(list)
            for e in conn.execute("SELECT pe.pattern_id, pe.txn_id FROM pattern_evidence pe JOIN patterns p ON "
                                  "p.id = pe.pattern_id WHERE p.run_id = ?", (run_id,)):
                ev[e["pattern_id"]].append(e["txn_id"])
            det = [Detected("recurring", p["title"], json.loads(p["params"]), ev[p["id"]], p["id"]) for p in pats]
            uid = conn.execute("SELECT user_id FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
            ext = dict(conn.execute("SELECT id, external_id FROM transactions WHERE user_id = ?", (uid,)).fetchall())
            match = _match_series(det, gt, ext)
            pid_of = {j: str(det[i].id) for i, j in match.items()}
            acc = [r for rs in lineage.values() for r in rs if r["status"] == "accepted"]
            cancels = {r["trigger_id"] for r in acc if r["action"] == "cancel"}
            reviews = {r["trigger_id"] for r in acc if r["action"] == "review_price_increase"}
            trend_pats = {str(p["id"]): json.loads(p["params"])["category"] for p in conn.execute(
                "SELECT id, params FROM patterns WHERE run_id = ? AND type = 'trend'", (run_id,))}
            reduces = {trend_pats.get(r["trigger_id"]) for r in acc if r["action"] == "reduce_spending"}
            for j in unused_gt:
                hit = pid_of.get(j) in cancels
                stats["unused_hit"] += hit
                stats["unused_n"] += 1
                matching.append({"persona": key, "repeat": rep, "planted": gt[j]["merchant"], "kind": "unused",
                                 "ok": hit})
            for j in used_gt:
                ok = pid_of.get(j) not in cancels
                stats["control_ok"] += ok
                stats["control_n"] += 1
                matching.append({"persona": key, "repeat": rep, "planted": gt[j]["merchant"], "kind": "used control",
                                 "ok": ok})
            unused_pids = {pid_of.get(j) for j in unused_gt}
            stats["cancel_n"] += len(cancels)
            stats["cancel_tp"] += len(cancels & unused_pids)
            for j in price_gt:
                stats["price_hit"] += pid_of.get(j) in reviews
                stats["price_n"] += 1
            for cat in trend_cats:
                stats["trend_hit"] += cat in reduces
                stats["trend_n"] += 1
            stats["accepted"] += len(acc)
            stats["reduce_other"] += len([c for c in reduces if c not in trend_cats])
        per_persona[key] = dict(stats)

    def rate(a, b):
        return round(a / b, 4) if b else None

    tot = Counter()
    for s in per_persona.values():
        tot.update(s)
    model = get_model("advisor")
    return {
        "verifier": {"first_draft_fail_rate": rate(first_rejected, lineages) or 0.0, "drafts": drafts,
                     "rejected": rejected, "lineages": lineages, "accepted": accepted, "dropped": dropped + withdrawn,
                     "by_check": dict(by_check.most_common())},
        "grounded_number_rate": rate(n_num_ok, n_num),
        "details": {
            "withdrawn": withdrawn, "dropped_after_max_attempts": dropped,
            "first_draft_numbers": n_num, "first_draft_numbers_grounded": n_num_ok,
            "first_draft_claims": n_claims, "first_draft_claims_ok": n_claims_ok,
            "first_draft_claim_correct_rate": rate(n_claims_ok, n_claims),
            "accepted_grounded_number_rate": rate(acc_num_ok, acc_num),
            "unused_recall": rate(tot["unused_hit"], tot["unused_n"]),
            "controls_passed": f"{tot['control_ok']}/{tot['control_n']}",
            "cancel_precision": rate(tot["cancel_tp"], tot["cancel_n"]),
            "price_rise_recall": rate(tot["price_hit"], tot["price_n"]),
            "trend_recall": rate(tot["trend_hit"], tot["trend_n"]),
            "reduce_on_unplanted_trend": tot["reduce_other"],
            "per_persona": per_persona, "planted_checks": matching, "failures": failures,
            "usage": {"model": model, "prompt_tokens": tokens["prompt"], "completion_tokens": tokens["completion"],
                      "estimated_cost": estimate_cost(model, tokens["prompt"], tokens["completion"])},
        },
    }


# ---- what-if ----

def whatif_sanity(conn, users: dict[str, int]) -> list[dict]:
    out = []
    for key in sorted(users):
        fc = conn.execute("SELECT * FROM forecasts WHERE user_id = ? AND scenario IS NULL ORDER BY id DESC LIMIT 1",
                          (users[key],)).fetchone()
        base, as_of = inputs_from_params(json.loads(fc["params"])), fc["as_of_month"]
        m = add_months(as_of, 2)

        def one(cents, month=m):
            return Scenario(items=[{"kind": "one_off", "label": "x", "amount_cents": cents, "start_month": month}])

        sim = whatif.simulate(base, one(-200000), 6)
        d50 = [p["balance_p50"] - b["balance_p50"] for p, b in zip(sim.projected, sim.baseline)]
        d10 = [p["balance_p10"] - b["balance_p10"] for p, b in zip(sim.projected, sim.baseline)]
        out.append({"persona": key, "check": f"$2,000 one-off in {m}: p50 and p10 move by exactly -$2,000 from {m}",
                    "ok": d50 == d10 == [0] + [-200000] * 5})
        rc = Scenario(items=[{"kind": "recurring_change", "label": "x", "amount_cents": -4000,
                              "start_month": add_months(as_of, 1), "months": 3}])
        sim = whatif.simulate(base, rc, 6)
        d = [p["balance_p50"] - b["balance_p50"] for p, b in zip(sim.projected, sim.baseline)]
        out.append({"persona": key, "check": "-$40/month for 3 months accumulates to -$120 and stays",
                    "ok": d == [-4000, -8000, -12000, -12000, -12000, -12000]})
        first = add_months(as_of, 1)
        bl = whatif.simulate(base, one(-1, first), 6).baseline
        x10, x50 = min(p["balance_p10"] for p in bl), min(p["balance_p50"] for p in bl)
        flips = [whatif.simulate(base, one(-x, first), 6).verdict for x in (x10, x10 + 1, x50, x50 + 1)]
        out.append({"persona": key, "check": f"verdict flips at the computed thresholds (p10 {x10}, p50 {x50} cents)",
                    "ok": flips == ["affordable", "tight", "tight", "not_affordable"], "got": flips})
    return out


def whatif_llm(conn, uid: int) -> dict:
    as_of = conn.execute("SELECT as_of_month FROM users WHERE id = ?", (uid,)).fetchone()[0]
    assert as_of == "2026-09", "PARSE_CASES are written for as_of 2026-09"
    cases = []
    for text, expected in PARSE_CASES:
        try:
            res = run_whatif(conn, uid, text=text)
        except whatif.ScenarioError as e:
            cases.append({"text": text, "expected": expected, "got": None, "parse_ok": False, "error": str(e)})
            continue
        got = [(i["kind"], i["amount_cents"], i["start_month"], i["months"]) for i in res["scenario"]["items"]]
        cases.append({"text": text, "expected": expected, "got": got,
                      "parse_ok": sorted(got) == sorted(map(tuple, expected)),
                      "verdict": res["verdict"], "explanation": res["explanation"],
                      "explanation_source": res["explanation_source"], "number_check": res["number_check"],
                      "first_explain_ok": not any(i.startswith("attempt 1:") for i in res["number_check"]["issues"])})
    explained = [c for c in cases if c["got"] is not None]
    return {"parse_accuracy": round(sum(c["parse_ok"] for c in cases) / len(cases), 4), "n": len(cases),
            "explain_first_try_pass_rate": round(sum(c["first_explain_ok"] for c in explained) / len(explained), 4)
            if explained else None,
            "explain_template_used": sum(c["explanation_source"] == "template" for c in explained),
            "cases": cases}


# ---- output ----

def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def to_md(res: dict) -> str:
    s, d = res["summary"], res["details"]
    v = s["verifier"]
    lines = [
        "# Advisor <-> Verifier loop and what-if eval", "",
        f"_Generated by `eval/advice.py` at {s['run_at']} (git {s['git_sha']}). Do not edit by hand._", "",
        f"Dataset **{s['dataset']}** (seed {s['seed']}), advisor `{s['models']['advisor']}`, prompt "
        f"`{d['prompt_version']}`, {d['repeats']} run(s) per persona on `data/eval.db` (stored LLM categories).", "",
        "## Loop", "",
        "| Metric | Value |", "|---|---|",
        f"| Lineages (recommendations drafted) | {v['lineages']} |",
        f"| First-draft failure rate | {_pct(v['first_draft_fail_rate'])} |",
        f"| Drafts / rejected | {v['drafts']} / {v['rejected']} |",
        f"| Accepted / dropped after 3 / withdrawn | {v['accepted']} / {d['dropped_after_max_attempts']} / {d['withdrawn']} |",
        f"| Grounded-number rate, first drafts | {_pct(s['grounded_number_rate'])} "
        f"({d['first_draft_numbers_grounded']}/{d['first_draft_numbers']}) |",
        f"| Claim-correct rate, first drafts | {_pct(d['first_draft_claim_correct_rate'])} "
        f"({d['first_draft_claims_ok']}/{d['first_draft_claims']}) |",
        f"| Grounded-number rate, accepted (100% by construction) | {_pct(d['accepted_grounded_number_rate'])} |",
        f"| Advisor tokens (prompt / completion), est. cost | {d['usage']['prompt_tokens']} / "
        f"{d['usage']['completion_tokens']}, ${d['usage']['estimated_cost'].get('usd')} |", "",
        "First-draft failures by check: " + (", ".join(f"{k} {n}" for k, n in v["by_check"].items()) or "none"), "",
        "## Planted facts (accepted recommendations, per run)", "",
        "| Metric | Value |", "|---|---|",
        f"| Planted unused memberships with an accepted cancel | {_pct(d['unused_recall'])} |",
        f"| Used-membership controls with no cancel | {d['controls_passed']} |",
        f"| Cancel precision | {_pct(d['cancel_precision'])} |",
        f"| Price rises with an accepted review (not gated) | {_pct(d['price_rise_recall'])} |",
        f"| Planted rising trend with an accepted reduce (not gated) | {_pct(d['trend_recall'])} |",
        f"| Reduce recommendations on an unplanted trend | {d['reduce_on_unplanted_trend']} |", "",
        "| Persona | Run | Planted | Kind | Passed |", "|---|---|---|---|---|",
        *[f"| {m['persona']} | {m['repeat']} | {m['planted']} | {m['kind']} | {m['ok']} |" for m in d["planted_checks"]],
        "", "## First-draft failures", "",
        "| Persona | Run | Draft | Failed checks | Outcome | Reason |", "|---|---|---|---|---|---|",
        *[f"| {f['persona']} | {f['repeat']} | {f['action']} {f['target']} | {', '.join(f['checks'])} | "
          f"{f['outcome']} after {f['attempts']} | {'; '.join(f['reasons'])[:220].replace('|', '/')} |"
          for f in d["failures"]],
        "", "## What-if", "",
        "| Persona | Deterministic check | Passed |", "|---|---|---|",
        *[f"| {c['persona']} | {c['check']} | {c['ok']} |" for c in d["whatif_sanity"]],
    ]
    w = d.get("whatif_llm")
    if w:
        lines += ["", f"Parser: {_pct(w['parse_accuracy'])} exact ({w['n']} scripted phrasings). Explanation number "
                      f"check passed first try: {_pct(w['explain_first_try_pass_rate'])}; template used "
                      f"{w['explain_template_used']} time(s).", "",
                  "| Text | Expected | Parsed | OK | Verdict | Explanation |", "|---|---|---|---|---|---|",
                  *[f"| {c['text']} | {c['expected']} | {c['got']} | {c['parse_ok']} | {c.get('verdict', '')} | "
                    f"{c.get('explanation_source', c.get('error', ''))}: {(c.get('explanation') or '')[:140]} |"
                    for c in w["cases"]]]
    lines += ["", "Caveats: synthetic personas only; few lineages per run; the Advisor runs at temperature 0, so repeats "
                  "are close to identical and mostly measure residual nondeterminism."]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--prompt-version", default=None, help="Advisor prompt version (default: active)")
    p.add_argument("--no-whatif-llm", action="store_true", help="skip the parser/explanation cases (LLM)")
    p.add_argument("--out", type=Path, default=RESULTS)
    args = p.parse_args()
    from backend.agents.advisor import PROMPT_VERSION, VERSIONS
    version = args.prompt_version or PROMPT_VERSION
    if version not in VERSIONS:
        raise SystemExit(f"--prompt-version must be one of {VERSIONS}")
    truth, _ = load_truth(args.dataset)
    conn = connect(DB)
    init_db(conn)
    users = persona_users(conn)
    runs = run_loops(conn, users, args.repeats, version)
    loop = score_loop(conn, runs, truth)
    details = loop["details"] | {"repeats": args.repeats, "prompt_version": version,
                                 "runs": runs, "whatif_sanity": whatif_sanity(conn, users)}
    if not args.no_whatif_llm:
        details["whatif_llm"] = whatif_llm(conn, users["maya_renter"])
    summary = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_sha": _git_sha(),
               "dataset": args.dataset.name, "seed": truth.get("seed"),
               "models": {"advisor": get_model("advisor"), "parser": get_model("parser")},
               "verifier": loop["verifier"], "grounded_number_rate": loop["grounded_number_rate"]}
    res = {"summary": summary, "details": details}
    args.out.mkdir(parents=True, exist_ok=True)
    for name in ("advice", f"advice_{version}"):
        (args.out / f"{name}.json").write_text(json.dumps(res, indent=2) + "\n")
        (args.out / f"{name}.md").write_text(to_md(res))
    print(to_md(res))


if __name__ == "__main__":
    main()
