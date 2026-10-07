"""
Pure involute-spur-gear geometry for the SolidWorks MCP, with no COM dependency.

Why this module exists
----------------------
A gear modelled through the generic sketch tools comes out as a smooth disc.
That is not one bug, it is the arithmetic: a single involute flank needs a
dozen points a tenth of a millimetre apart, every ``draw_*`` call goes through
SolidWorks' inference engine whose snap radius is measured in SCREEN PIXELS
(see ``.claude/knowledge/verificacao_e_qa.md`` section 0), and a tooth-scale
point next to its neighbour is exactly what that engine collapses -- silently,
by moving the point, not by refusing the call. One flattened tooth gap cut and
patterned 20 times around the blank is a blank again.

So the whole outline -- every tooth, closed -- is computed HERE, in plain
Python, and handed to SolidWorks as one profile with the inference engine
switched off (``SetAddToDB``). This module owns the involute; ``server.py``
owns only the sketch call.

Units: millimetres and degrees in, millimetres and degrees out, like
``alfa_drawing``. ``server.py`` converts to the metres the API wants.

Reference geometry is ISO 53 standard full-depth: addendum 1.0*m, dedendum
1.25*m, 20-degree pressure angle, no profile shift, zero backlash. The flanks
are true involutes sampled into chords, and ``flank_chord_error`` reports how
far those chords sit from the real curve so the caller can say what it built.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# A tooth gap narrower than this fraction of the angular pitch means the
# dedendum/fillet asked for does not fit between two teeth.
_MIN_GAP_FRACTION = 0.02

# ISO 53 rack tip radius, as a multiple of the module: the root fillet a
# standard hob actually leaves behind.
DEFAULT_ROOT_FILLET_COEFFICIENT = 0.38

# Below this, 20-degree full-depth teeth are undercut by the generating rack
# and the involute flank is cut away near the root (ISO: 2*ha/sin^2(alpha)).
MIN_TEETH_WITHOUT_UNDERCUT = 17


def involute_angle(base_radius: float, radius: float) -> float:
    """Angle swept along an involute between its base circle and ``radius``.

    This is inv(alpha_r) = tan(alpha_r) - alpha_r with cos(alpha_r) = rb/r --
    the classic involute function. Below the base circle the involute does not
    exist, and 0.0 is the honest answer there (the caller joins the root with
    a radial line instead).
    """
    if radius <= base_radius:
        return 0.0
    cos_a = max(-1.0, min(1.0, base_radius / radius))
    alpha = math.acos(cos_a)
    return math.tan(alpha) - alpha


def involute_point(base_radius: float, radius: float) -> tuple:
    """A point of the involute at ``radius``, as (x, y), starting on +X."""
    angle = involute_angle(base_radius, radius)
    return (radius * math.cos(angle), radius * math.sin(angle))


def polygon_area(points) -> float:
    """Shoelace area of a closed polygon given as [(x, y), ...]."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, list(points[1:]) + [points[0]]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def center_distance(module: float, teeth_a: int, teeth_b: int) -> float:
    """Standard (zero-backlash, no profile shift) centre distance of a pair."""
    return module * (teeth_a + teeth_b) / 2.0


def gear_table(module: float, teeth: int, pressure_angle: float = 20.0,
               addendum_coefficient: float = 1.0,
               dedendum_coefficient: float = 1.25) -> dict:
    """The dimension table of a standard external spur gear, in millimetres.

    Every diameter a drawing or a mating part needs, derived from the three
    numbers that actually define the gear (module, teeth, pressure angle),
    so nothing downstream has to re-derive -- or invent -- them.
    """
    alpha = math.radians(pressure_angle)
    pitch_diameter = module * teeth
    return {
        "module": module,
        "teeth": teeth,
        "pressure_angle": pressure_angle,
        "pitch_diameter": pitch_diameter,
        "base_diameter": pitch_diameter * math.cos(alpha),
        "tip_diameter": pitch_diameter + 2.0 * addendum_coefficient * module,
        "root_diameter": pitch_diameter - 2.0 * dedendum_coefficient * module,
        "addendum": addendum_coefficient * module,
        "dedendum": dedendum_coefficient * module,
        "whole_depth": (addendum_coefficient + dedendum_coefficient) * module,
        "circular_pitch": math.pi * module,
        "tooth_thickness_at_pitch_circle": math.pi * module / 2.0,
        "angular_pitch_degrees": 360.0 / teeth,
    }


