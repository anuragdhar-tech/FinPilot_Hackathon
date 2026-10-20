"""Goal tracking with monthly replay on the synthetic personas. Scenarios, not an accuracy measurement.

    python -m eval.goals [--repeats 3] [--dataset data/synthetic] [--out eval/results]

Reuses data/eval.db from `python -m eval.analysis` (stored categorizations; no Categorizer calls). Each goal is
created on a persona, replayed or checked, read back, then deleted (which restores the persona's clock).

Scenarios (fixed before any goal run, from the realized balances in the generator's CSVs, not from the engine):
- G1 maya_renter: save $2,400 between the end of 2026-03 and the end of 2026-09. Realized: missed.
- G2 ortiz_family: the same goal. Realized: achieved, narrowly.
  Replayed month by month with the real Advisor; reported per month: status, figures, whether the Advisor ran and
  why, accepted advice. Not scored as accuracy: two scenarios say nothing general.
Controlled pair: maya_renter at 2026-06, target 2026-09. Same persona, month and data; only the goal differs:
  modest = 50% and ambitious = 150% of the engine's p50 projected savings (computed in code; the script stops before
  any LLM call if p50 is not positive or the two statuses are not on_track/ahead vs behind). --repeats runs each.
Deterministic (no LLM): the status flips at the computed p10/p50 thresholds on each persona's real forecast; the
  final status and saved amount match the realized balances computed from the CSV; for each cancel/reduce action,
  goal_gap - goal_gap_after equals the action's projected_balance_delta at the target month (two code paths).

Results: eval/results/goals.{json,md}. All numbers computed here, never typed.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from backend import goals
from backend.agents import advisor, analyzer, forecaster, goal_tracker, verifier
from backend.db import connect, init_db
from backend.ledger import load_txns, month_end
from backend.llm.client import Completion, get_model
from backend.models import Claim, GoalCreate
from eval.advice import persona_users
from eval.categorization import _git_sha, estimate_cost

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
DB = ROOT / "data" / "eval.db"

SCENARIOS = [  # fixed from realized balances before any goal run
    {"id": "G1", "persona": "maya_renter", "target_cents": 240000, "start": "2026-03", "target": "2026-09"},
    {"id": "G2", "persona": "ortiz_family", "target_cents": 240000, "start": "2026-03", "target": "2026-09"},
]
PAIR = {"persona": "maya_renter", "start": "2026-06", "target": "2026-09", "modest": 0.5, "ambitious": 1.5}
GOAL_KINDS = verifier.GOAL_KINDS


def no_advisor(messages, **kwargs) -> Completion:  # deterministic checks: the graph runs, the Advisor drafts nothing
    return Completion(json.dumps({"recommendations": []}), "none", 0, 0, False)


def money(c: int | None) -> str:
    return "" if c is None else ("-" if c < 0 else "") + f"${abs(c) / 100:,.2f}"


# ---- realized balances, straight from the CSV (independent of the DB and the engine) ----

def realized_savings(dataset: Path, persona: str, start: str, target: str) -> int:
    meta = json.loads((dataset / "personas.json").read_text())[persona]
    bal = {start: meta["opening_balance_cents"], target: meta["opening_balance_cents"]}
    with open(dataset / meta["csv"]) as f:
        for r in csv.DictReader(f):
            for m in bal:
                if r["date"] <= month_end(m).isoformat():
                    bal[m] += round(float(r["amount"]) * 100)
    return bal[target] - bal[start]


# ---- per-run stats from the stored drafts and audit rows ----

def run_stats(conn, run_id: str) -> dict:
    rows = conn.execute("SELECT r.*, a.verifier_verdict, a.checks, a.rule_checks FROM recommendations r JOIN audit_trail a "
                        "ON a.rec_id = r.id WHERE r.run_id = ? ORDER BY r.lineage_id, r.attempt", (run_id,)).fetchall()
    first = [r for r in rows if r["attempt"] == 1]
    goal_checks = [c for r in first for c in json.loads(r["checks"]) if c["claim"]["kind"] in GOAL_KINDS]
    failed = Counter(x for r in first if r["verifier_verdict"] == "reject" for x in
                     [k["rule"] for k in json.loads(r["rule_checks"]) if not k["ok"]]
                     + [f"claim:{k['claim']['kind']}" for k in json.loads(r["checks"]) if not k["ok"]])
    accepted = []
    for r in conn.execute("SELECT * FROM recommendations WHERE run_id = ? AND status = 'accepted' ORDER BY id", (run_id,)):
        kinds = sorted({Claim.model_validate(c).kind for c in json.loads(r["claims"])} & GOAL_KINDS)
        accepted.append({"action": r["action"], "target": r["target"], "title": r["title"], "body": r["body"],
                         "goal_kinds": kinds})
    tok = conn.execute("SELECT COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0) FROM agent_events "
                       "WHERE run_id = ? AND node = 'advise'", (run_id,)).fetchone()
    return {"lineages": len(first), "first_rejected": sum(r["verifier_verdict"] == "reject" for r in first),
            "first_failed_checks": dict(failed), "goal_claims_first": len(goal_checks),
            "goal_claims_first_ok": sum(c["ok"] for c in goal_checks), "accepted": accepted,
            "prompt_tokens": tok[0], "completion_tokens": tok[1]}


# ---- deterministic ----

def threshold_checks(conn, users: dict[str, int]) -> list[dict]:
    out = []
    for key in sorted(users):
        u = conn.execute("SELECT * FROM users WHERE id = ?", (users[key],)).fetchone()
        start, target = SCENARIOS[0]["start"], SCENARIOS[0]["target"]
        txns = load_txns(conn, u["id"], start)
        fc = forecaster.forecast(txns, analyzer.analyze(txns, start).patterns, u["opening_balance_cents"], start)
        base = goal_tracker.balance_at(txns, u["opening_balance_cents"], start)
        g = lambda t: goal_tracker.Goal(0, "x", t, start, target)  # noqa: E731
        p50 = goal_tracker.projected_savings(g(1), base, fc.points)
        p10 = goal_tracker.projected_savings(g(1), base, fc.points, "balance_p10")
        cases = [(p50, "on_track"), (p50 + 1, "behind")] + ([(p10, "ahead"), (p10 + 1, "on_track")] if p10 > 0 else [])
        got = [goal_tracker.check(g(t), txns, u["opening_balance_cents"], fc.points, start).status for t, _ in cases]
        out.append({"persona": key, "check": f"status flips at p50 {money(p50)}" + (f" and p10 {money(p10)}" if p10 > 0
                    else f" (p10 {money(p10)} not positive: ahead not reachable)"),
                    "ok": got == [s for _, s in cases], "got": got})
    return out


def gap_after_consistency(ctx: verifier.Context) -> list[dict]:
    out = []
    for p in ctx.patterns.values():
        for action in ("cancel", "reduce_spending"):
            acted = verifier.acted_inputs(ctx.inputs, str(p.id), action)
            if acted is None:
                continue
            gap_after = verifier.goal_gap(ctx, acted)
            delta = verifier.balance_delta(ctx, str(p.id), action, ctx.goal.target_month)
            out.append({"action": action, "target": p.params.get("merchant") or p.params.get("category"),
                        "gap_minus_gap_after": verifier.goal_gap(ctx, ctx.inputs) - gap_after, "delta": delta,
                        "ok": verifier.goal_gap(ctx, ctx.inputs) - gap_after == delta})
    return out


# ---- scenarios ----

def replay(conn, uid: int, sc: dict, version: str) -> list[dict]:
    body = GoalCreate(name=f"{sc['id']} save {money(sc['target_cents'])}", target_cents=sc["target_cents"],
                      target_month=sc["target"], start_month=sc["start"])
    gid, _, _ = goals.create_goal(conn, uid, body, defer_advice=False, advisor_version=version)
    try:
        while goals.advance_state(conn, gid)[0]:
            goals.advance(conn, gid, defer_advice=False, advisor_version=version)
            print(f"  {sc['id']}: checked {goals.progress(conn, gid)['current']['month']}")
        months = goals.progress(conn, gid)["months"]
        for m in months:
            m["stats"] = run_stats(conn, m["run_id"]) if m["decision"] == "advise" else None
            m["recommendations"] = [{"action": r["action"], "target": r["target"]} for r in m["recommendations"]]
        return months
    finally:
        goals.delete_goal(conn, gid)


def pair_targets(conn, uid: int) -> dict:
    """Decided before any LLM call: modest/ambitious from the engine's p50 projected savings."""
    u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    txns = load_txns(conn, uid, PAIR["start"])
    fc = forecaster.forecast(txns, analyzer.analyze(txns, PAIR["start"]).patterns, u["opening_balance_cents"], PAIR["start"])
    base = goal_tracker.balance_at(txns, u["opening_balance_cents"], PAIR["start"])
    g = goal_tracker.Goal(0, "x", 1, PAIR["start"], PAIR["target"])
    p50 = goal_tracker.projected_savings(g, base, fc.points)
    p10 = goal_tracker.projected_savings(g, base, fc.points, "balance_p10")
    if p50 <= 0:
        raise SystemExit(f"controlled pair: p50 projected savings {money(p50)} is not positive for {PAIR['persona']} "
                         f"at {PAIR['start']}; choose another persona or fixed targets before running")
    t = {"p50_projected_savings": p50, "p10_projected_savings": p10,
         "modest": round(PAIR["modest"] * p50), "ambitious": round(PAIR["ambitious"] * p50)}
    st = {k: goal_tracker.status_for(t[k], p50, p10) for k in ("modest", "ambitious")}
    if st["modest"] == "behind" or st["ambitious"] != "behind":
        raise SystemExit(f"controlled pair statuses {st} are not (on_track|ahead, behind); not running")
    return t | {"expected_status": st}


