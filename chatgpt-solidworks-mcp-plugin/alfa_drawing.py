"""
Pure drawing-geometry logic for the SolidWorks MCP, with no COM dependency.

Why this module exists
----------------------
Every drawing bug this layer fixes (dimension on the wrong edge, view off the
sheet, text at the sheet origin, un-normalised scale) is a *geometry* bug, not
a COM bug. Keeping that geometry in ``server.py`` -- next to ``win32com`` calls
and behind a COM executor that is started at import time -- makes it testable
only on a machine with SolidWorks open. Here it is plain Python: the whole
selection/placement/verification policy runs in milliseconds under pytest, and
``server.py`` keeps only the thin layer that actually talks to SolidWorks.

Units: every public function takes and returns millimetres, except
``project_point`` and ``classify_edge``, which work in the metres the
SolidWorks API uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

M_TO_MM = 1000.0


def mm(meters: float) -> float:
    """Metres (SolidWorks system units) to millimetres, rounded for display."""
    return round(meters * M_TO_MM, 3)


def to_m(millimeters: float) -> float:
    return millimeters / M_TO_MM


def deg(radians: float) -> float:
    return round(math.degrees(radians), 4)


# ---------------------------------------------------------------------------
# Normalised scale series
# ---------------------------------------------------------------------------
# ABNT NBR 8196 / ISO 5455. Ordered largest-first so that pick_scale_that_fits
# returns the biggest scale that still fits -- the legibility-preserving choice.

NORMAL_SCALES: tuple[tuple[int, int], ...] = (
    (50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1),
    (1, 2), (1, 5), (1, 10), (1, 20), (1, 25), (1, 50),
    (1, 75), (1, 100), (1, 200), (1, 500), (1, 1000),
)


def is_normalized_scale(numerator: float, denominator: float, tol: float = 1e-9) -> bool:
    """True when numerator:denominator sits on the normalised series.

    Compares the *ratio*, not the pair, because SolidWorks stores (2, 4) for
    what the user set as 1:2 and (0.05, 1) for 1:20.
    """
    if denominator == 0:
        return False
    ratio = numerator / denominator
    return any(abs(ratio - n / d) <= tol * max(1.0, abs(ratio)) for n, d in NORMAL_SCALES)


def nearest_normalized_scale(ratio: float) -> tuple[int, int]:
    """Closest normalised scale to an arbitrary ratio, compared in log space.

    Log space because scale error is multiplicative: for a part that wants
    1:25, the scale 1:20 is as wrong as 1:31, even though 25-20 and 31-25
    differ linearly.
    """
    if ratio <= 0:
        raise ValueError(f"Scale ratio must be positive, got {ratio}.")
    return min(NORMAL_SCALES, key=lambda nd: abs(math.log(ratio) - math.log(nd[0] / nd[1])))


def format_scale(numerator: float, denominator: float) -> str:
    return f"{numerator:g}:{denominator:g}"


# ---------------------------------------------------------------------------
# Paper formats and usable area
# ---------------------------------------------------------------------------
# Width x height in mm, landscape. Matched with tolerance because a sheet
# built from a custom template is rarely exactly 420.000 mm.

PAPER_FORMATS: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
}

# The ANSI/US series, in millimetres. A stock SolidWorks install often
# defaults to Letter, and identify_format returning None for the sheet the
# user actually has makes every format-dependent check silently inert --
# found live on a 279.4 x 215.9 sheet.
ANSI_FORMATS: dict[str, tuple[float, float]] = {
    "Letter/ANSI A": (279.4, 215.9),   # 11 x 8.5 in
    "Legal": (355.6, 215.9),           # 14 x 8.5 in
    "Tabloid/ANSI B": (431.8, 279.4),  # 17 x 11 in
    "ANSI C": (558.8, 431.8),          # 22 x 17 in
    "ANSI D": (863.6, 558.8),          # 34 x 22 in
    "ANSI E": (1117.6, 863.6),         # 44 x 34 in
}

# Margins per ABNT NBR 10068: 25 mm on the filing edge, 10 mm elsewhere.
DEFAULT_MARGIN_MM = 10.0
DEFAULT_BINDING_MARGIN_MM = 25.0
# Title block sits bottom-right; ABNT caps its width at 178 mm.
DEFAULT_TITLE_BLOCK_MM = (178.0, 55.0)
# Band reserved around a view for its own dimensions and their text.
DEFAULT_DIM_BAND_MM = 25.0
# How far outside a view outline a dimension text is placed by default.
DEFAULT_DIM_OFFSET_MM = 12.0


def identify_format(width_mm: float, height_mm: float, tol: float = 5.0) -> Optional[str]:
    """Name the paper format of a sheet, in either orientation.

    ISO (A0-A4) is tried first, then ANSI/US. Returns None only for a truly
    custom size; the caller can still work from size_mm.
    """
    for table in (PAPER_FORMATS, ANSI_FORMATS):
        for name, (w, h) in table.items():
            if (abs(width_mm - w) <= tol and abs(height_mm - h) <= tol) or (
                abs(width_mm - h) <= tol and abs(height_mm - w) <= tol
            ):
                return name
    return None


@dataclass
class SheetConfig:
    """Drawing-standard numbers the MCP must not re-guess on every call."""

    margin_mm: float = DEFAULT_MARGIN_MM
    binding_margin_mm: float = DEFAULT_BINDING_MARGIN_MM
    title_block_mm: tuple[float, float] = DEFAULT_TITLE_BLOCK_MM
    dim_band_mm: float = DEFAULT_DIM_BAND_MM
    dim_offset_mm: float = DEFAULT_DIM_OFFSET_MM
    min_view_gap_mm: float = 5.0

    def usable_area(self, width_mm: float, height_mm: float) -> list[float]:
        """[xmin, ymin, xmax, ymax] of the sheet minus margins, in mm.

        The title block is NOT subtracted here: it occupies only the
        bottom-right corner, and cutting the whole bottom band off would
        reject layouts that are perfectly legal. ``title_block_box`` returns
        it as its own rectangle so callers test views against it separately.
        """
        return [
            self.binding_margin_mm,
            self.margin_mm,
            width_mm - self.margin_mm,
            height_mm - self.margin_mm,
        ]

    def title_block_box(self, width_mm: float, height_mm: float) -> list[float]:
        tb_w, tb_h = self.title_block_mm
        right = width_mm - self.margin_mm
        bottom = self.margin_mm
        return [right - tb_w, bottom, right, bottom + tb_h]


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------
# A box is [xmin, ymin, xmax, ymax] -- exactly the shape IView.GetOutline
# returns (in metres), which is why it is the native currency here.


def normalize_box(box) -> list[float]:
    x0, y0, x1, y1 = box[0], box[1], box[2], box[3]
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def box_center(box) -> list[float]:
    b = normalize_box(box)
    return [(b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0]


def box_size(box) -> list[float]:
    b = normalize_box(box)
    return [b[2] - b[0], b[3] - b[1]]


def boxes_overlap(a, b, gap: float = 0.0) -> bool:
    """True when two boxes come closer than ``gap`` (detector for E06).

    ``gap`` is required clearance, so with gap > 0 merely touching counts as
    overlapping -- which is what a drawing reviewer means by "too close".
    """
    a, b = normalize_box(a), normalize_box(b)
    return not (
        a[2] + gap <= b[0] or b[2] + gap <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


def box_contains(outer, inner) -> bool:
    """True when ``inner`` lies fully inside ``outer`` (detector for E07)."""
    o, i = normalize_box(outer), normalize_box(inner)
    return o[0] <= i[0] and o[1] <= i[1] and i[2] <= o[2] and i[3] <= o[3]


def point_in_box(point, box) -> bool:
    b = normalize_box(box)
    return b[0] <= point[0] <= b[2] and b[1] <= point[1] <= b[3]


def free_rectangles(area, occupied, min_size=(0.0, 0.0)) -> list[list[float]]:
    """Empty rectangles of ``area`` left over by the ``occupied`` boxes.

    Deliberately a horizontal-band decomposition and not a full
    maximal-rectangle sweep: drawing layout is row-based (views in
    registers), the result is easy to reason about, and an under-estimated
    free area only costs a smaller chosen scale -- never an overlapping view.
    """
    area = normalize_box(area)
    cuts = {area[1], area[3]}
    for box in occupied:
        b = normalize_box(box)
        if b[3] > area[1] and b[1] < area[3]:
            cuts.add(max(b[1], area[1]))
            cuts.add(min(b[3], area[3]))
    levels = sorted(cuts)
    out: list[list[float]] = []
    for low, high in zip(levels, levels[1:]):
        if high - low < min_size[1]:
            continue
        blockers = sorted(
            normalize_box(b) for b in occupied
            if normalize_box(b)[1] < high - 1e-9 and normalize_box(b)[3] > low + 1e-9
        )
        x = area[0]
        for b in blockers:
            if b[0] > x and b[0] - x >= min_size[0]:
                out.append([x, low, min(b[0], area[2]), high])
            x = max(x, b[2])
        if area[2] - x >= min_size[0]:
            out.append([x, low, area[2], high])
    return out


# ---------------------------------------------------------------------------
# Model -> sheet projection
# ---------------------------------------------------------------------------


def project_point(xform, point_m):
    """Project a model point (metres) into sheet space (metres).

    ``xform`` is the 13-double vector from IView.GetViewXform: a 3x3 rotation
    stored COLUMN-major in [0:9], a translation in [9:12] and the view scale
    in [12]. Getting the column/row convention wrong is the classic silent
    failure here -- it shows up only as a mirrored or rotated projection, so
    the convention is asserted in the tests rather than trusted.
    """
    if len(xform) != 13:
        raise ValueError(f"A view transform has 13 doubles, got {len(xform)}.")
    x, y, z = point_m
    s = xform[12]
    return (
        (xform[0] * x + xform[3] * y + xform[6] * z) * s + xform[9],
        (xform[1] * x + xform[4] * y + xform[7] * z) * s + xform[10],
    )


def view_scale_from_xform(xform) -> float:
    return xform[12]


# ---------------------------------------------------------------------------
# Edge classification
# ---------------------------------------------------------------------------


@dataclass
class EdgeInfo:
    """One visible drawing edge, in both model and sheet space.

    ``kind`` is line | circle | arc. Sheet coordinates are what a click
    targets; model values are what a dimension is expected to read back.
    """

    kind: str
    sheet_points: list = field(default_factory=list)
    sheet_center: Optional[tuple] = None
    sheet_radius: Optional[float] = None
    model_length: Optional[float] = None
    model_radius: Optional[float] = None
    orientation: Optional[str] = None
    id: Optional[str] = None
    component: Optional[str] = None

    def as_dict(self) -> dict:
        out: dict = {"id": self.id, "kind": self.kind}
        if self.component:
            out["component"] = self.component
        if self.orientation:
            out["orientation"] = self.orientation
        if self.kind == "line":
            out["p1_sheet_mm"] = [mm(c) for c in self.sheet_points[0]]
            out["p2_sheet_mm"] = [mm(c) for c in self.sheet_points[1]]
            out["length_model_mm"] = mm(self.model_length or 0.0)
        else:
            out["center_sheet_mm"] = [mm(c) for c in (self.sheet_center or (0.0, 0.0))]
            out["radius_sheet_mm"] = mm(self.sheet_radius or 0.0)
            out["radius_model_mm"] = mm(self.model_radius or 0.0)
            out["diameter_model_mm"] = mm(2.0 * (self.model_radius or 0.0))
            if self.kind == "arc":
                out["p1_sheet_mm"] = [mm(c) for c in self.sheet_points[0]]
                out["p2_sheet_mm"] = [mm(c) for c in self.sheet_points[1]]
                out["length_model_mm"] = mm(self.model_length or 0.0)
        return out


# An edge whose projected direction lies within this angle of an axis counts
# as axis-aligned. 0.5 deg is tight enough that a 1:100 taper is not called
# "horizontal", and loose enough to absorb float error in the transform.
_AXIS_TOL_RAD = math.radians(0.5)


def classify_edge(xform, curve_params, circle_params=None, is_circle: bool = False) -> EdgeInfo:
    """Build an EdgeInfo from raw IEdge data already read out of COM.

    ``curve_params`` is IEdge.GetCurveParams (start xyz, end xyz, ...).
    ``circle_params`` is ICurve.CircleParams (centre xyz, axis xyz, radius).

    Separating this from the COM read is the point of the module: a full
    circle (start == end) is exactly the case the old selector got wrong, and
    here it is one assertion away from being proven right.
    """
    if len(curve_params) < 6:
        raise ValueError("IEdge.GetCurveParams returned fewer than 6 doubles.")
    start = tuple(curve_params[0:3])
    end = tuple(curve_params[3:6])
    p1 = project_point(xform, start)
    p2 = project_point(xform, end)
    scale = view_scale_from_xform(xform)

    if is_circle and circle_params is not None:
        if len(circle_params) < 7:
            raise ValueError("ICurve.CircleParams returned fewer than 7 doubles.")
        center_model = tuple(circle_params[0:3])
        radius_model = float(circle_params[6])
        center_sheet = project_point(xform, center_model)
        # A full circle has coincident endpoints; an arc does not. This is the
        # distinction the old selector collapsed, which made a hole project to
        # a degenerate point-"segment" sitting on its own circumference.
        closed = max(abs(a - b) for a, b in zip(start, end)) < 1e-9
        span = 2.0 * math.pi if closed else _arc_span(center_model, start, end)
        return EdgeInfo(
            kind="circle" if closed else "arc",
            sheet_points=[p1, p2],
            sheet_center=center_sheet,
            sheet_radius=radius_model * abs(scale),
            model_radius=radius_model,
            model_length=radius_model * span,
        )

    length = math.dist(start, end)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if math.hypot(dx, dy) < 1e-12:
        orientation = "degenerate"
    else:
        angle = math.atan2(abs(dy), abs(dx))
        orientation = (
            "horizontal" if angle <= _AXIS_TOL_RAD
            else "vertical" if abs(angle - math.pi / 2) <= _AXIS_TOL_RAD
            else "inclined"
        )
    return EdgeInfo(kind="line", sheet_points=[p1, p2],
                    model_length=length, orientation=orientation)


def _arc_span(center, start, end) -> float:
    """Swept angle of an arc, from its centre and endpoints (radians)."""
    v1 = [s - c for s, c in zip(start, center)]
    v2 = [e - c for e, c in zip(end, center)]
    n1 = math.sqrt(sum(c * c for c in v1))
    n2 = math.sqrt(sum(c * c for c in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    cos = max(-1.0, min(1.0, sum(a * b for a, b in zip(v1, v2)) / (n1 * n2)))
    return math.acos(cos)


# ---------------------------------------------------------------------------
# Hit testing: which entity did the caller mean?
# ---------------------------------------------------------------------------


def distance_to_edge(edge: EdgeInfo, x_m: float, y_m: float) -> float:
    """Sheet-space distance (metres) from a point to an edge.

    Lines use distance to the segment. Circles and arcs use distance to the
    arc band, except that a point *inside* a full circle returns 0: clicking
    the middle of a hole is the natural gesture and the old code made it the
    one gesture guaranteed to fail.
    """
    if edge.kind == "line":
        return _distance_to_segment(edge.sheet_points[0], edge.sheet_points[1], x_m, y_m)

    cx, cy = edge.sheet_center
    r = edge.sheet_radius or 0.0
    d_center = math.hypot(x_m - cx, y_m - cy)
    if edge.kind == "circle":
        return 0.0 if d_center <= r else d_center - r
    # An arc is not an enclosure, so only the band counts.
    return abs(d_center - r)


def _distance_to_segment(p1, p2, x: float, y: float) -> float:
    sx, sy = p1
    ex, ey = p2
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(x - sx, y - sy)
    t = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / length_sq))
    return math.hypot(x - (sx + t * dx), y - (sy + t * dy))


class AmbiguousPick(Exception):
    """Two or more entities are equally plausible for one clicked point.

    Raised instead of silently choosing, because silently choosing is exactly
    how a dimension lands on the wrong edge with no error reported. The
    message lists the candidates so the caller can re-ask by entity ID.
    """

    def __init__(self, message: str, candidates: list):
        super().__init__(message)
        self.candidates = candidates


def pick_nearest_edge(
    edges,
    x_m: float,
    y_m: float,
    tolerance_m: float = 0.002,
    ambiguity_margin_m: float = 0.0005,
):
    """The single edge a sheet point means, or an error explaining why not.

    Returns (edge, distance_m). Raises LookupError when nothing is within
    ``tolerance_m``, and AmbiguousPick when the runner-up is within
    ``ambiguity_margin_m`` of the winner -- the case that used to resolve by
    enumeration order, i.e. by luck.
    """
    scored = sorted(
        ((edge, distance_to_edge(edge, x_m, y_m)) for edge in edges),
        key=lambda pair: pair[1],
    )
    in_range = [pair for pair in scored if pair[1] <= tolerance_m]
    if not in_range:
        nearest = f" Nearest was {mm(scored[0][1])} mm away." if scored else ""
        raise LookupError(
            f"No entity within {mm(tolerance_m)} mm of "
            f"({mm(x_m)}, {mm(y_m)}) mm on the sheet.{nearest}"
        )
    best, best_d = in_range[0]
    rivals = [pair for pair in in_range[1:] if pair[1] - best_d <= ambiguity_margin_m]
    if rivals:
        candidates = [(best, best_d)] + rivals
        raise AmbiguousPick(
            f"{len(candidates)} entities are within {mm(ambiguity_margin_m)} mm of "
            f"each other at ({mm(x_m)}, {mm(y_m)}) mm: "
            + ", ".join(f"{e.id or e.kind} ({mm(d)} mm)" for e, d in candidates)
            + ". Call get_view_entities and dimension by entity ID instead.",
            [e for e, _ in candidates],
        )
    return best, best_d


# ---------------------------------------------------------------------------
# Dimension text placement
# ---------------------------------------------------------------------------

SIDES = ("above", "below", "left", "right")


def place_dimension_text(view_outline, side: str = "above", offset_m: float = 0.012,
                         along: float = 0.5):
    """Where a dimension text goes, in sheet metres, given the view outline.

    Replaces the old ``place_x=0, place_y=0`` default, which put the text at
    the sheet origin -- outside the margin, in the bottom-left corner.
    ``along`` slides the text along that side (0 = start, 1 = end) so stacked
    dimensions on the same side do not land on top of each other.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {', '.join(SIDES)}, got '{side}'.")
    b = normalize_box(view_outline)
    x = b[0] + along * (b[2] - b[0])
    y = b[1] + along * (b[3] - b[1])
    return {
        "above": (x, b[3] + offset_m),
        "below": (x, b[1] - offset_m),
        "left": (b[0] - offset_m, y),
        "right": (b[2] + offset_m, y),
    }[side]


