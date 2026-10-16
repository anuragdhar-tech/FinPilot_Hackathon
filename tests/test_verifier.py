"""Verifier: every claim kind recomputes from raw data (and fails when off by more than its tolerance), every rule
catches its failure type, and the number extractor reads money, percents, months, dates and day/month spans."""
import copy

import pytest

from backend.agents import verifier
from backend.agents.verifier import Draft, extract_numbers, verify
from backend.db import connect
from backend.ledger import add_months
from tests.ledger_fixture import AS_OF, build_ledger, figure, final, pattern_id, run_main, trend_id, uber_rec


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    path = tmp_path_factory.mktemp("v") / "t.db"
    build_ledger(path)
    run_id = run_main(path, lambda m, **kw: final())
    conn = connect(path)
    c = verifier.Context.load(conn, run_id)
    conn.close()
    return c


def draft(rec: dict, **over) -> Draft:
    r = {**rec, **over}
    return Draft("L1", 1, r["action"], r["target"], r["title"], r["body"], r["trigger_type"], r["trigger_id"],
                 r["claims"])


def failed(rep) -> set[str]:
    return {r["rule"] for r in rep.rule_checks if not r["ok"]} | {
        f"claim:{c['claim']['kind']}" for c in rep.checks if not c["ok"]}


def test_good_cancel_passes_with_recomputed_impact(ctx):
    rep = verify(draft(uber_rec(ctx)), ctx)
    assert rep.verdict == "pass", rep.reasons
    assert rep.impact_annual_cents == 12 * 999
    assert {c["claim"]["kind"]: c["recomputed"] for c in rep.checks} == {
        "recurring_cost": 999, "projected_balance_delta": 6 * 999}


M6 = add_months(AS_OF, 6)
CASES = [  # (pattern, action, kind, figure filter, expected recomputed value or None to take the tool's figure)
    ("UBER", "cancel", "recurring_cost", {"period": "month"}, 999),
    ("UBER", "cancel", "recurring_cost", {"period": "year"}, 11988),
    ("UBER", "cancel", "days_since_parent_spend", {}, None),
    ("UBER", "cancel", "projected_balance_delta", {"month": M6}, 6 * 999),
    ("SPOTIFY", "review_price_increase", "prev_recurring_cost", {}, 1199),
    ("SPOTIFY", "review_price_increase", "price_change", {"period": "month"}, 100),
    ("SPOTIFY", "review_price_increase", "price_change", {"period": "year"}, 1200),
    ("trend", "reduce_spending", "category_spend", {}, None),
    ("trend", "reduce_spending", "excess_spend", {"period": "month"}, None),
    ("trend", "reduce_spending", "percent_change", {}, None),
    ("trend", "reduce_spending", "projected_balance_delta", {"month": M6}, None),
]


@pytest.mark.parametrize("merchant,action,kind,match,expected", CASES)
def test_each_claim_kind_recomputes_and_rejects_beyond_tolerance(ctx, merchant, action, kind, match, expected):
    pid = trend_id(ctx) if merchant == "trend" else pattern_id(ctx, merchant)
    claim = figure(ctx, pid, kind, **match)
    if expected is not None:
        assert claim["value"] == expected  # the tool's figure (from params) agrees with the expected value
    d = Draft("L", 1, action, merchant if merchant != "trend" else "food delivery", "t", "b", "pattern", str(pid),
              [claim])
    [chk] = verify(d, ctx).checks
    assert chk["ok"] and chk["recomputed"] == claim["value"], chk  # recomputed from transactions / engine
    off = {**claim, "value": claim["value"] + chk["tolerance"] + 1}
    [bad] = verify(Draft("L", 1, action, d.target, "t", "b", "pattern", str(pid), [off]), ctx).checks
    assert not bad["ok"] and "recomputed" in bad["note"]


