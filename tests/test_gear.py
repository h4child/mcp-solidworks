"""The involute flank used by create_gear (pure math, no SolidWorks needed)."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("win32com.client", reason="server imports pywin32")

import server  # noqa: E402


def _flank(teeth, module=1.5, alpha_deg=20.0):
    alpha = math.radians(alpha_deg)
    r_pitch = module * teeth / 2
    r_base = r_pitch * math.cos(alpha)
    theta0 = math.pi / (2 * teeth) - (math.tan(alpha) - alpha)
    r_start = max(r_pitch - 1.25 * module, r_base)
    pts = server._gear_flank_points(r_base, r_start, (r_pitch + module) * 1.1, theta0, 12)
    return pts, r_pitch, r_base


@pytest.mark.parametrize("teeth", [18, 20, 40, 54])
def test_flank_is_on_the_pitch_circle_at_half_the_gap_angle(teeth):
    _, r_pitch, r_base = _flank(teeth)
    alpha = math.radians(20.0)
    theta0 = math.pi / (2 * teeth) - (math.tan(alpha) - alpha)
    x, y = server._gear_flank_points(r_base, r_pitch, r_pitch, theta0, 2)[0]
    assert math.hypot(x, y) == pytest.approx(r_pitch)
    assert math.atan2(y, x) == pytest.approx(math.pi / (2 * teeth))


def test_flank_radius_grows_monotonically_and_ends_where_asked():
    pts, _, _ = _flank(20)
    radii = [math.hypot(x, y) for x, y in pts]
    assert radii == sorted(radii)
    assert len(pts) == 12
    assert radii[-1] == pytest.approx((15.0 + 1.5) * 1.1)