@dataclass
class GearProfile:
    """One closed outline of a spur gear, plus what it is and how exact it is."""

    points: list                      # [(x_mm, y_mm), ...] counter-clockwise, closed
    table: dict                       # gear_table() of this gear
    area_mm2: float                   # cross-section of the toothed outline
    blank_area_mm2: float             # a smooth disc at the tip diameter
    tip_land_mm: float                # crest width at the tip; 0.0 = pointed tooth
    effective_tip_diameter: float     # < tip_diameter when the tooth went pointed
    root_fillet_radius: float         # 0.0 when none could be fitted
    root_fillet_note: str
    chord_error_mm: float             # worst gap between a flank chord and the involute
    points_per_tooth: int
    warnings: list = field(default_factory=list)

    @property
    def tooth_area_fraction(self) -> float:
        """How much of the blank the tooth gaps remove. The "is it smooth?" number.

        A real 20-degree gear lands around 0.80-0.92 here. 1.0 is a disc, which
        is precisely the failure this whole module exists to prevent, so it is
        reported as a measurement and not assumed.
        """
        return self.area_mm2 / self.blank_area_mm2


def _polar(radius: float, angle: float) -> tuple:
    return (radius * math.cos(angle), radius * math.sin(angle))


def _arc_points(center, start_point, end_point, segments: int) -> list:
    """Interior points of the arc from ``start_point`` to ``end_point``.

    Both endpoints are assumed equidistant from ``center`` (they are: every
    caller here builds them that way). The short way round is used, which is
    always the right one for a fillet or a tip land.
    """
    cx, cy = center
    radius = math.hypot(start_point[0] - cx, start_point[1] - cy)
    a0 = math.atan2(start_point[1] - cy, start_point[0] - cx)
    a1 = math.atan2(end_point[1] - cy, end_point[0] - cx)
    sweep = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    return [
        (cx + radius * math.cos(a0 + sweep * i / segments),
         cy + radius * math.sin(a0 + sweep * i / segments))
        for i in range(1, segments)
    ]


def _flank_radii(base_radius: float, start_radius: float, end_radius: float,
                 count: int) -> list:
    """Radii sampling one flank, spaced evenly along the involute's roll angle.

    Equal steps in radius bunch the samples where the curve is straight and
    starve the root, where it bends most; equal steps in the roll angle
    tan(alpha_r) -- the involute's natural parameter -- spread the chord error
    evenly instead.
    """
    low = max(start_radius, base_radius)
    t_low = math.tan(math.acos(max(-1.0, min(1.0, base_radius / low)))) if low > base_radius else 0.0
    t_high = math.tan(math.acos(max(-1.0, min(1.0, base_radius / end_radius))))
    radii = []
    for index in range(count):
        t = t_low + (t_high - t_low) * index / (count - 1)
        alpha = math.atan(t)
        radii.append(base_radius / math.cos(alpha))
    radii[0], radii[-1] = low, end_radius
    return radii