@pytest.mark.parametrize("kind", ["forecast_balance", "forecast_balance_p10"])
def test_forecast_balance_reruns_the_engine(ctx, kind):
    m = add_months(AS_OF, 3)
    pt = next(p for p in ctx.base_points() if p["month"] == m)
    value = pt["balance_p50" if kind == "forecast_balance" else "balance_p10"]
    claim = {"kind": kind, "value": value, "unit": "cents", "source_ref": f"forecast:{ctx.forecast_id}", "month": m,
             "period": None}
    rec = uber_rec(ctx)
    [_, _, chk] = verify(draft(rec, claims=rec["claims"] + [claim]), ctx).checks
    assert chk["ok"] and chk["recomputed"] == value
    claim["source_ref"] = f"forecast:{ctx.forecast_id + 99}"
    [_, _, chk] = verify(draft(rec, claims=rec["claims"] + [claim]), ctx).checks
    assert not chk["ok"] and "this run's forecast" in chk["note"]


def test_wrong_claim_value_and_unclaimed_number_are_both_caught(ctx):
    rep = verify(draft(uber_rec(ctx, cost=1299, extra_text=" That is $150 a year.")), ctx)
    assert rep.verdict == "reject"
    assert failed(rep) == {"claim:recurring_cost", "numbers"}
    fb = rep.feedback()
    assert {"check": "claim recurring_cost (pattern:%d)" % pattern_id(ctx, "UBER"), "claimed": 1299,
            "recomputed": 999, "note": "claimed 1299, recomputed 999"} in fb
    assert any("'$150'" in f["note"] for f in fb if f["check"] == "numbers")


def test_trigger_must_exist_in_this_run(ctx):
    assert "trigger" in failed(verify(draft(uber_rec(ctx), trigger_id="99999"), ctx))


def test_redraft_must_keep_lineage_trigger(ctx):
    d = draft(uber_rec(ctx))
    d.lineage_trigger = str(pattern_id(ctx, "SPOTIFY"))
    assert "trigger" in failed(verify(d, ctx))


def test_claim_citing_another_pattern_fails(ctx):
    rec = uber_rec(ctx)
    rec["claims"][0]["source_ref"] = f"pattern:{pattern_id(ctx, 'SPOTIFY')}"
    rep = verify(draft(rec), ctx)
    assert "claim:recurring_cost" in failed(rep) and "must cite the trigger" in rep.checks[0]["note"]


def test_target_must_name_the_trigger(ctx):
    assert "target" in failed(verify(draft(uber_rec(ctx), target="Netflix"), ctx))
    assert verify(draft(uber_rec(ctx), target="UBER ONE membership"), ctx).verdict == "pass"


def test_cancel_a_used_membership_is_unsupported(ctx):
    pid = pattern_id(ctx, "DASHPASS")
    claims = [figure(ctx, pid, "recurring_cost", period="month")]
    rep = verify(Draft("L", 1, "cancel", "DashPass", "Cancel DashPass ($9.99/month)", "", "pattern", str(pid), claims),
                 ctx)
    assert "action" in failed(rep) and "required_claims" in failed(rep)
    assert "possibly_unused=False" in next(r["note"] for r in rep.rule_checks if r["rule"] == "action")


def test_price_review_needs_a_price_rise_and_reduce_needs_a_rising_trend(ctx):
    uber = pattern_id(ctx, "UBER")
    rep = verify(Draft("L", 1, "review_price_increase", "Uber One", "t", "b", "pattern", str(uber),
                       [figure(ctx, uber, "recurring_cost", period="month")]), ctx)
    assert {"action", "required_claims"} <= failed(rep)
    tid = trend_id(ctx)
    flat = copy.deepcopy(ctx)
    flat.patterns[tid].params["slope_cents_per_month"] = -5
    rep = verify(Draft("L", 1, "reduce_spending", "food delivery", "t", "b", "pattern", str(tid),
                       [figure(ctx, tid, "excess_spend", period="month"),
                        figure(ctx, tid, "projected_balance_delta", month=M6)]), flat)
    assert failed(rep) == {"action"}


