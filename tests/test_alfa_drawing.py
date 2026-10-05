"""
Regression tests for the drawing-geometry layer (``alfa_drawing``).

Every test here is a bug that shipped. The names say which: a test called
``test_full_circle_is_not_a_degenerate_segment`` exists because the old
selector projected a hole to a zero-length chord, so clicking the middle of a
hole -- the natural gesture -- was the one gesture guaranteed to miss.

No SolidWorks and no COM: this whole file runs in well under a second, which
is the point of keeping the geometry out of ``server.py``.

Run with:  python -m pytest tests/test_alfa_drawing.py -q
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alfa_drawing as ad  # noqa: E402


# A view transform at 1:1 with no rotation and no offset: model (x, y) maps
# straight to sheet (x, y). Column-major 3x3 identity, zero translation,
# scale 1 -- the shape IView.GetViewXform returns.
IDENTITY_XFORM = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1.0)


def xform(scale=1.0, tx=0.0, ty=0.0):
    return (1, 0, 0, 0, 1, 0, 0, 0, 1, tx, ty, 0, scale)


def line_params(p1, p2):
    """An IEdge.GetCurveParams vector for a straight edge."""
    return tuple(p1) + tuple(p2) + (0.0, 0.0)


def circle_params(center, radius, axis=(0, 0, 1)):
    """An ICurve.CircleParams vector: centre xyz, axis xyz, radius."""
    return tuple(center) + tuple(axis) + (radius,)


# ---------------------------------------------------------------------------
# Units and scales
# ---------------------------------------------------------------------------


def test_mm_round_trip():
    assert ad.mm(2.4) == 2400.0
    assert ad.to_m(2400.0) == pytest.approx(2.4)


@pytest.mark.parametrize("num,den", [(1, 1), (1, 20), (2, 1), (1, 100)])
def test_normalised_scales_are_accepted(num, den):
    assert ad.is_normalized_scale(num, den)


def test_scale_is_matched_by_ratio_not_by_pair():
    """SolidWorks stores 1:2 as (2, 4) or 1:20 as (0.05, 1).

    Comparing the pair rejects both as "not normalised" and floods the report
    with false E08 findings.
    """
    assert ad.is_normalized_scale(2, 4)
    assert ad.is_normalized_scale(0.05, 1)
    assert ad.is_normalized_scale(10, 10)


def test_arbitrary_scale_is_rejected_and_a_neighbour_suggested():
    """scale=0.0437 passes the old validation; it must not pass this one."""
    assert not ad.is_normalized_scale(0.0437, 1)
    assert ad.nearest_normalized_scale(0.0437) == (1, 25)


def test_zero_denominator_is_not_a_crash():
    assert ad.is_normalized_scale(1, 0) is False


def test_nearest_scale_refuses_a_non_positive_ratio():
    with pytest.raises(ValueError, match="positive"):
        ad.nearest_normalized_scale(0)


def test_long_beam_at_1to1_does_not_fit_an_a3_and_a_scale_is_chosen():
    """The headline positioning failure: a 2400 mm beam inserted at 1:1.

    A3 is 420x297; nothing 2400 mm long fits, so the tool must pick a scale
    rather than let the model assert that 1:1 worked.
    """
    cfg = ad.SheetConfig()
    usable = cfg.usable_area(420, 297)
    avail = (usable[2] - usable[0], usable[3] - usable[1])
    assert ad.pick_scale_that_fits((2400, 150), avail, margin_mm=cfg.dim_band_mm) == (1, 10)


def test_nothing_fits_returns_none_instead_of_a_silly_scale():
    assert ad.pick_scale_that_fits((500000, 500000), (100, 100)) is None


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_projection_uses_the_column_major_convention():
    """IView.GetViewXform stores its rotation by columns.

    A row-major read of a rotated view mirrors the projection, which looks
    plausible on screen and puts every dimension on the mirrored edge. The
    90-degree-about-Z case below distinguishes the two conventions: column
    major sends +X to +Y, row major sends it to -Y.
    """
    rot_z90 = (0, 1, 0, -1, 0, 0, 0, 0, 1, 0, 0, 0, 1.0)
    x, y = ad.project_point(rot_z90, (1.0, 0.0, 0.0))
    assert (round(x, 9), round(y, 9)) == (0.0, 1.0)


def test_projection_applies_scale_then_translation():
    x, y = ad.project_point(xform(scale=0.05, tx=0.15, ty=0.15), (2.0, 0.0, 0.0))
    assert (round(x, 6), round(y, 6)) == (0.25, 0.15)


def test_a_malformed_transform_fails_loudly():
    """The old code raised a bare RuntimeError deep inside a loop; a short
    vector now fails at the boundary with the length in the message."""
    with pytest.raises(ValueError, match="13 doubles"):
        ad.project_point((1, 0, 0), (0, 0, 0))


# ---------------------------------------------------------------------------
# Edge classification
# ---------------------------------------------------------------------------


def test_a_straight_edge_reports_its_model_length_not_its_sheet_length():
    """At 1:20 a 2400 mm beam is 120 mm on paper.

    Reporting the sheet length is how "the dimension says 150" happens: the
    model compares the wrong number and concludes everything is fine.
    """
    edge = ad.classify_edge(xform(scale=0.05), line_params((0, 0, 0), (2.4, 0, 0)))
    assert edge.kind == "line"
    assert edge.orientation == "horizontal"
    assert ad.mm(edge.model_length) == 2400.0
    assert round(edge.sheet_points[1][0] - edge.sheet_points[0][0], 9) == 0.12


def test_orientation_distinguishes_horizontal_vertical_and_inclined():
    h = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (1, 0, 0)))
    v = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0, 1, 0)))
    i = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (1, 1, 0)))
    assert (h.orientation, v.orientation, i.orientation) == ("horizontal", "vertical", "inclined")


def test_a_45_degree_cut_is_inclined_not_rounded_to_an_axis():
    """E14: a tube cut at 45 degrees must be recognisable as a cut."""
    edge = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.1, 0.1, 0)))
    assert edge.orientation == "inclined"


def test_a_nearly_horizontal_edge_is_not_called_horizontal():
    """A 1:100 taper is a real taper and needs its own dimension."""
    edge = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (1.0, 0.01, 0)))
    assert edge.orientation == "inclined"


def test_full_circle_is_not_a_degenerate_segment():
    """THE hole bug: a full circle has start == end.

    The old selector took only GetCurveParams[0:6] and measured the distance
    to the segment between them. For a closed circle that segment collapses
    to a single point sitting on the circumference, so the hole was
    effectively invisible except from one lucky angle.
    """
    params = line_params((0.01, 0, 0), (0.01, 0, 0))  # start == end
    edge = ad.classify_edge(IDENTITY_XFORM, params,
                            circle_params=circle_params((0, 0, 0), 0.01), is_circle=True)
    assert edge.kind == "circle"
    assert ad.mm(edge.model_radius) == 10.0
    assert edge.as_dict()["diameter_model_mm"] == 20.0
    assert ad.mm(edge.model_length) == pytest.approx(ad.mm(2 * math.pi * 0.01))


def test_an_open_arc_is_classified_as_an_arc_with_its_swept_length():
    """A quarter arc of R10: endpoints differ, so it is an arc, not a circle."""
    edge = ad.classify_edge(IDENTITY_XFORM,
                            line_params((0.01, 0, 0), (0, 0.01, 0)),
                            circle_params=circle_params((0, 0, 0), 0.01), is_circle=True)
    assert edge.kind == "arc"
    assert ad.mm(edge.model_length) == pytest.approx(ad.mm(0.01 * math.pi / 2), abs=0.01)


def test_circle_sheet_radius_follows_the_view_scale():
    """A 10 mm hole in a 1:20 view is 0.5 mm on paper.

    This is the number that decides legibility: a scale that makes a hole
    smaller than about 2 mm on the sheet is unusable however well it fits.
    """
    edge = ad.classify_edge(xform(scale=0.05),
                            line_params((0.01, 0, 0), (0.01, 0, 0)),
                            circle_params=circle_params((0, 0, 0), 0.01), is_circle=True)
    assert ad.mm(edge.sheet_radius) == 0.5
    assert ad.mm(edge.model_radius) == 10.0


def test_short_curve_params_fail_loudly():
    with pytest.raises(ValueError, match="6 doubles"):
        ad.classify_edge(IDENTITY_XFORM, (0, 0, 0))


def test_circle_without_radius_data_fails_loudly():
    with pytest.raises(ValueError, match="7 doubles"):
        ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0, 0, 0)),
                         circle_params=(0, 0, 0), is_circle=True)


# ---------------------------------------------------------------------------
# Hit testing
# ---------------------------------------------------------------------------


def make_edges():
    """A 100 x 50 mm rectangle with a 20 mm hole in the middle, at 1:1."""
    rect = [
        ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.1, 0, 0))),      # bottom
        ad.classify_edge(IDENTITY_XFORM, line_params((0, 0.05, 0), (0.1, 0.05, 0))),  # top
        ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0, 0.05, 0))),     # left
        ad.classify_edge(IDENTITY_XFORM, line_params((0.1, 0, 0), (0.1, 0.05, 0))),  # right
    ]
    hole = ad.classify_edge(IDENTITY_XFORM,
                            line_params((0.06, 0.025, 0), (0.06, 0.025, 0)),
                            circle_params=circle_params((0.05, 0.025, 0), 0.01),
                            is_circle=True)
    for i, e in enumerate(rect + [hole], start=1):
        e.id = f"V1#E{i}"
    return rect + [hole]


def test_clicking_the_centre_of_a_hole_selects_the_hole():
    """The gesture the old code could not serve.

    A point inside a full circle has distance 0 from it, so the hole wins
    outright instead of losing to whatever straight edge happens to pass
    within the tolerance.
    """
    edges = make_edges()
    picked, distance = ad.pick_nearest_edge(edges, 0.05, 0.025)
    assert picked.kind == "circle"
    assert distance == 0.0


def test_clicking_an_edge_selects_that_edge():
    edges = make_edges()
    picked, _ = ad.pick_nearest_edge(edges, 0.05, 0.0001)
    assert picked.id == "V1#E1"  # bottom


def test_clicking_far_from_everything_raises_with_the_real_distance():
    """"No edge found" with no number is unactionable; the model cannot tell
    whether it missed by 3 mm or by 300."""
    edges = make_edges()
    with pytest.raises(LookupError, match="Nearest was"):
        ad.pick_nearest_edge(edges, 0.5, 0.5)


def test_an_ambiguous_click_is_refused_instead_of_resolved_by_luck():
    """E01's real mechanism.

    Two edges within the tolerance used to resolve by enumeration order, so
    the dimension landed on whichever edge SolidWorks happened to list first
    -- and the tool reported success either way. Now the caller is told, with
    the candidate IDs, to dimension by ID.
    """
    a = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.1, 0, 0)))
    b = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0.0002, 0), (0.1, 0.0002, 0)))
    a.id, b.id = "V1#E1", "V1#E2"
    with pytest.raises(ad.AmbiguousPick) as exc:
        ad.pick_nearest_edge([a, b], 0.05, 0.0001)
    assert {e.id for e in exc.value.candidates} == {"V1#E1", "V1#E2"}
    assert "get_view_entities" in str(exc.value)


def test_a_clear_winner_is_not_called_ambiguous():
    a = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.1, 0, 0)))
    b = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0.01, 0), (0.1, 0.01, 0)))
    a.id, b.id = "V1#E1", "V1#E2"
    picked, _ = ad.pick_nearest_edge([a, b], 0.05, 0.0)
    assert picked.id == "V1#E1"


def test_distance_to_an_arc_is_the_band_not_the_interior():
    """An arc is not an enclosure: a point at its centre is radius away."""
    arc = ad.classify_edge(IDENTITY_XFORM,
                           line_params((0.01, 0, 0), (0, 0.01, 0)),
                           circle_params=circle_params((0, 0, 0), 0.01), is_circle=True)
    assert ad.distance_to_edge(arc, 0.0, 0.0) == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Boxes and layout
# ---------------------------------------------------------------------------


def test_boxes_that_merely_touch_overlap_once_a_gap_is_required():
    a, b = [0, 0, 10, 10], [10, 0, 20, 10]
    assert not ad.boxes_overlap(a, b)
    assert ad.boxes_overlap(a, b, gap=1.0)


def test_two_views_at_the_default_position_are_reported_as_overlapping():
    """insert_drawing_view defaults to x=150, y=150.

    Two calls without explicit positions stack both views on the same centre,
    and nothing in the old code said so.
    """
    views = [
        {"name": "Drawing View1", "outline_mm": [100, 100, 200, 200], "scale_ratio": (1, 1)},
        {"name": "Drawing View2", "outline_mm": [100, 100, 200, 200], "scale_ratio": (1, 1)},
    ]
    issues = ad.check_sheet_layout(views, [25, 10, 410, 287], None, ad.SheetConfig())
    assert any(i["code"] == "E06" and i["severity"] == "critical" for i in issues)


def test_a_view_hanging_off_the_sheet_is_critical():
    views = [{"name": "V1", "outline_mm": [300, 100, 700, 200], "scale_ratio": (1, 1)}]
    issues = ad.check_sheet_layout(views, [25, 10, 410, 287], None, ad.SheetConfig())
    assert [i["code"] for i in issues] == ["E07"]


def test_a_view_over_the_title_block_is_critical():
    cfg = ad.SheetConfig()
    title = cfg.title_block_box(420, 297)
    views = [{"name": "V1", "outline_mm": [250, 12, 400, 60], "scale_ratio": (1, 1)}]
    issues = ad.check_sheet_layout(views, cfg.usable_area(420, 297), title, cfg)
    assert any(i["code"] == "E07" for i in issues)


def test_a_clean_layout_produces_no_issues():
    cfg = ad.SheetConfig()
    views = [
        {"name": "V1", "outline_mm": [40, 150, 180, 260], "scale_ratio": (1, 10)},
        {"name": "V2", "outline_mm": [220, 150, 360, 260], "scale_ratio": (1, 10)},
    ]
    issues = ad.check_sheet_layout(views, cfg.usable_area(420, 297),
                                   cfg.title_block_box(420, 297), cfg)
    assert issues == []


def test_the_usable_area_keeps_the_binding_margin_on_the_left():
    """ABNT NBR 10068: 25 mm on the filing edge, 10 mm elsewhere."""
    area = ad.SheetConfig().usable_area(420, 297)
    assert area == [25.0, 10.0, 410.0, 287.0]


def test_the_title_block_is_not_cut_off_the_whole_bottom_band():
    """Subtracting the full band would reject legal layouts.

    A view low on the sheet but left of the title block is fine, and the
    checker has to agree.
    """
    cfg = ad.SheetConfig()
    low_left_view = [30, 12, 200, 60]
    assert ad.box_contains(cfg.usable_area(420, 297), low_left_view)
    assert not ad.boxes_overlap(low_left_view, cfg.title_block_box(420, 297))


def test_free_rectangles_finds_space_beside_an_occupied_box():
    free = ad.free_rectangles([0, 0, 100, 100], [[0, 0, 40, 100]], min_size=(10, 10))
    assert any(r[0] >= 40 and r[2] == 100 for r in free)


def test_free_rectangles_of_an_empty_area_is_the_area():
    assert ad.free_rectangles([0, 0, 100, 50], []) == [[0, 0, 100, 50]]


# ---------------------------------------------------------------------------
# Text placement
# ---------------------------------------------------------------------------


def test_the_default_text_position_is_never_the_sheet_origin():
    """place_x=0, place_y=0 was the old default: the bottom-left corner of
    the sheet, outside the frame, for every dimension the model did not
    position by hand."""
    for side in ad.SIDES:
        x, y = ad.place_dimension_text([0.1, 0.1, 0.2, 0.2], side=side, offset_m=0.012)
        assert (x, y) != (0.0, 0.0)


def test_text_is_placed_outside_the_view_on_the_named_side():
    box = [0.1, 0.1, 0.2, 0.2]
    assert ad.place_dimension_text(box, "above", 0.012)[1] == pytest.approx(0.212)
    assert ad.place_dimension_text(box, "below", 0.012)[1] == pytest.approx(0.088)
    assert ad.place_dimension_text(box, "left", 0.012)[0] == pytest.approx(0.088)
    assert ad.place_dimension_text(box, "right", 0.012)[0] == pytest.approx(0.212)


def test_an_unknown_side_is_refused_with_the_valid_options():
    with pytest.raises(ValueError, match="above"):
        ad.place_dimension_text([0, 0, 1, 1], side="northwest")


def test_a_horizontal_edge_dimensions_above_or_below_by_which_is_nearer():
    box = [0.0, 0.0, 0.1, 0.1]
    top = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0.1, 0), (0.1, 0.1, 0)))
    bottom = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.1, 0, 0)))
    assert ad.side_for_edge(top, box) == "above"
    assert ad.side_for_edge(bottom, box) == "below"


def test_a_vertical_edge_dimensions_left_or_right():
    box = [0.0, 0.0, 0.1, 0.1]
    left = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0, 0.1, 0)))
    right = ad.classify_edge(IDENTITY_XFORM, line_params((0.1, 0, 0), (0.1, 0.1, 0)))
    assert ad.side_for_edge(left, box) == "left"
    assert ad.side_for_edge(right, box) == "right"


def test_stacked_dimensions_are_staggered_along_the_side():
    offsets = [ad.stagger_along(i, 4) for i in range(4)]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == 4
    assert all(0.0 <= o <= 1.0 for o in offsets)


def test_a_single_dimension_is_centred():
    assert ad.stagger_along(0, 1) == 0.5


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def test_an_unknown_issue_code_is_a_programming_error():
    with pytest.raises(ValueError, match="Unknown issue code"):
        ad.issue("E99", "critical", "nope")


def test_an_unknown_severity_is_a_programming_error():
    with pytest.raises(ValueError, match="severity"):
        ad.issue("E01", "fatal", "nope")


def test_worst_status_ranks_critical_over_warning_over_clean():
    assert ad.worst_status([]) == "approved"
    assert ad.worst_status([ad.issue("E03", "warning", "x")]) == "warning"
    assert ad.worst_status([ad.issue("E03", "warning", "x"),
                            ad.issue("E01", "critical", "y")]) == "critical"


# ---------------------------------------------------------------------------
# The assertion that closes the loop
# ---------------------------------------------------------------------------


def test_a_dimension_that_measures_the_wrong_thing_is_reported():
    """The complaint, as a test.

    The beam is 2400 mm; the dimension landed on the 150 mm flange. The old
    tool echoed its inputs and said success. Now it is a critical E01.
    """
    issues = ad.check_measured_value(150.0, expected_mm=2400.0, ref="V1#D1")
    assert [i["code"] for i in issues] == ["E01"]
    assert "150.0" in issues[0]["message"] and "2400.0" in issues[0]["message"]


def test_a_dimension_within_tolerance_is_silent():
    assert ad.check_measured_value(2400.2, expected_mm=2400.0, tolerance_mm=0.5) == []


def test_no_expectation_means_no_assertion():
    """expected_mm is optional: the tool must stay usable when the model
    genuinely does not know the value yet."""
    assert ad.check_measured_value(123.0, expected_mm=None) == []


def test_a_dimension_attached_to_one_entity_when_two_were_asked_is_reported():
    """The unchecked second SelectByID2.

    When the second selection silently failed, AddDimension2 dimensioned the
    length of the first edge alone and the tool reported a distance between
    two points. That is E04, and it is detectable by counting attachments.
    """
    issues = ad.check_measured_value(150.0, expected_mm=None, attached_count=1,
                                     expected_attachments=2, ref="V1#D1")
    assert [i["code"] for i in issues] == ["E04"]


def test_both_failures_are_reported_together():
    issues = ad.check_measured_value(150.0, expected_mm=2400.0, attached_count=1,
                                     expected_attachments=2)
    assert {i["code"] for i in issues} == {"E01", "E04"}


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_a_hole_with_no_dimension_is_critical():
    edges = make_edges()
    issues = ad.check_dimension_coverage("V1", edges, dimensions=[])
    hole_issues = [i for i in issues if "hole" in i["message"]]
    assert hole_issues and hole_issues[0]["severity"] == "critical"
    assert hole_issues[0]["code"] == "E02"


def test_a_dimensioned_hole_is_not_reported():
    edges = make_edges()
    hole_id = next(e.id for e in edges if e.kind == "circle")
    dims = [{"id": "V1#D1", "value_mm": 20.0, "unit": "mm", "attached": [hole_id]}]
    issues = ad.check_dimension_coverage("V1", edges, dims)
    assert not [i for i in issues if hole_id in i["refs"]]


def test_only_the_outermost_edges_count_as_missing_overall_dimensions():
    """Requiring a dimension on every edge is the wrong rule.

    A rolled beam has dozens of flange and silhouette edges that no
    draughtsman dimensions; flagging them all buries the real findings.
    """
    edges = make_edges()
    extremes = ad.extreme_edges(edges)
    assert len(extremes) == 4
    assert all(e.kind == "line" for e in extremes)


def test_an_inclined_edge_without_a_dimension_is_flagged_as_a_cut_angle():
    """E14: a tube cut at 45 degrees with no angle stated is unmanufacturable."""
    cut = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.05, 0.05, 0)))
    cut.id = "V1#E9"
    issues = ad.check_dimension_coverage("V1", [cut], dimensions=[])
    assert any("angle" in i["message"] for i in issues)


def test_a_tiny_edge_is_detail_and_is_not_flagged():
    """A 1 mm chamfer run-out is not a missing dimension."""
    chamfer = ad.classify_edge(IDENTITY_XFORM, line_params((0, 0, 0), (0.0007, 0.0007, 0)))
    chamfer.id = "V1#E9"
    assert ad.check_dimension_coverage("V1", [chamfer], dimensions=[]) == []


def test_the_same_measurement_twice_is_a_duplicate():
    dims = [
        {"id": "V1#D1", "value_mm": 100.0, "unit": "mm", "attached": ["V1#E1"]},
        {"id": "V1#D2", "value_mm": 100.0, "unit": "mm", "attached": ["V1#E1"]},
    ]
    groups = ad.group_duplicate_dimensions(dims)
    assert len(groups) == 1 and len(groups[0]) == 2


def test_two_different_features_that_happen_to_measure_the_same_are_not_duplicates():
    """Two separate 20 mm holes are not a duplicated dimension.

    Grouping on value alone would call them one, and the real duplicate --
    the same edge dimensioned twice -- would be lost in the noise.
    """
    dims = [
        {"id": "V1#D1", "value_mm": 20.0, "unit": "mm", "attached": ["V1#E5"]},
        {"id": "V1#D2", "value_mm": 20.0, "unit": "mm", "attached": ["V1#E6"]},
    ]
    assert ad.group_duplicate_dimensions(dims) == []


def test_a_length_and_an_angle_with_the_same_number_are_not_duplicates():
    dims = [
        {"id": "V1#D1", "value_mm": 45.0, "unit": "mm", "attached": ["V1#E1"]},
        {"id": "V1#D2", "value_mm": 45.0, "unit": "deg", "attached": ["V1#E1"]},
    ]
    assert ad.group_duplicate_dimensions(dims) == []


def test_a_dangling_dimension_is_critical():
    dims = [{"id": "V1#D1", "value_mm": 100.0, "unit": "mm",
             "attached": [None], "dangling": True}]
    issues = ad.check_dimension_coverage("V1", [], dims)
    assert [i["code"] for i in issues] == ["E16"]
    assert issues[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# Text-position checks
# ---------------------------------------------------------------------------


def test_text_at_the_sheet_origin_is_critical():
    views = [{"name": "V1", "outline_mm": [40, 40, 140, 140],
              "usable_area_mm": [25, 10, 410, 287],
              "dimensions": [{"id": "V1#D1", "text_position_mm": [0, 0]}]}]
    issues = ad.check_text_positions(views, ad.SheetConfig())
    assert [i["code"] for i in issues] == ["E05"]
    assert issues[0]["severity"] == "critical"


def test_text_sitting_on_another_view_is_a_warning():
    views = [
        {"name": "V1", "outline_mm": [40, 40, 140, 140],
         "usable_area_mm": [25, 10, 410, 287],
         "dimensions": [{"id": "V1#D1", "text_position_mm": [250, 100]}]},
        {"name": "V2", "outline_mm": [200, 40, 300, 140],
         "usable_area_mm": [25, 10, 410, 287], "dimensions": []},
    ]
    issues = ad.check_text_positions(views, ad.SheetConfig())
    assert any(i["code"] == "E05" and "V2" in i["message"] for i in issues)


def test_well_placed_text_produces_no_issues():
    views = [{"name": "V1", "outline_mm": [40, 40, 140, 140],
              "usable_area_mm": [25, 10, 410, 287],
              "dimensions": [{"id": "V1#D1", "text_position_mm": [90, 160]}]}]
    assert ad.check_text_positions(views, ad.SheetConfig()) == []


# ---------------------------------------------------------------------------
# Entity registry
# ---------------------------------------------------------------------------


def test_an_id_resolves_back_to_its_payload():
    reg = ad.EntityRegistry()
    eid = reg.register("Drawing1", "V1", 1, {"entity": "COM-pointer"})
    assert eid == "V1#E1"
    assert reg.resolve("Drawing1", eid)["entity"] == "COM-pointer"


def test_an_unknown_id_says_how_to_get_a_real_one():
    reg = ad.EntityRegistry()
    with pytest.raises(LookupError, match="get_view_entities"):
        reg.resolve("Drawing1", "V1#E99")


def test_a_rebuild_expires_every_id_instead_of_resolving_a_stale_pointer():
    """IDs that outlive a rebuild are worse than no IDs.

    A stale COM pointer either throws from deep inside a COM call or, worse,
    resolves to different geometry. The generation counter turns that into a
    clear "read again".
    """
    reg = ad.EntityRegistry()
    eid = reg.register("Drawing1", "V1", 1, {"entity": "x"})
    reg.invalidate("Drawing1")
    with pytest.raises(LookupError, match="rebuilt"):
        reg.resolve("Drawing1", eid)


def test_registries_of_two_documents_do_not_collide():
    reg = ad.EntityRegistry()
    reg.register("A.slddrw", "V1", 1, {"entity": "a"})
    reg.register("B.slddrw", "V1", 1, {"entity": "b"})
    assert reg.resolve("A.slddrw", "V1#E1")["entity"] == "a"
    assert reg.resolve("B.slddrw", "V1#E1")["entity"] == "b"


def test_ids_can_be_listed_per_view():
    reg = ad.EntityRegistry()
    reg.register("D", "V1", 1, {"entity": "a"})
    reg.register("D", "V1", 2, {"entity": "b"})
    reg.register("D", "V2", 1, {"entity": "c"})
    assert sorted(reg.ids_for_view("D", "V1")) == ["V1#E1", "V1#E2"]