def side_for_edge(edge: EdgeInfo, view_outline) -> str:
    """The conventional side to put an edge's dimension on.

    A horizontal length reads above or below the view, a vertical one left or
    right, and each goes to the nearer outside -- which is what a draughtsman
    does by hand and what the LLM has been guessing.
    """
    b = normalize_box(view_outline)
    cx, cy = box_center(b)
    if edge.kind == "line" and edge.orientation == "vertical":
        ex = (edge.sheet_points[0][0] + edge.sheet_points[1][0]) / 2.0
        return "left" if ex <= cx else "right"
    if edge.kind in ("circle", "arc"):
        ey = (edge.sheet_center or (cx, cy))[1]
        return "above" if ey >= cy else "below"
    ey = (edge.sheet_points[0][1] + edge.sheet_points[1][1]) / 2.0
    return "above" if ey >= cy else "below"


def stagger_along(index: int, count: int) -> float:
    """Spread ``count`` texts along one side so they do not collide.

    Keeps everything inside the middle 70% of the side, where a leader line
    still reaches the geometry.
    """
    if count <= 1:
        return 0.5
    return 0.15 + 0.7 * (index / (count - 1))


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
# Codes match the taxonomy in the Alfa technical report, chapter 4.3, so that
# a finding here and a finding in the report are the same finding.

