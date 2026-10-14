"""Pure classification metrics for the categorization eval. No I/O, unit-tested in tests/test_eval_metrics.py.

A record is one transaction: truth (primary label), acceptable (lenient label set, includes truth),
pred (category or None when the agent left it uncategorized), confidence, from_cache.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

CALIBRATION_BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.0001)]


@dataclass(frozen=True)
class Rec:
    truth: str
    acceptable: frozenset[str]
    pred: str | None
    confidence: float | None = None
    from_cache: bool | None = None


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def strict_accuracy(recs: list[Rec]) -> float:
    return _rate(sum(r.pred == r.truth for r in recs), len(recs))


def lenient_accuracy(recs: list[Rec]) -> float:
    return _rate(sum(r.pred in r.acceptable for r in recs), len(recs))


def per_category(recs: list[Rec]) -> dict[str, dict]:
    """Strict precision/recall/F1 per label in truth or predictions. Uncategorized = a miss (FN), not an FP."""
    labels = sorted({r.truth for r in recs} | {r.pred for r in recs if r.pred is not None})
    out = {}
    for c in labels:
        tp = sum(r.truth == c and r.pred == c for r in recs)
        fp = sum(r.truth != c and r.pred == c for r in recs)
        fn = sum(r.truth == c and r.pred != c for r in recs)
        p, rc = (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        out[c] = {"precision": round(p, 4), "recall": round(rc, 4), "f1": round(f1, 4), "support": tp + fn}
    return out


def macro_f1(scores: dict[str, dict]) -> float:
    return round(sum(s["f1"] for s in scores.values()) / len(scores), 4) if scores else 0.0


def confusions(recs: list[Rec]) -> list[dict]:
    """(truth, predicted) pairs where they differ, most frequent first; lenient_ok counts rows the lenient score forgives."""
    pairs, forgiven = Counter(), Counter()
    for r in recs:
        if r.pred is not None and r.pred != r.truth:
            pairs[(r.truth, r.pred)] += 1
            forgiven[(r.truth, r.pred)] += r.pred in r.acceptable
    return [{"true": t, "predicted": p, "count": n, "lenient_ok": forgiven[(t, p)]}
            for (t, p), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))]


def calibration(recs: list[Rec]) -> list[dict]:
    """Does stated confidence match observed accuracy? Uses categorized rows only."""
    out = []
    for lo, hi in CALIBRATION_BUCKETS:
        b = [r for r in recs if r.confidence is not None and lo <= r.confidence < hi]
        out.append({"bucket": f"[{lo:.2f}, {min(hi, 1.0):.2f}{']' if hi > 1 else ')'}", "n": len(b),
                    "accuracy": strict_accuracy(b) if b else None,
                    "mean_confidence": round(sum(r.confidence for r in b) / len(b), 4) if b else None})
    return out


def source_split(recs: list[Rec]) -> dict:
    """Cache hit rate and accuracy split by where the label came from."""
    done = [r for r in recs if r.pred is not None]
    cached = [r for r in done if r.from_cache]
    llm = [r for r in done if not r.from_cache]
    return {
        "cache_hit_rate": _rate(len(cached), len(done)),
        "from_cache": {"n": len(cached), "strict": strict_accuracy(cached), "lenient": lenient_accuracy(cached)},
        "from_llm": {"n": len(llm), "strict": strict_accuracy(llm), "lenient": lenient_accuracy(llm)},
    }