def test_projected_delta_only_for_cancel_or_reduce(ctx):
    sp = pattern_id(ctx, "SPOTIFY")
    claim = {"kind": "projected_balance_delta", "value": 0, "unit": "cents", "source_ref": f"forecast:{ctx.forecast_id}",
             "month": M6, "period": None}
    rep = verify(Draft("L", 1, "review_price_increase", "Spotify", "t", "b", "pattern", str(sp),
                       [figure(ctx, sp, "price_change", period="month"), claim]), ctx)
    assert "claim:projected_balance_delta" in failed(rep)


def test_schema_failures(ctx):
    rec = uber_rec(ctx)
    bad = copy.deepcopy(rec)
    bad["claims"][0]["source_ref"] = "somewhere"
    assert {"schema", "required_claims"} <= failed(verify(draft(bad), ctx))
    assert "schema" in failed(verify(draft(rec, action="save_more"), ctx))
    wrong_unit = copy.deepcopy(rec)
    wrong_unit["claims"][0]["unit"] = "percent"
    assert "claim:recurring_cost" in failed(verify(draft(wrong_unit), ctx))


@pytest.mark.parametrize("extra,grounded", [
    (" About $120 a year.", False),  # no year claim in this draft
    (" Over 6 months.", True),  # as_of + 6 is the delta claim's month
    (" Over 5 months.", False),
    (" Unused for 319 days.", False),  # no days claim in this draft
    (" Up 12%.", False),
    (" Since 2026-01.", False),
    (" Since Jan 2026.", False),
    (" Since 2026-01-07.", False),
    (" Your 2 trips and 1 refund.", True),  # other bare integers are ignored (approved rule)
    (" That is $10/month rounded.", True),  # $9.99 shown in whole dollars
    (" That is $9/month.", False),
])
def test_numbers_in_text_must_match_a_claim(ctx, extra, grounded):
    rep = verify(draft(uber_rec(ctx, extra_text=extra)), ctx)
    assert ("numbers" not in failed(rep)) == grounded, rep.reasons


def test_year_claim_grounds_annual_amount_and_twelve_months(ctx):
    rec = uber_rec(ctx, extra_text=" That is $119.88 over 12 months.")
    pid = pattern_id(ctx, "UBER")
    rec["claims"].append(figure(ctx, pid, "recurring_cost", period="year"))
    assert verify(draft(rec), ctx).verdict == "pass"


def test_extract_numbers():
    nums = extract_numbers("Cancel X ($1,234.50/mo, $12 a year, 26%) by 2027-03 or Mar 2027, since 2026-01-07, "
                           "319 days, 6-month window, 3 trips")
    assert [(n.kind, n.value) for n in nums] == [
        ("month", "2027-03"), ("date", "2026-01-07"), ("month", "2027-03"), ("money", 123450), ("money", 1200),
        ("percent", 26.0), ("days", 319), ("months", 6)]


# ---- adv-v2: per-number redraft feedback ----

def hints(ctx, rec):
    return [f for f in verify(draft(rec), ctx).feedback(per_number=True) if f["check"] == "numbers"]


def test_per_number_feedback_gives_nearest_claim_or_remove(ctx):
    rec = uber_rec(ctx, extra_text=" That is $61 sooner, over 3 months, up 12%.")
    by_text = {h["text"]: h for h in hints(ctx, rec)}
    money, span, pct = by_text["$61"], by_text["3 months"], by_text["12%"]
    assert money["nearest_claim"]["kind"] == "projected_balance_delta"
    assert "write it exactly as $59.94 or $60" in money["instruction"]
    assert "only these spans are backed by your claims' months: 6 months" in span["instruction"]
    assert pct["instruction"].startswith("remove the phrase containing '12%'")


def test_wrong_rounding_points_at_the_exact_and_whole_dollar_forms(ctx):
    rec = uber_rec(ctx, extra_text=" About $9 a month.")
    [h] = hints(ctx, rec)
    assert h["nearest_claim"]["value"] == 999 and "exactly as $9.99 or $10" in h["instruction"]


def test_v1_feedback_is_one_numbers_entry(ctx):
    rep = verify(draft(uber_rec(ctx, extra_text=" Up 12%, $9.")), ctx)
    [entry] = [f for f in rep.feedback() if f["check"] == "numbers"]
    assert set(entry) == {"check", "note"}