ISSUE_CODES: dict[str, str] = {
    "E01": "dimension on the wrong entity (measured value differs from expected)",
    "E02": "missing dimension",
    "E03": "duplicate dimension",
    "E04": "dimension attached to the wrong number of entities",
    "E05": "dimension text badly positioned",
    "E06": "views overlap",
    "E07": "view outside the usable area or over the title block",
    "E08": "scale wrong or not on the normalised series",
    "E09": "views not aligned / wrong projection angle",
    "E16": "dangling dimension",
    # Assembly placement. Same taxonomy extended to the side of the model that
    # the drawing codes above cannot see: where the components actually ARE.
    # A part in the wrong place produces a perfectly valid drawing of the wrong
    # assembly, so these have to be checkable on their own.
    "M01": "component neither fixed nor mated: its position is not reproducible",
    "M02": "component sitting on the assembly origin (placement probably did not take)",
    "M03": "two or more components sharing one position (stacked on top of each other)",
    "M04": "component fixed and mated at the same time (over-constrained)",
    "M05": "component position could not be read (suppressed, lightweight or not loaded)",
    "M06": "component declared as moving is fixed, so the mechanism cannot move",
}

SEVERITIES = ("critical", "warning", "info")


def issue(code: str, severity: str, message: str, *refs) -> dict:
    if code not in ISSUE_CODES:
        raise ValueError(f"Unknown issue code '{code}'. Known: {', '.join(sorted(ISSUE_CODES))}.")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}, got '{severity}'.")
    return {"code": code, "severity": severity, "message": message,
            "refs": [r for r in refs if r is not None]}


