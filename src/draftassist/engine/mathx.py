"""The log-odds helpers every engine module uses. Lowest level: imports nothing from the engine, so model.py,
synergy.py, comp.py and traits.py can all share one definition."""
from __future__ import annotations

import math

LOGIT_EPS = 1e-4  # probabilities are clamped to [eps, 1 - eps] so logit stays finite


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def logit(p: float, eps: float = LOGIT_EPS) -> float:
    """log(p / (1 - p)), with p clamped to [eps, 1 - eps]. Pass a smaller eps where extreme probabilities are
    meaningful (the synergy score's expectation, engine/synergy.py)."""
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))
