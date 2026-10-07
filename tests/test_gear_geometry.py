"""
Tests for the involute-gear geometry, with no SolidWorks and no pywin32.

What these are for: "the gear came out smooth" was never a COM bug. It was
geometry -- a tooth profile that never existed, or existed and got collapsed.
``gear_geometry`` is where the profile is decided, so this is where it can be
checked properly: the meshing condition (tooth thickness at the pitch circle),
the involute itself against its own definition, the outline's closure and the
fact that it cannot cross itself, and the area that ``create_spur_gear`` later
compares the real solid against.

Run with:  python -m pytest tests/test_gear_geometry.py -q
"""

import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gear_geometry as gg  # noqa: E402

# Every size this generator is expected to handle: coarse and fine modules, a
# pinion near the undercut limit, and tooth counts on both sides of z=42, where
# the root circle passes the base circle and the fillet construction changes.
SIZES = [
    (2.0, 6), (2.0, 8), (2.0, 12), (2.0, 17), (2.0, 20), (2.0, 25),
    (2.0, 40), (2.0, 42), (2.0, 50), (2.0, 80), (2.0, 127),
    (0.5, 20), (1.0, 31), (5.0, 18), (10.0, 20),
]


def unwrapped_polar_angles(points):
    """The outline's polar angle, continued across the +/-pi branch cut."""
    angles = [math.atan2(y, x) for x, y in points]
    out = [angles[0]]
    for angle in angles[1:]:
        turns = round((out[-1] - angle) / (2 * math.pi))
        out.append(angle + turns * 2 * math.pi)
    return out


# ---------------------------------------------------------------------------
# The involute function itself
# ---------------------------------------------------------------------------


def test_the_involute_is_flat_on_its_own_base_circle():
    assert gg.involute_angle(10.0, 10.0) == 0.0
    assert gg.involute_angle(10.0, 9.0) == 0.0


def test_the_involute_matches_its_definition_at_the_pressure_angle():
    """At the pitch circle the involute has swept inv(alpha) = tan(a) - a.

    This identity is the whole reason an involute gear meshes at a constant
    ratio, so it is checked against the angle rather than against itself.
    """
    module, teeth, alpha = 2.0, 20, math.radians(20.0)
    pitch_radius = module * teeth / 2.0
    base_radius = pitch_radius * math.cos(alpha)
    assert gg.involute_angle(base_radius, pitch_radius) == pytest.approx(
        math.tan(alpha) - alpha, rel=1e-12)


def test_the_involute_unwinds_a_string_of_the_right_length():
    """Its defining property: the tangent length equals the arc rolled over."""
    base_radius = 18.0
    for radius in (18.5, 20.0, 25.0, 40.0):
        angle = gg.involute_angle(base_radius, radius)
        tangent_length = math.sqrt(radius ** 2 - base_radius ** 2)
        rolled_arc = base_radius * (angle + math.acos(base_radius / radius))
        assert tangent_length == pytest.approx(rolled_arc, rel=1e-12)


# ---------------------------------------------------------------------------
# The dimension table
# ---------------------------------------------------------------------------


def test_the_table_is_the_standard_one():
    table = gg.gear_table(2.0, 20)
    assert table["pitch_diameter"] == pytest.approx(40.0)
    assert table["tip_diameter"] == pytest.approx(44.0)       # + 2*1.0*m
    assert table["root_diameter"] == pytest.approx(35.0)      # - 2*1.25*m
    assert table["base_diameter"] == pytest.approx(40.0 * math.cos(math.radians(20)))
    assert table["circular_pitch"] == pytest.approx(math.pi * 2.0)
    assert table["tooth_thickness_at_pitch_circle"] == pytest.approx(math.pi)
    assert table["angular_pitch_degrees"] == pytest.approx(18.0)