def worst_status(issues) -> str:
    if any(i["severity"] == "critical" for i in issues):
        return "critical"
    if any(i["severity"] == "warning" for i in issues):
        return "warning"
    return "approved"


# ---------------------------------------------------------------------------
# Layout checks (E06, E07, E08)
# ---------------------------------------------------------------------------


def check_sheet_layout(views, usable_area_mm, title_block_mm, config: SheetConfig) -> list[dict]:
    """Every layout problem visible from outlines and scales alone.

    ``views`` is a list of dicts with name, outline_mm and scale_ratio.
    """
    issues: list[dict] = []
    for view in views:
        name = view.get("name", "?")
        box = view.get("outline_mm")
        if box is None:
            continue
        if usable_area_mm and not box_contains(usable_area_mm, box):
            issues.append(issue("E07", "critical",
                                f"view {name} is not fully inside the usable area "
                                f"{[round(v, 1) for v in usable_area_mm]} mm", name))
        if title_block_mm and boxes_overlap(box, title_block_mm):
            issues.append(issue("E07", "critical",
                                f"view {name} overlaps the title block", name))
        ratio = view.get("scale_ratio")
        if ratio and not is_normalized_scale(ratio[0], ratio[1]):
            suggestion = nearest_normalized_scale(ratio[0] / ratio[1]) if ratio[1] else None
            extra = f"; nearest normalised is {suggestion[0]}:{suggestion[1]}" if suggestion else ""
            issues.append(issue("E08", "warning",
                                f"view {name} is at {format_scale(*ratio)}, "
                                f"which is not on the normalised series{extra}", name))
    for i, a in enumerate(views):
        for b in views[i + 1:]:
            if a.get("outline_mm") is None or b.get("outline_mm") is None:
                continue
            if boxes_overlap(a["outline_mm"], b["outline_mm"], gap=config.min_view_gap_mm):
                issues.append(issue("E06", "critical",
                                    f"views {a.get('name')} and {b.get('name')} are closer "
                                    f"than the {config.min_view_gap_mm} mm minimum gap",
                                    a.get("name"), b.get("name")))
    return issues


