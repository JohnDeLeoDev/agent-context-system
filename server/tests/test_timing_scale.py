"test_a_month_of_events_is_read_in_under_two_seconds allowed 2.0 s. On ls it takes well under\nthat; on s1 (a NAS, 3.8x slower on a fixed CPU loop) it takes 2.12 to 2.24 s on an idle node, so\ns1 failed the deploy gate on every commit whatever the gate's budget. The bound keeps its purpose,\ncatching a quadratic slowdown at 100k events, by growing with a calibration measured on the same\nmachine, and never shrinking below its base."
from __future__ import annotations

import pytest

from timing_scale import REFERENCE_SECS, calibrate, scaled_bound


def test_the_reference_is_the_measured_ls_time():
    assert REFERENCE_SECS == pytest.approx(0.0672)


def test_a_machine_as_fast_as_the_reference_keeps_the_base_bound():
    assert scaled_bound(2.0, 0.0672, 0.0672) == pytest.approx(2.0)


def test_a_faster_machine_never_gets_a_tighter_bound():
    assert scaled_bound(2.0, 0.01, 0.0672) == pytest.approx(2.0)


def test_a_slower_machine_gets_a_proportionally_larger_bound():
    assert scaled_bound(2.0, 0.2546, 0.0672) == pytest.approx(2.0 * 0.2546 / 0.0672)
    assert scaled_bound(2.0, 0.2353, 0.0672) == pytest.approx(2.0 * 0.2353 / 0.0672)


def test_the_measured_s1_time_fits_under_its_scaled_bound_with_room():
    assert 2.243 < scaled_bound(2.0, 0.2546, 0.0672) / 2


def test_a_quadratic_slowdown_still_fails_on_every_node():
    'Quadratic at 100k events is minutes, far above the bound even at 4x scale.'
    assert scaled_bound(2.0, 0.2546, 0.0672) < 60.0


def test_the_scale_has_a_ceiling_so_a_broken_calibration_cannot_disable_the_test():
    assert scaled_bound(2.0, 50.0, 0.0672) == pytest.approx(2.0 * 10.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_an_unusable_calibration_keeps_the_base_bound(bad):
    assert scaled_bound(2.0, bad, 0.0672) == pytest.approx(2.0)


def test_calibrate_measures_a_positive_time():
    assert 0.0 < calibrate() < 5.0