def _fit_root_fillet(root_radius: float, base_radius: float,
                     requested: float, gap_half_angle_available: float) -> tuple:
    """Largest fillet radius that fits in the root, and why it is not bigger.

    The fillet is tangent to the root circle and to the radial segment that
    joins the root to the base circle. Two things bound it: it must end below
    the base circle (where the involute takes over), and its tangent point on
    the root circle must stay inside the tooth gap.
    """
    if requested <= 0.0:
        return 0.0, "no root fillet was asked for"
    # Tangency to a radial line: |C| = rf + rho and the tangent point on that
    # line sits at sqrt(rf^2 + 2*rf*rho). Requiring that below rb bounds rho.
    if base_radius <= root_radius:
        return 0.0, ("the root circle lies above the base circle at this tooth "
                     "count, so the involute reaches the root directly and there "
                     "is no radial segment to fillet against")
    by_base_circle = (base_radius ** 2 - root_radius ** 2) / (2.0 * root_radius)
    # delta = asin(rho/(rf+rho)) is how far the tangent point swings into the
    # gap; it has to stay within the room the gap leaves.
    sin_max = math.sin(max(0.0, gap_half_angle_available))
    by_gap = (root_radius * sin_max / (1.0 - sin_max)) if sin_max < 1.0 else requested
    radius = min(requested, by_base_circle * 0.98, by_gap * 0.98)
    if radius <= 0.0:
        return 0.0, "no fillet fits between the root circle and the base circle"
    if radius < requested * 0.999:
        limit = "the base circle" if by_base_circle * 0.98 <= by_gap * 0.98 else "the tooth gap"
        return radius, (f"reduced from {requested:.4f} mm to {radius:.4f} mm by {limit}")
    return radius, f"{radius:.4f} mm, tangent to the root circle and the radial flank"


def _pointed_tip_radius(base_radius: float, half_angle_at_base: float,
                        tip_radius: float) -> float:
    """Radius where the two involute flanks of one tooth meet (a pointed tip).

    Bisection, because inv() has no closed-form inverse. Called only when the
    flank angle has already been shown to run out before the tip diameter.
    """
    low, high = base_radius, tip_radius
    for _ in range(80):
        mid = (low + high) / 2.0
        if half_angle_at_base - involute_angle(base_radius, mid) > 0.0:
            low = mid
        else:
            high = mid
    return low


def flank_chord_error(base_radius: float, radii: list, half_angle_at_base: float,
                      samples_per_chord: int = 12) -> float:
    """Worst distance from a sampled flank chord to the true involute.

    This is the accuracy claim of the whole profile, so it is measured against
    the analytic curve rather than asserted from the sample count.
    """
    worst = 0.0

    def point_at(radius: float) -> tuple:
        angle = -(half_angle_at_base - involute_angle(base_radius, radius))
        return _polar(radius, angle)

    for r_low, r_high in zip(radii, radii[1:]):
        p0, p1 = point_at(r_low), point_at(r_high)
        chord = math.dist(p0, p1)
        if chord <= 0.0:
            continue
        for index in range(1, samples_per_chord):
            radius = r_low + (r_high - r_low) * index / samples_per_chord
            px, py = point_at(radius)
            # Perpendicular distance from the exact point to the chord.
            cross = abs((p1[0] - p0[0]) * (py - p0[1]) - (p1[1] - p0[1]) * (px - p0[0]))
            worst = max(worst, cross / chord)
    return worst