def pick_scale_that_fits(size_at_1to1_mm, available_mm, margin_mm: float = 0.0):
    """Largest normalised scale at which something fits the available space.

    Used instead of letting the model pick a scale: a 2400 mm beam at 1:1 on
    an A3 is the single most common positioning failure, and no amount of
    prompting makes arithmetic reliable.
    """
    w, h = size_at_1to1_mm
    avail_w, avail_h = available_mm[0] - 2 * margin_mm, available_mm[1] - 2 * margin_mm
    if avail_w <= 0 or avail_h <= 0:
        return None
    for num, den in NORMAL_SCALES:
        s = num / den
        if w * s <= avail_w and h * s <= avail_h:
            return (num, den)
    return None


# ---------------------------------------------------------------------------
# Dimension-coverage checks (E02, E03, E04, E05, E16)
# ---------------------------------------------------------------------------


@dataclass
class CoverageRules:
    """What "fully dimensioned" means for a view.

    "Every edge dimensioned" is the wrong target: a rolled beam has dozens of
    silhouette and flange edges nobody dimensions. The useful rule is by
    intent -- overall extents, every hole, every non-square cut angle.
    """

    require_overall_extents: bool = True
    require_hole_diameters: bool = True
    require_cut_angles: bool = True
    # An edge shorter than this is detail (a chamfer, a fillet run-out) and is
    # not expected to carry its own dimension.
    min_edge_length_mm: float = 3.0
    # Two dimensions count as duplicates when their values differ by less.
    duplicate_tolerance_mm: float = 0.05


