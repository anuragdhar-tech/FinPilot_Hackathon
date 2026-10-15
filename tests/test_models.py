from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from backend import models as m


def test_category_literal_matches_tuple():
    assert get_args(m.Category) == m.CATEGORIES


def test_simulate_request_requires_exactly_one():
    m.SimulateRequest(text="trip next month ~$1,500")
    with pytest.raises(ValidationError):
        m.SimulateRequest()
    scenario = {"items": [{"kind": "one_off", "label": "trip", "amount_cents": -150000, "start_month": "2026-11"}]}
    with pytest.raises(ValidationError):
        m.SimulateRequest(text="x", scenario=scenario)


def test_pattern_discriminated_union():
    p = TypeAdapter(m.Pattern).validate_python({
        "id": 1, "type": "anomaly", "title": "Spike", "evidence_txn_ids": [7],
        "params": {"txn_id": 7, "kind": "spike", "date": "2026-06-13", "amount_cents": -90000,
                   "baseline_cents": -8000},
    })
    assert isinstance(p, m.AnomalyPattern)
    with pytest.raises(ValidationError):  # mom_shift was dropped from the contract (Oct 16)
        TypeAdapter(m.Pattern).validate_python({"id": 2, "type": "mom_shift", "title": "x", "evidence_txn_ids": [],
                                                "params": {}})


def test_claim_source_ref_format():
    m.Claim(kind="monthly_savings", value=1599, unit="cents", source_ref="pattern:12")
    with pytest.raises(ValidationError):
        m.Claim(kind="monthly_savings", value=1599, unit="cents", source_ref="somewhere")


def test_month_format():
    with pytest.raises(ValidationError):
        m.ScenarioItem(kind="one_off", label="x", amount_cents=-1, start_month="2026-13")
