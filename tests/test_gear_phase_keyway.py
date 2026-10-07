"""Meshing phase and hub keyway of create_spur_gear (pure maths, no SolidWorks)."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gear_geometry as gg  # noqa: E402


def _rotated(points, degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [(x * c - y * s, x * s + y * c) for x, y in points]


def _radius_at(points, degrees):
    """Radius of the outline vertex whose polar angle is closest to ``degrees``."""
    target = math.radians(degrees)

    def gap(p):
        return abs((math.atan2(p[1], p[0]) - target + math.pi) % (2 * math.pi) - math.pi)

    x, y = min(points, key=gap)
    return math.hypot(x, y)


@pytest.mark.parametrize("teeth_a,teeth_b", [(20, 40), (18, 54), (17, 51), (20, 21)])
def test_phase_puts_a_gap_in_front_of_the_drivers_tooth(teeth_a, teeth_b):
    """Driver at phase 0 has a tooth on +X; the driven gear, built with
    phase = 180 + 180/teeth, must show a GAP towards the driver (angle 180)."""
    driver = gg.spur_gear_outline(1.5, teeth_a)
    driven = gg.spur_gear_outline(1.5, teeth_b)
    assert _radius_at(driver.points, 0.0) == pytest.approx(driver.table["tip_diameter"] / 2, rel=0.02)
    phased = _rotated(driven.points, 180.0 + 180.0 / teeth_b)
    assert _radius_at(phased, 180.0) == pytest.approx(driven.table["root_diameter"] / 2, rel=0.02)


def test_unphased_even_gear_faces_a_tooth_at_180_so_the_pair_would_clash():
    """The reason phase exists: two even-toothed gears both at phase 0 put tooth
    against tooth on the line of centres."""
    driven = gg.spur_gear_outline(1.5, 40)
    assert _radius_at(driven.points, 180.0) == pytest.approx(driven.table["tip_diameter"] / 2, rel=0.02)


@pytest.mark.parametrize("bore,width,top", [(16.0, 5.0, 10.3), (12.0, 4.0, 7.8), (20.0, 6.0, 12.8)])
def test_keyway_area_matches_numeric_integration(bore, width, top):
    r = bore / 2.0
    steps = 200000
    dx = width / steps
    area = 0.0
    for i in range(steps):
        x = -width / 2.0 + (i + 0.5) * dx
        area += (top - math.sqrt(r * r - x * x)) * dx
    assert gg.keyway_removed_area(r, width, top) == pytest.approx(area, rel=1e-6)


def test_keyway_rejects_a_slot_wider_than_the_bore_or_not_reaching_past_it():
    with pytest.raises(ValueError):
        gg.keyway_removed_area(5.0, 10.0, 8.0)
    with pytest.raises(ValueError):
        gg.keyway_removed_area(5.0, 4.0, 5.0)