def extreme_edges(edges) -> list:
    """The edges that define a view's overall width and height.

    Four at most: the leftmost vertical, rightmost vertical, lowest
    horizontal, highest horizontal. These are the ones whose absence is a
    genuine "missing overall dimension".
    """
    lines = [e for e in edges if e.kind == "line" and e.orientation in ("horizontal", "vertical")]
    if not lines:
        return []
    out = []
    verticals = [e for e in lines if e.orientation == "vertical"]
    horizontals = [e for e in lines if e.orientation == "horizontal"]
    if verticals:
        out.append(min(verticals, key=lambda e: min(p[0] for p in e.sheet_points)))
        out.append(max(verticals, key=lambda e: max(p[0] for p in e.sheet_points)))
    if horizontals:
        out.append(min(horizontals, key=lambda e: min(p[1] for p in e.sheet_points)))
        out.append(max(horizontals, key=lambda e: max(p[1] for p in e.sheet_points)))
    unique: list = []
    for e in out:
        if not any(e is seen for seen in unique):
            unique.append(e)
    return unique


def check_dimension_coverage(view_name: str, edges, dimensions,
                             rules: Optional[CoverageRules] = None) -> list[dict]:
    """Missing, duplicate, mis-attached and dangling dimensions in one view.

    ``dimensions`` is a list of dicts with id, value_mm, unit, attached
    (entity IDs) and dangling, as get_view_dimensions returns them.
    """
    rules = rules or CoverageRules()
    issues: list[dict] = []
    covered = {ref for d in dimensions for ref in (d.get("attached") or []) if ref}

    if rules.require_overall_extents:
        for edge in extreme_edges(edges):
            if (edge.model_length or 0.0) * M_TO_MM < rules.min_edge_length_mm:
                continue
            if edge.id not in covered:
                issues.append(issue("E02", "warning",
                                    f"{view_name}: outer edge {edge.id} "
                                    f"({mm(edge.model_length or 0)} mm) has no dimension",
                                    edge.id))
    if rules.require_hole_diameters:
        for edge in edges:
            if edge.kind == "circle" and edge.id not in covered:
                issues.append(issue("E02", "critical",
                                    f"{view_name}: hole {edge.id} "
                                    f"(diameter {mm(2 * (edge.model_radius or 0))} mm) "
                                    f"has no dimension", edge.id))
    if rules.require_cut_angles:
        for edge in edges:
            if edge.kind == "line" and edge.orientation == "inclined" \
                    and (edge.model_length or 0.0) * M_TO_MM >= rules.min_edge_length_mm \
                    and edge.id not in covered:
                issues.append(issue("E02", "warning",
                                    f"{view_name}: inclined edge {edge.id} has no dimension; "
                                    f"a cut at an angle needs its angle stated", edge.id))

    for group in group_duplicate_dimensions(dimensions, rules.duplicate_tolerance_mm):
        issues.append(issue("E03", "warning",
                            f"{view_name}: {len(group)} dimensions read the same value "
                            f"{group[0].get('value_mm')} {group[0].get('unit', 'mm')}",
                            *[d.get("id") for d in group]))
    for d in dimensions:
        if d.get("dangling"):
            issues.append(issue("E16", "critical",
                                f"{view_name}: dimension {d.get('id')} is dangling "
                                f"(no longer attached to geometry)", d.get("id")))
    return issues