def controlled_pair(conn, uid: int, t: dict, repeats: int, version: str) -> dict:
    arms = {}
    for arm in ("modest", "ambitious"):
        runs = []
        for rep in range(repeats):
            body = GoalCreate(name=f"pair {arm}", target_cents=t[arm], target_month=PAIR["target"],
                              start_month=PAIR["start"])
            gid, run_id, _ = goals.create_goal(conn, uid, body, defer_advice=False, advisor_version=version)
            try:
                status = goals.progress(conn, gid)["current"]["status"]
                runs.append({"status": status, **run_stats(conn, run_id)})
            finally:
                goals.delete_goal(conn, gid)
            print(f"  pair {arm} {rep + 1}/{repeats}: {status}, {len(runs[-1]['accepted'])} accepted")
        acc = [a for r in runs for a in r["accepted"]]
        saving = [a for a in acc if a["action"] in ("cancel", "reduce_spending")]
        arms[arm] = {
            "target_cents": t[arm], "statuses": [r["status"] for r in runs], "accepted": len(acc),
            "accepted_with_goal_claim": sum(bool(a["goal_kinds"]) for a in acc),
            "saving_recs": len(saving), "saving_recs_with_goal_gap_after": sum("goal_gap_after" in a["goal_kinds"]
                                                                               for a in saving),
            "runs_stating_goal_gap": sum(any("goal_gap" in a["goal_kinds"] for a in r["accepted"]) for r in runs),
            "action_sets": [sorted(f"{a['action']} {a['target']}" for a in r["accepted"]) for r in runs],
            "lineages": sum(r["lineages"] for r in runs), "first_rejected": sum(r["first_rejected"] for r in runs),
            "first_failed_checks": dict(sum((Counter(r["first_failed_checks"]) for r in runs), Counter())),
            "goal_claims_first": sum(r["goal_claims_first"] for r in runs),
            "goal_claims_first_ok": sum(r["goal_claims_first_ok"] for r in runs),
            "prompt_tokens": sum(r["prompt_tokens"] for r in runs),
            "completion_tokens": sum(r["completion_tokens"] for r in runs),
            "example": [{"title": a["title"], "body": a["body"], "goal_kinds": a["goal_kinds"]} for a in runs[0]["accepted"]],
        }
    um, ua = ({x for s in arms[k]["action_sets"] for x in s} for k in ("modest", "ambitious"))
    return {"targets": t, "arms": arms,
            "action_set_jaccard": round(len(um & ua) / len(um | ua), 3) if um | ua else None}