def spur_gear_outline(module: float, teeth: int, pressure_angle: float = 20.0,
                      addendum_coefficient: float = 1.0,
                      dedendum_coefficient: float = 1.25,
                      root_fillet_coefficient: float = DEFAULT_ROOT_FILLET_COEFFICIENT,
                      flank_points: int = 7, tip_points: int = 3,
                      root_points: int = 3, fillet_points: int = 3) -> GearProfile:
    """The complete closed outline of an external involute spur gear, in mm.

    One polygon, counter-clockwise, every tooth in it, ready to be drawn as a
    single closed profile and extruded once. The outline's polar angle never
    decreases from point to point and comes back to its start after exactly one
    turn (it holds still only along the radial segment at the root), so the
    outline is star-shaped about the gear axis and therefore cannot cross
    itself -- a self-intersecting profile is a failed extrusion, and with
    several hundred segments it is not something to eyeball.
    """
    if module <= 0:
        raise ValueError(f"module must be positive, got {module}.")
    if teeth < 6:
        raise ValueError(
            f"teeth must be at least 6, got {teeth}. Below that an involute "
            f"flank has almost no usable height and the teeth are pointed; a "
            f"real drive with that ratio uses a profile-shifted pinion, which "
            f"this generator does not cut."
        )
    if not 10.0 <= pressure_angle <= 35.0:
        raise ValueError(
            f"pressure_angle must be between 10 and 35 degrees, got {pressure_angle}."
        )
    if dedendum_coefficient <= addendum_coefficient:
        raise ValueError(
            "dedendum_coefficient must exceed addendum_coefficient, otherwise "
            "the mating tooth tip hits this gear's root."
        )
    if flank_points < 3:
        raise ValueError(f"flank_points must be at least 3, got {flank_points}.")
    for name, value in (("tip_points", tip_points), ("root_points", root_points),
                        ("fillet_points", fillet_points)):
        if value < 1:
            raise ValueError(f"{name} must be at least 1, got {value}.")

    table = gear_table(module, teeth, pressure_angle,
                       addendum_coefficient, dedendum_coefficient)
    alpha = math.radians(pressure_angle)
    base_radius = table["base_diameter"] / 2.0
    tip_radius = table["tip_diameter"] / 2.0
    root_radius = table["root_diameter"] / 2.0
    if root_radius <= 0.0:
        raise ValueError(
            f"a {teeth}-tooth gear of module {module} has a root diameter of "
            f"{table['root_diameter']:.3f} mm: the dedendum eats the whole "
            f"blank. Raise the tooth count or the module."
        )
    angular_pitch = 2.0 * math.pi / teeth
    warnings = []

    # Angle from a tooth's centreline to its flank, at the base circle. At the
    # pitch circle this leaves exactly half the standard tooth thickness,
    # pi*m/2, which is what makes the gear mesh with any other gear of the
    # same module and pressure angle.
    half_angle_at_base = math.pi / (2.0 * teeth) + (math.tan(alpha) - alpha)

    # --- tip: full land, or a tooth that ran out of flank before the tip ---
    half_angle_at_tip = half_angle_at_base - involute_angle(base_radius, tip_radius)
    effective_tip_radius = tip_radius
    if half_angle_at_tip <= 1e-9:
        effective_tip_radius = _pointed_tip_radius(base_radius, half_angle_at_base, tip_radius)
        half_angle_at_tip = 0.0
        warnings.append(
            f"the teeth are POINTED: the two involute flanks meet at diameter "
            f"{2 * effective_tip_radius:.3f} mm, below the nominal tip diameter "
            f"{table['tip_diameter']:.3f} mm, so there is no tip land to chamfer "
            f"and the addendum is effectively {effective_tip_radius - table['pitch_diameter'] / 2:.3f} mm"
        )
    tip_land = 2.0 * half_angle_at_tip * effective_tip_radius
    if 0.0 < tip_land < 0.2 * module:
        warnings.append(
            f"the tip land is only {tip_land:.3f} mm ({tip_land / module:.2f}*module); "
            f"below about 0.25*module the crest is too thin to survive hardening"
        )
    if teeth < MIN_TEETH_WITHOUT_UNDERCUT and pressure_angle <= 20.0:
        warnings.append(
            f"{teeth} teeth at {pressure_angle:g} degrees is below the undercut "
            f"limit of {MIN_TEETH_WITHOUT_UNDERCUT}: a real hobbed gear would have "
            f"its flank undercut near the root, weakening the tooth and shortening "
            f"the line of contact. This outline draws the full involute instead, "
            f"so it is stronger than the part a hob would actually cut"
        )

    # --- root fillet ---
    gap_half_angle_available = max(0.0, angular_pitch / 2.0 - half_angle_at_base)
    fillet_radius, fillet_note = _fit_root_fillet(
        root_radius, base_radius, root_fillet_coefficient * module,
        gap_half_angle_available * (1.0 - _MIN_GAP_FRACTION),
    )

    # --- one tooth, as angles relative to its own centreline ---
    flank_start_radius = max(root_radius, base_radius)
    radii = _flank_radii(base_radius, flank_start_radius, effective_tip_radius, flank_points)
    chord_error = flank_chord_error(base_radius, radii, half_angle_at_base)

    def flank_angle(radius: float) -> float:
        return half_angle_at_base - involute_angle(base_radius, radius)

    right: list = []  # the -angle half of the tooth, from the root upwards
    if fillet_radius > 0.0:
        delta = math.asin(fillet_radius / (root_radius + fillet_radius))
        tangent_on_line = math.sqrt(root_radius ** 2 + 2.0 * root_radius * fillet_radius)
        root_corner_angle = -(half_angle_at_base + delta)
        center = _polar(root_radius + fillet_radius, root_corner_angle)
        start = _polar(root_radius, root_corner_angle)
        end = _polar(tangent_on_line, -half_angle_at_base)
        right.append(start)
        right.extend(_arc_points(center, start, end, fillet_points))
        right.append(end)
    else:
        root_corner_angle = -(half_angle_at_base if root_radius < base_radius
                              else flank_angle(root_radius))
        right.append(_polar(root_radius, root_corner_angle))
    # The involute itself. Its first point sits on the base circle (or on the
    # root circle when that is higher), continuous with whatever came before.
    right.extend(_polar(radius, -flank_angle(radius)) for radius in radii)

    if root_corner_angle <= -angular_pitch / 2.0:
        raise ValueError(
            f"a {teeth}-tooth gear with dedendum {dedendum_coefficient}*module and a "
            f"{root_fillet_coefficient}*module root fillet has no room left between "
            f"two teeth: the root of one tooth runs into the next. Lower "
            f"dedendum_coefficient or root_fillet_coefficient."
        )

    left = [(x, -y) for x, y in reversed(right)]
    tip_arc = (_arc_points((0.0, 0.0), right[-1], left[0], tip_points)
               if half_angle_at_tip > 0.0 else [])
    tooth = right + tip_arc + left
    points_per_tooth = len(tooth) + (root_points - 1)

    # --- all the teeth, plus the root arc that joins each to the next ---
    points: list = []
    for index in range(teeth):
        offset = index * angular_pitch
        cos_o, sin_o = math.cos(offset), math.sin(offset)
        points.extend((x * cos_o - y * sin_o, x * sin_o + y * cos_o) for x, y in tooth)
        gap_start = _polar(root_radius, offset - root_corner_angle)
        gap_end = _polar(root_radius, offset + angular_pitch + root_corner_angle)
        points.extend(_arc_points((0.0, 0.0), gap_start, gap_end, root_points))

    if fillet_radius <= 0.0 and root_fillet_coefficient > 0.0:
        warnings.append(
            f"no root fillet was cut ({fillet_note}): the flank meets the root "
            f"circle in a corner, which is where a gear tooth breaks. Fine for a "
            f"concept or a 3D print, not for a loaded steel gear -- add a fillet "
            f"to the real part, or lower dedendum_coefficient to make room"
        )
    if chord_error > 0.05:
        warnings.append(
            f"the involute flanks are chords {chord_error:.4f} mm off the true "
            f"curve, which is coarse for a running gear: raise flank_points "
            f"(the error falls roughly with its square) if these flanks have to "
            f"transmit motion rather than just show the shape"
        )

    area = polygon_area(points)
    blank_area = math.pi * tip_radius ** 2
    return GearProfile(
        points=points,
        table=table,
        area_mm2=area,
        blank_area_mm2=blank_area,
        tip_land_mm=tip_land,
        effective_tip_diameter=2.0 * effective_tip_radius,
        root_fillet_radius=fillet_radius,
        root_fillet_note=fillet_note,
        chord_error_mm=chord_error,
        points_per_tooth=points_per_tooth,
        warnings=warnings,
    )