def test_the_centre_distance_is_the_meshing_one():
    assert gg.center_distance(2.0, 20, 40) == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# The outline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_outline_has_exactly_the_teeth_that_were_asked_for(module, teeth):
    profile = gg.spur_gear_outline(module, teeth)
    assert len(profile.points) == profile.points_per_tooth * teeth
    # Every tooth is the same tooth, rotated by one angular pitch.
    step = profile.points_per_tooth
    first = profile.points[:step]
    turn = 2 * math.pi / teeth
    for index in range(1, teeth):
        rotated = [
            (x * math.cos(index * turn) - y * math.sin(index * turn),
             x * math.sin(index * turn) + y * math.cos(index * turn))
            for x, y in first
        ]
        for expected, got in zip(rotated, profile.points[index * step:(index + 1) * step]):
            assert math.dist(expected, got) < 1e-9


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_outline_cannot_cross_itself(module, teeth):
    """A self-intersecting profile is a failed extrusion.

    With several hundred segments it is not something to eyeball, so it is
    proved structurally instead: the polar angle never decreases and completes
    exactly one turn, which makes the outline star-shaped about the gear axis.
    """
    profile = gg.spur_gear_outline(module, teeth)
    angles = unwrapped_polar_angles(profile.points)
    steps = [b - a for a, b in zip(angles, angles[1:])]
    assert min(steps) >= -1e-12, f"the outline doubles back at step {steps.index(min(steps))}"
    assert 0 < angles[-1] - angles[0] < 2 * math.pi


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_outline_stays_between_the_root_and_tip_circles(module, teeth):
    profile = gg.spur_gear_outline(module, teeth)
    radii = [math.hypot(x, y) for x, y in profile.points]
    assert min(radii) == pytest.approx(profile.table["root_diameter"] / 2.0, abs=1e-9)
    assert max(radii) == pytest.approx(profile.effective_tip_diameter / 2.0, abs=1e-9)
    assert profile.effective_tip_diameter <= profile.table["tip_diameter"] + 1e-9


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_tooth_gaps_really_remove_material(module, teeth):
    """The number create_spur_gear uses to decide the gear is not a disc.

    If this ever reached 1.0 the tool would have nothing to compare a smooth
    disc against, which is the exact failure it exists to catch.
    """
    profile = gg.spur_gear_outline(module, teeth)
    assert profile.area_mm2 < profile.blank_area_mm2
    assert 0.4 < profile.tooth_area_fraction < 0.98


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_tooth_is_the_standard_thickness_at_the_pitch_circle(module, teeth):
    """pi*m/2 at the pitch circle is what makes two gears mesh.

    Measured off the generated outline, not off the formula that drew it: the
    pitch circle crosses a flank between two sampled points, so the crossing
    is interpolated and compared with the standard thickness.
    """
    profile = gg.spur_gear_outline(module, teeth, flank_points=40)
    pitch_radius = profile.table["pitch_diameter"] / 2.0
    if pitch_radius >= profile.effective_tip_diameter / 2.0:
        pytest.skip("pointed teeth: the flank does not reach the pitch circle")
    crossings = []
    points = profile.points[:profile.points_per_tooth + 1]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        r1, r2 = math.hypot(x1, y1), math.hypot(x2, y2)
        if (r1 - pitch_radius) * (r2 - pitch_radius) < 0:
            t = (pitch_radius - r1) / (r2 - r1)
            crossings.append(math.atan2(y1 + t * (y2 - y1), x1 + t * (x2 - x1)))
    assert len(crossings) == 2, f"expected one tooth's two flanks, got {len(crossings)}"
    thickness = abs(crossings[1] - crossings[0]) * pitch_radius
    assert thickness == pytest.approx(math.pi * module / 2.0, rel=2e-3)


@pytest.mark.parametrize("module,teeth", SIZES)
def test_the_flank_chords_stay_close_to_the_true_involute(module, teeth):
    """The accuracy claim the tool reports, checked against the exact curve."""
    coarse = gg.spur_gear_outline(module, teeth, flank_points=4)
    fine = gg.spur_gear_outline(module, teeth, flank_points=16)
    assert fine.chord_error_mm < coarse.chord_error_mm
    # Chord error falls with the square of the sample count; 16 vs 4 samples is
    # a factor of 16 in theory, and well over 4 even allowing for the uneven
    # curvature along the flank.
    assert fine.chord_error_mm < coarse.chord_error_mm / 4.0
    assert fine.chord_error_mm < 0.02 * module


def test_a_finer_profile_converges_on_the_same_cross_section():
    """Sampling the flanks harder may not change the gear it approximates."""
    coarse = gg.spur_gear_outline(2.0, 20, flank_points=5)
    fine = gg.spur_gear_outline(2.0, 20, flank_points=40)
    assert fine.area_mm2 == pytest.approx(coarse.area_mm2, rel=5e-3)
    assert fine.area_mm2 > coarse.area_mm2  # chords cut inside the real flank


# ---------------------------------------------------------------------------
# The root fillet
# ---------------------------------------------------------------------------


def test_the_root_fillet_is_tangent_to_the_root_circle():
    """A fillet that is not tangent is a notch, which is where teeth break.

    Reconstructed from the fillet radius alone: a circle of radius rho tangent
    to the root circle from outside has its centre at rf + rho, and tangent to
    the radial flank means it sits asin(rho/(rf+rho)) away from it. Every point
    of the arc the generator emitted must then be exactly rho from that centre.
    """
    fillet_points = 16
    profile = gg.spur_gear_outline(2.0, 20, fillet_points=fillet_points)
    rho = profile.root_fillet_radius
    assert rho > 0
    root_radius = profile.table["root_diameter"] / 2.0
    half_angle_at_base = (math.pi / (2 * 20)
                          + math.tan(math.radians(20)) - math.radians(20))
    delta = math.asin(rho / (root_radius + rho))
    centre_angle = -(half_angle_at_base + delta)
    centre = ((root_radius + rho) * math.cos(centre_angle),
              (root_radius + rho) * math.sin(centre_angle))
    arc = profile.points[:fillet_points + 1]
    for point in arc:
        assert math.dist(point, centre) == pytest.approx(rho, abs=1e-9)
    assert math.hypot(*arc[0]) == pytest.approx(root_radius, abs=1e-9)
    # ... and it hands over to the flank below the involute, not on top of it.
    assert math.hypot(*arc[-1]) < profile.table["base_diameter"] / 2.0