# ---- report ----

def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}% ({a}/{b})" if b else "n/a (0)"


def to_md(res: dict) -> str:
    s, d = res["summary"], res["details"]
    out = [f"# Goal tracking: replay scenarios and controlled pair ({s['prompt_version']})", "",
           f"Run {s['run_at']} at {s['git_sha']}; Advisor `{s['models']['advisor']}`; synthetic dataset, seed "
           f"{s['seed']}. **Scenarios chosen from realized balances, not an accuracy measurement**: two replayed goals "
           "and one same-month pair show how the loop behaves; they do not estimate how often status is right.", "",
           "## Deterministic checks (no LLM)", "", "| Check | Result |", "|---|---|"]
    for c in d["threshold_checks"]:
        out.append(f"| {c['persona']}: {c['check']} | {'pass' if c['ok'] else 'FAIL ' + str(c['got'])} |")
    for r in d["realized_checks"]:
        out.append(f"| {r['id']}: final status and saved match the CSV ({r['realized_status']}, "
                   f"{money(r['realized_saved'])}) | {'pass' if r['ok'] else 'FAIL'} |")
    ga = d["gap_after_consistency"]
    out.append(f"| goal_gap - goal_gap_after = projected_balance_delta at the target month ({len(ga)} actions, "
               f"{PAIR['persona']} {PAIR['start']}) | {sum(c['ok'] for c in ga)}/{len(ga)} |")
    for sc in d["scenarios"]:
        out += ["", f"## {sc['id']}: {sc['persona']}, save {money(sc['target_cents'])} from the end of {sc['start']} "
                    f"to the end of {sc['target']} (realized: {sc['realized_status']}, {money(sc['realized_saved'])})",
                "", "| Month | Status | Saved | Plan | p50 / p10 at target | Gap | Advisor | Why | Accepted advice "
                    "(goal claims) |", "|---|---|---|---|---|---|---|---|---|"]
        for m in sc["months"]:
            st = m["stats"]
            recs = ("; ".join(f"{a['action']} {a['target']}" + (f" ({', '.join(a['goal_kinds'])})" if a["goal_kinds"]
                                                                 else "") for a in st["accepted"]) or "none"
                    ) if st else (f"carried from {m['advice_from_month']}" if m["decision"] == "carry" else "")
            proj = "" if m["projected_p50_cents"] is None else \
                f"{money(m['projected_p50_cents'])} / {money(m['projected_p10_cents'])}"
            out.append(f"| {m['month']} | {m['status']} | {money(m['saved_cents'])} | {money(m['planned_cents'])} | "
                       f"{proj} | {money(m['gap_cents'])} | {m['decision']} | {m['reason']} | {recs} |")
        adv = [m for m in sc["months"] if m["decision"] == "advise"]
        open_ = [m for m in sc["months"] if m["decision"] != "final"]
        out += ["", f"Advisor called in {len(adv)} of {len(open_)} open months; first-draft rejections "
                    f"{sum(m['stats']['first_rejected'] for m in adv)}/{sum(m['stats']['lineages'] for m in adv)}; "
                    f"first-behind month: {sc['first_behind'] or 'never'}; months flagged behind: "
                    f"{sc['behind_months']}."]
    p = d["pair"]
    t = p["targets"]
    out += ["", f"## Controlled pair: {PAIR['persona']} at {PAIR['start']}, target {PAIR['target']}", "",
            f"Engine p50 projected savings {money(t['p50_projected_savings'])} (p10 {money(t['p10_projected_savings'])}), "
            f"positive, so the pair stays on this persona: modest {money(t['modest'])} "
            f"({int(PAIR['modest'] * 100)}%), ambitious {money(t['ambitious'])} ({int(PAIR['ambitious'] * 100)}%). "
            f"Same data, same month; only the goal differs. {s['repeats']} runs each.", "",
            "| | Modest | Ambitious |", "|---|---|---|"]
    a, b = p["arms"]["modest"], p["arms"]["ambitious"]
    rows = [("Status (code)", ", ".join(sorted(set(a["statuses"]))), ", ".join(sorted(set(b["statuses"])))),
            ("Accepted recommendations", a["accepted"], b["accepted"]),
            ("... carrying a verified goal claim", pct(a["accepted_with_goal_claim"], a["accepted"]),
             pct(b["accepted_with_goal_claim"], b["accepted"])),
            ("Cancel/reduce carrying goal_gap_after", pct(a["saving_recs_with_goal_gap_after"], a["saving_recs"]),
             pct(b["saving_recs_with_goal_gap_after"], b["saving_recs"])),
            ("Runs stating the goal_gap", f"{a['runs_stating_goal_gap']}/{s['repeats']}",
             f"{b['runs_stating_goal_gap']}/{s['repeats']}"),
            ("First-draft failure rate", pct(a["first_rejected"], a["lineages"]), pct(b["first_rejected"], b["lineages"])),
            ("First-draft goal claims recomputed correctly", pct(a["goal_claims_first_ok"], a["goal_claims_first"]),
             pct(b["goal_claims_first_ok"], b["goal_claims_first"])),
            ("Action sets per run", " / ".join(", ".join(x) or "none" for x in a["action_sets"]),
             " / ".join(", ".join(x) or "none" for x in b["action_sets"]))]
    out += [f"| {k} | {x} | {y} |" for k, x, y in rows]
    out += ["", f"Jaccard of the two arms' action sets: {p['action_set_jaccard']}.", "", "Example (run 1):", ""]
    for arm in ("modest", "ambitious"):
        for e in p["arms"][arm]["example"]:
            out.append(f"- **{arm}**: {e['title']} -- {e['body']}")
    u = s["usage"]
    out += ["", f"Advisor tokens: {u['prompt_tokens']:,} prompt, {u['completion_tokens']:,} completion; estimated "
                f"cost ${u['estimated_cost']['usd']} ({u['estimated_cost'].get('note', '')}).", ""]
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--out", type=Path, default=RESULTS)
    args = p.parse_args()
    version = advisor.PROMPT_VERSION
    conn = connect(DB)
    init_db(conn)
    users = persona_users(conn)
    if any(conn.execute("SELECT 1 FROM goals").fetchall()):
        raise SystemExit("data/eval.db already has goals (an interrupted run?); delete them first")

    # decided before any LLM call
    t = pair_targets(conn, users[PAIR["persona"]])
    print(f"controlled pair: p50 {money(t['p50_projected_savings'])}, modest {money(t['modest'])}, ambitious "
          f"{money(t['ambitious'])}, expected {t['expected_status']}")
    gid, run_id, _ = goals.create_goal(conn, users[PAIR["persona"]], GoalCreate(
        name="consistency", target_cents=t["ambitious"], target_month=PAIR["target"], start_month=PAIR["start"]),
        defer_advice=False, advisor_llm=no_advisor)
    try:
        consistency = gap_after_consistency(verifier.Context.load(conn, run_id))
    finally:
        goals.delete_goal(conn, gid)
    thresholds = threshold_checks(conn, users)

    scenarios, realized_checks = [], []
    for sc in SCENARIOS:
        saved = realized_savings(args.dataset, sc["persona"], sc["start"], sc["target"])
        status = "achieved" if saved >= sc["target_cents"] else "missed"
        months = replay(conn, users[sc["persona"]], sc, version)
        last = months[-1]
        realized_checks.append({"id": sc["id"], "realized_status": status, "realized_saved": saved,
                                "app_status": last["status"], "app_saved": last["saved_cents"],
                                "ok": (last["status"], last["saved_cents"]) == (status, saved)})
        behind = [m["month"] for m in months if m["status"] == "behind"]
        scenarios.append(sc | {"realized_status": status, "realized_saved": saved, "months": months,
                               "first_behind": behind[0] if behind else None, "behind_months": len(behind)})
    pair = controlled_pair(conn, users[PAIR["persona"]], t, args.repeats, version)

    pt = sum(m["stats"]["prompt_tokens"] for sc in scenarios for m in sc["months"] if m["stats"]) + \
        sum(a["prompt_tokens"] for a in pair["arms"].values())
    ct = sum(m["stats"]["completion_tokens"] for sc in scenarios for m in sc["months"] if m["stats"]) + \
        sum(a["completion_tokens"] for a in pair["arms"].values())
    model = get_model("advisor")
    summary = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_sha": _git_sha(),
               "dataset": args.dataset.name, "seed": json.loads((args.dataset / "ground_truth.json").read_text()).get("seed"),
               "models": {"advisor": model}, "prompt_version": f"{version}+{advisor.GOAL_VERSION}",
               "repeats": args.repeats,
               "usage": {"prompt_tokens": pt, "completion_tokens": ct, "estimated_cost": estimate_cost(model, pt, ct)}}
    res = {"summary": summary, "details": {"threshold_checks": thresholds, "realized_checks": realized_checks,
                                           "gap_after_consistency": consistency, "scenarios": scenarios, "pair": pair}}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "goals.json").write_text(json.dumps(res, indent=2) + "\n")
    (args.out / "goals.md").write_text(to_md(res))
    print(to_md(res))


if __name__ == "__main__":
    main()
