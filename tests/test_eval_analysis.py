"""Backtest arithmetic in eval/analysis.py (pure functions)."""
from eval.analysis import _mape, summarize_forecast


def test_mape_skips_zero_actuals():
    assert _mape([(110, 100), (50, 0), (90, 100)]) == 0.1
    assert _mape([(5, 0)]) is None


def test_summarize_forecast_coverage_and_baselines():
    rec = {"persona": "p", "actual": 100, "engine": 90, "no_trend": 80, "naive_last": 150, "naive_mean": 100,
           "balance_p10": 0, "balance_p90": 10, "categories": {"dining": {"actual": 50, "engine": 40,
                                                                           "naive_last": 50, "naive_mean": 25}}}
    s = summarize_forecast([rec | {"actual_balance": 5}, rec | {"actual_balance": 11}])
    assert (s["mape"], s["no_trend_mape"], s["naive_mape"], s["naive_mean_mape"]) == (0.1, 0.2, 0.5, 0.0)
    assert s["coverage_80"] == 0.5 and s["origins"] == 2
    assert s["mean_error_cents"] == -10 and s["baseline_mean_error_cents"]["naive_last"] == 50
    assert s["per_category"]["dining"] == {"mape": 0.2, "naive_mape": 0.0, "naive_mean_mape": 0.5, "n": 2}