def group_duplicate_dimensions(dimensions, tolerance_mm: float = 0.05) -> list[list[dict]]:
    """Dimensions that measure the same thing twice (detector for E03).

    Grouped by value *and* unit, and only when the attached entity sets match
    or one is unknown -- two different holes that happen to be 20 mm apart are
    not duplicates.
    """
    groups: list[list[dict]] = []
    for d in dimensions:
        value = d.get("value_mm")
        if value is None:
            continue
        placed = False
        for group in groups:
            ref = group[0]
            if ref.get("unit", "mm") != d.get("unit", "mm"):
                continue
            if abs((ref.get("value_mm") or 0.0) - value) > tolerance_mm:
                continue
            if _same_attachment(ref, d):
                group.append(d)
                placed = True
                break
        if not placed:
            groups.append([d])
    return [g for g in groups if len(g) > 1]


def _same_attachment(a: dict, b: dict) -> bool:
    sa = {r for r in (a.get("attached") or []) if r}
    sb = {r for r in (b.get("attached") or []) if r}
    if not sa or not sb:
        return True  # unknown attachment: treat equal values as suspicious
    return sa == sb


def check_text_positions(sheet_views, config: SheetConfig) -> list[dict]:
    """Dimension texts sitting on another view, or outside the usable area.

    ``sheet_views`` is a list of dicts with name, outline_mm, usable_area_mm
    and dimensions (each with id and text_position_mm).
    """
    issues: list[dict] = []
    for view in sheet_views:
        for d in view.get("dimensions", []):
            pos = d.get("text_position_mm")
            if not pos:
                continue
            if pos[0] == 0 and pos[1] == 0:
                issues.append(issue("E05", "critical",
                                    f"dimension {d.get('id')} has its text at the sheet "
                                    f"origin (0, 0) -- outside the drawing frame", d.get("id")))
                continue
            usable = view.get("usable_area_mm")
            if usable and not point_in_box(pos, usable):
                issues.append(issue("E05", "warning",
                                    f"dimension {d.get('id')} has its text outside the "
                                    f"usable area", d.get("id")))
            for other in sheet_views:
                if other.get("name") == view.get("name"):
                    continue
                if other.get("outline_mm") and point_in_box(pos, other["outline_mm"]):
                    issues.append(issue("E05", "warning",
                                        f"dimension {d.get('id')} of view {view.get('name')} "
                                        f"has its text over view {other.get('name')}",
                                        d.get("id"), other.get("name")))
    return issues


