"""Shared score contract: raw pillars are independent of experiment weights."""
import math

SCORING_VERSION = "v3-normalized-replay"
RAW_MAX = {"structure": 25, "momentum": 30, "flow": 20, "derivatives": 15}
BASE_WEIGHTS = {**RAW_MAX, "news": 10}
PILLAR_COLUMNS = {"structure": "structure_score", "momentum": "momentum_score",
                  "flow": "flow_score", "derivatives": "derivative_score"}


def score_snapshot(snapshot, weights=None):
    weights = BASE_WEIGHTS if weights is None else weights
    if set(weights) != set(BASE_WEIGHTS) or any(
        not math.isfinite(float(v)) or float(v) < 0 for v in weights.values()
    ) or not math.isclose(sum(weights.values()), 100) or weights["news"] != 10:
        raise ValueError("Weights must be nonnegative, total 100, with news=10")
    contributions = {}
    for pillar, column in PILLAR_COLUMNS.items():
        value = float(snapshot[column])
        if not math.isfinite(value):
            raise ValueError(f"Invalid raw pillar: {column}")
        contributions[pillar] = min(RAW_MAX[pillar], max(0, value)) / RAW_MAX[pillar] * weights[pillar]
    macro = float(snapshot.get("macro_score", 5)) - 5
    penalty = float(snapshot.get("score_penalty", 0))
    if not math.isfinite(macro) or not math.isfinite(penalty) or penalty < 0:
        raise ValueError("Invalid macro adjustment or penalty")
    # Reserve 10 points for optional news; macro is an explicit modifier.
    total = min(90, max(0, sum(contributions.values()) + macro - penalty))
    return round(total, 4), {**contributions, "macro_adjustment": macro, "penalty": -penalty}
