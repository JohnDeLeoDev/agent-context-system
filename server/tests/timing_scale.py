'Scale a wall-clock bound to the speed of the machine running it.\n\nA test that asserts "under N seconds" holds on a fast machine and fails on a slow one, and the\ndeploy gate then blocks that node on every commit whatever its time budget. The bound grows with\na calibration measured on the same machine (a fixed pure-Python loop), never shrinks below its\nbase, and stops growing at MAX_SCALE so a broken calibration cannot disable the check.'
from __future__ import annotations

import math
import time


REFERENCE_SECS = 0.0672
MAX_SCALE = 10.0


def calibrate() -> float:
    'Seconds for a fixed CPU-bound loop, best of 5 so a busy moment does not inflate it.'
    best = math.inf
    for _ in range(5):
        start = time.perf_counter()
        x = 0
        for i in range(2_000_000):
            x += i * i % 7
        best = min(best, time.perf_counter() - start)
    return best


def scaled_bound(base: float, calibration: float, reference: float = REFERENCE_SECS) -> float:
    '`base` seconds on a machine as fast as the reference; more on a slower one. An unusable\n    calibration (zero, negative, not a number, infinite) leaves the base.'
    if not (math.isfinite(calibration) and calibration > 0 and reference > 0):
        return base
    return base * min(MAX_SCALE, max(1.0, calibration / reference))