def check_measured_value(measured_mm: float, expected_mm: Optional[float],
                         tolerance_mm: float = 0.5, attached_count: Optional[int] = None,
                         expected_attachments: Optional[int] = None,
                         ref: Optional[str] = None) -> list[dict]:
    """Turn one created dimension into an assertion (detectors for E01, E04).

    This is the single change that closes the loop: the model knows the beam
    is 2400 mm long, so a dimension that reads 150 mm is a reported failure
    instead of a cheerful success message.
    """
    issues: list[dict] = []
    if expected_attachments is not None and attached_count is not None \
            and attached_count != expected_attachments:
        issues.append(issue("E04", "critical",
                            f"dimension is attached to {attached_count} entities, "
                            f"expected {expected_attachments}", ref))
    if expected_mm is not None and abs(measured_mm - expected_mm) > tolerance_mm:
        issues.append(issue("E01", "critical",
                            f"dimension measures {measured_mm} mm but {expected_mm} mm was "
                            f"expected (tolerance {tolerance_mm} mm) -- it is most likely on "
                            f"the wrong entity", ref))
    return issues


# ---------------------------------------------------------------------------
# Entity registry
# ---------------------------------------------------------------------------


class EntityRegistry:
    """Short, stable IDs for drawing entities, per document.

    The model cannot hold a COM pointer, so it needs a name it can quote back
    ("dimension V1#E17"). IDs are invalidated by generation: a rebuild bumps
    the generation and every older ID is refused with a message that says to
    read again, instead of resolving to a stale pointer.
    """

    def __init__(self):
        self._docs: dict[str, dict] = {}

    def generation(self, doc: str) -> int:
        return self._docs.setdefault(doc, {"gen": 0, "items": {}})["gen"]

    def invalidate(self, doc: str) -> int:
        entry = self._docs.setdefault(doc, {"gen": 0, "items": {}})
        entry["gen"] += 1
        entry["items"] = {}
        return entry["gen"]

    def register(self, doc: str, view: str, index: int, payload: dict) -> str:
        entry = self._docs.setdefault(doc, {"gen": 0, "items": {}})
        entity_id = f"{view}#E{index}"
        entry["items"][entity_id] = dict(payload, view=view, gen=entry["gen"])
        return entity_id

    def resolve(self, doc: str, entity_id: str) -> dict:
        entry = self._docs.get(doc)
        if entry is None or entity_id not in entry["items"]:
            raise LookupError(
                f"Unknown entity ID '{entity_id}'. Call get_view_entities on the view "
                f"first; IDs are only valid until the drawing is rebuilt."
            )
        item = entry["items"][entity_id]
        if item["gen"] != entry["gen"]:
            raise LookupError(
                f"Entity ID '{entity_id}' expired when the drawing was rebuilt. "
                f"Call get_view_entities again."
            )
        return item

    def ids_for_view(self, doc: str, view: str) -> list[str]:
        entry = self._docs.get(doc, {"items": {}})
        return [k for k, v in entry["items"].items()
                if v["view"] == view and v["gen"] == entry.get("gen", 0)]

    def clear(self, doc: Optional[str] = None) -> None:
        if doc is None:
            self._docs.clear()
        else:
            self._docs.pop(doc, None)