def test_the_root_fillet_is_reduced_rather_than_overrunning_the_base_circle():
    """It must end below the involute, whatever was asked for."""
    profile = gg.spur_gear_outline(2.0, 20, root_fillet_coefficient=5.0)
    assert 0 < profile.root_fillet_radius < 5.0 * 2.0
    assert "reduced" in profile.root_fillet_note
    base_radius = profile.table["base_diameter"] / 2.0
    root_radius = profile.table["root_diameter"] / 2.0
    tangent_radius = math.sqrt(root_radius ** 2 + 2 * root_radius * profile.root_fillet_radius)
    assert tangent_radius < base_radius


def test_no_fillet_is_asked_for_means_no_fillet_and_no_complaint():
    profile = gg.spur_gear_outline(2.0, 20, root_fillet_coefficient=0.0)
    assert profile.root_fillet_radius == 0.0
    assert not any("root fillet was cut" in w for w in profile.warnings)


def test_a_gear_whose_root_passes_its_base_circle_says_why_it_has_no_fillet():
    """Above roughly 42 teeth the involute reaches the root circle directly,
    so there is no radial segment left to fillet against. That is a real
    limitation of this construction and has to be reported, not hidden."""
    profile = gg.spur_gear_outline(2.0, 60)
    assert profile.root_fillet_radius == 0.0
    assert "base circle" in profile.root_fillet_note
    assert any("no root fillet was cut" in w for w in profile.warnings)


# ---------------------------------------------------------------------------
# What it refuses, and what it warns about
# ---------------------------------------------------------------------------


def test_an_undercut_pinion_is_built_but_flagged():
    profile = gg.spur_gear_outline(2.0, 12)
    assert any("undercut" in w for w in profile.warnings)


def test_a_pointed_tooth_is_reported_and_not_drawn_past_its_own_apex():
    """Few teeth plus a tall addendum runs the flanks together below the tip
    diameter. Drawing to the nominal tip anyway is how a profile crosses
    itself, so the tip is truncated to the apex and the result says so."""
    profile = gg.spur_gear_outline(1.0, 6, addendum_coefficient=1.5,
                                 dedendum_coefficient=1.6)
    assert profile.effective_tip_diameter < profile.table["tip_diameter"]
    assert profile.tip_land_mm == 0.0
    assert any("POINTED" in w for w in profile.warnings)
    angles = unwrapped_polar_angles(profile.points)
    assert min(b - a for a, b in zip(angles, angles[1:])) >= -1e-12


@pytest.mark.parametrize("kwargs,message", [
    ({"module": 0}, "module must be positive"),
    ({"module": -1}, "module must be positive"),
    ({"teeth": 5}, "teeth must be at least 6"),
    ({"pressure_angle": 5}, "pressure_angle"),
    ({"pressure_angle": 40}, "pressure_angle"),
    ({"dedendum_coefficient": 0.9}, "dedendum_coefficient must exceed"),
    ({"flank_points": 2}, "flank_points must be at least 3"),
    ({"tip_points": 0}, "tip_points must be at least 1"),
])
def test_an_impossible_gear_is_refused_with_a_reason(kwargs, message):
    arguments = {"module": 2.0, "teeth": 20}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=message):
        gg.spur_gear_outline(**arguments)


def test_a_dedendum_that_eats_the_whole_blank_is_refused():
    with pytest.raises(ValueError, match="eats the whole blank"):
        gg.spur_gear_outline(2.0, 6, dedendum_coefficient=3.5)


# ---------------------------------------------------------------------------
# polygon_area, which the volume check depends on
# ---------------------------------------------------------------------------


def test_polygon_area_is_the_shoelace_area_either_way_round():
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert gg.polygon_area(square) == pytest.approx(4.0)
    assert gg.polygon_area(list(reversed(square))) == pytest.approx(4.0)


def test_polygon_area_converges_on_a_circle():
    for count in (64, 256, 1024):
        points = [(math.cos(2 * math.pi * i / count), math.sin(2 * math.pi * i / count))
                  for i in range(count)]
        assert gg.polygon_area(points) == pytest.approx(math.pi, rel=10.0 / count ** 2)
