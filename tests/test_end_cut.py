"""
COM-free tests for the end-cut tool (v5.21.0): cut_part_end and its pure helpers.
Nothing here talks to SolidWorks; the orchestration test replaces _run and the
tool-to-tool calls with fakes.
"""
import asyncio
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client", reason="server needs pywin32")
import server  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------- registration
def test_cut_part_end_is_registered_with_docs_and_annotations():
    tool = server.mcp._tool_manager._tools["cut_part_end"]
    assert tool.description and "PLANE" in tool.description and len(tool.description) > 400
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True   # it edits (and saves over) a part
    assert server.TIMEOUT_BUDGET_OVERRIDES["cut_part_end"] >= 120


# ------------------------------------------------------------ _end_cut_distance
def test_distance_from_offset_normalises_the_normal():
    n, d = server._end_cut_distance([0, 0, 5], 900, None, None)
    assert n == (0.0, 0.0, 1.0) and d == 900


def test_distance_from_point_is_n_dot_p():
    n, d = server._end_cut_distance([1, 0, 1], None, [30, 7, 800], None)
    assert d == pytest.approx((30 + 800) / math.sqrt(2))
    assert n[0] == pytest.approx(math.sqrt(0.5))


def test_origin_shifts_the_plane_into_part_coordinates():
    # a vertical plane x = 880 in the assembly, part origin at x = -26.9 -> x = 906.9 in the part
    n, d = server._end_cut_distance([1, 0, 0], 880, None, [-26.9, 360, 29.6])
    assert d == pytest.approx(906.9)
    n, d = server._end_cut_distance([0, 0, 2], None, [0, 0, 1900], [5, 5, 1100])
    assert d == pytest.approx(800)


@pytest.mark.parametrize("args, text", [
    (dict(normal=[0, 0, 0], offset=1), "non-zero"),
    (dict(normal=[1, 0], offset=1), "3 numbers"),
    (dict(normal="x", offset=1), "3 numbers"),
    (dict(normal=[1, 0, 0]), "exactly one of"),
    (dict(normal=[1, 0, 0], offset=1, point=[1, 0, 0]), "exactly one of"),
    (dict(normal=[1, 0, 0], point=[1, 2]), "point must be"),
    (dict(normal=[1, 0, 0], offset=1, origin=[1]), "origin must be"),
])
def test_distance_rejects_bad_input(args, text):
    args = {"offset": None, "point": None, "origin": None, **args}
    with pytest.raises(ValueError) as exc:
        server._end_cut_distance(args["normal"], args["offset"], args["point"], args["origin"])
    assert text in str(exc.value)


# -------------------------------------------------------------- _end_cut_plan
@pytest.mark.parametrize("normal, plane", [
    ((1, 0, 0), "top"),          # normal in XZ (perpendicular to Y) -> Top plane
    ((0, 0, 1), "top"),
    ((0.74, 0, 0.67), "top"),
    ((1, 1, 0), "front"),        # normal in XY -> Front plane
    ((0, 1, 0), "front"),
    ((0, 1, 1), "right"),        # normal in YZ -> Right plane
    ((0, 0, -1), "top"),
])
def test_plan_picks_the_plane_that_contains_the_normal(normal, plane):
    assert server._end_cut_plan(normal, 10.0, 1000.0)["plane"] == plane


@pytest.mark.parametrize("normal, plane", [((0.74, 0, 0.67), "top"), ((1, 1, 0), "front"),
                                           ((0, 1, 1), "right"), ((0, 0, -1), "top"), ((-1, 0, 0), "top")])
def test_plan_polygon_is_the_removed_half_space_in_part_coordinates(normal, plane):
    """Map the sketch rectangle back to part coordinates: two corners sit ON the plane,
    two are `reach` beyond it on the removed side, and the rectangle is not degenerate."""
    plan = server._end_cut_plan(normal, 120.0, 1000.0)
    u, v, _w = server._CUT_PLANE_FRAMES[plane]
    n = plan["normal"]
    pts3 = [tuple(p[0] * u[i] + p[1] * v[i] for i in range(3)) for p in plan["polygon"]]
    dist = [dot(n, p) for p in pts3]
    assert dist[0] == pytest.approx(120.0) and dist[1] == pytest.approx(120.0)
    assert dist[2] == pytest.approx(1120.0) and dist[3] == pytest.approx(1120.0)
    assert plan["depth"] == pytest.approx(2000.0)
    # the origin (and so a part around it) is on the keep side when the offset is positive
    assert dot(n, (0, 0, 0)) < 120.0


def test_plan_negative_offset_and_flipped_normal_are_the_same_plane():
    a = server._end_cut_plan((1, 0, 0), 50.0, 100.0)
    b = server._end_cut_plan((-1, 0, 0), -50.0, 100.0)
    assert {tuple(round(c, 6) for c in p) for p in a["polygon"][:2]} == \
           {tuple(round(c, 6) for c in p) for p in b["polygon"][:2]}
    # ...but they remove opposite sides
    assert a["polygon"][2] != b["polygon"][2]


def test_plan_rejects_compound_angles_and_bad_reach():
    with pytest.raises(ValueError, match="compound angle"):
        server._end_cut_plan((1, 1, 1), 0.0, 100.0)
    with pytest.raises(ValueError, match="reach"):
        server._end_cut_plan((1, 0, 0), 0.0, 0.0)


# ------------------------------------------------- validation before COM
def test_cut_part_end_validates_before_touching_com(monkeypatch):
    async def boom(fn):
        raise AssertionError("COM must not be reached")
    monkeypatch.setattr(server, "_run", boom)
    with pytest.raises(ValueError, match="exactly one of"):
        run(server.cut_part_end([1, 0, 0]))
    with pytest.raises(ValueError, match="compound angle"):
        run(server.cut_part_end([1, 1, 1], offset=5))
    with pytest.raises(ValueError, match="must end in .sldprt"):
        run(server.cut_part_end([1, 0, 0], offset=5, part_path="C:/x/a.sldasm"))
    with pytest.raises(FileNotFoundError):
        run(server.cut_part_end([1, 0, 0], offset=5, part_path="C:/no/such/file.sldprt"))
    with pytest.raises(ValueError, match="Unknown unit"):
        run(server.cut_part_end([1, 0, 0], offset=5, unit="parsec"))


# ------------------------------------------------- orchestration with fakes
class Rig:
    """Scripted stand-ins: _run returns queued results in call order."""

    def __init__(self, vol_before_mm3, vol_after_mm3, far_m, faces_m2, bodies_after=1, probe=None):
        box = [-0.03, -0.03, 0.0, 0.03, 0.03, 0.948]
        self.queue = [
            (False, box, {"volume_m3": vol_before_mm3 * 1e-9}, "Perna.SLDPRT", "C:/x/Perna.SLDPRT"),
            probe if probe is not None else 0.948,          # farthest point along n before cutting
            -0.0,                                           # farthest point along -n
            ([-0.03, -0.03, 0.0, 0.03, 0.03, 0.9], bodies_after,
             {"volume_m3": vol_after_mm3 * 1e-9}, far_m, [{"area_m2": a} for a in faces_m2]),
        ]
        self.calls = []

    def install(self, monkeypatch):
        async def fake_run(fn, *a, **k):
            return self.queue.pop(0)

        async def create_sketch(plane):
            self.calls.append(("sketch", plane))

        async def draw_profile(points, closed, unit, tol):
            self.calls.append(("poly", len(points), closed))

        async def close_sketch():
            self.calls.append(("close_sketch",))

        async def cut_extrude(depth, through_all, both, unit, tol):
            self.calls.append(("cut", round(depth, 3), through_all, both))
            return {"verified": True, "actual_depth": depth, "warnings": []}

        async def save_document(path=None):
            self.calls.append(("save",)); return {"path": "C:/x/Perna.SLDPRT"}

        async def close_document(save=False):
            self.calls.append(("close", save)); return {}

        monkeypatch.setattr(server, "_run", fake_run)
        for name, fn in (("create_sketch", create_sketch), ("draw_profile", draw_profile),
                         ("close_sketch", close_sketch), ("cut_extrude", cut_extrude),
                         ("save_document", save_document), ("close_document", close_document)):
            monkeypatch.setattr(server, name, fn)


def test_cut_part_end_flow_and_result(monkeypatch):
    rig = Rig(648432.0, 615600.0, 0.9, [684e-6])
    rig.install(monkeypatch)
    res = run(server.cut_part_end([0, 0, 1], offset=900, unit="mm"))
    assert [c[0] for c in rig.calls] == ["sketch", "poly", "close_sketch", "cut", "save"]
    assert rig.calls[0][1] == "top" and rig.calls[1][1:] == (4, True) and rig.calls[3][2:] == (False, True)
    assert res["removed_mm3"] == pytest.approx(32832.0)
    assert res["end_on_plane"] is True and res["cut_face_area_mm2"] == pytest.approx(684.0)
    assert res["bodies_after"] == 1 and res["saved"] is True and not res["warnings"]
    assert res["bounding_box_after_mm"]["size"][2] == pytest.approx(900.0)
    assert res["plane"]["offset"] == 900 and res["plane"]["sketch_plane"] == "top"


def test_cut_part_end_without_save_does_not_save(monkeypatch):
    rig = Rig(648432.0, 615600.0, 0.9, [684e-6])
    rig.install(monkeypatch)
    res = run(server.cut_part_end([0, 0, 1], offset=900, unit="mm", save=False))
    assert ("save",) not in rig.calls and res["saved"] is False


def test_cut_part_end_warns_when_the_end_is_not_on_the_plane_or_split(monkeypatch):
    rig = Rig(648432.0, 615600.0, 0.93, [], bodies_after=2)
    rig.install(monkeypatch)
    res = run(server.cut_part_end([0, 0, 1], offset=900, unit="mm"))
    text = " | ".join(res["warnings"])
    assert res["end_on_plane"] is False
    assert "not on the plane" in text and "2 solid bodies" in text and "no planar face" in text
    assert res["cut_face_area_mm2"] is None


def test_cut_part_end_refuses_a_plane_that_misses_the_part(monkeypatch):
    rig = Rig(648432.0, 648432.0, 0.948, [], probe=0.5)      # farthest point 500 mm < plane at 900 mm
    rig.install(monkeypatch)
    with pytest.raises(ValueError, match="does not cut the part"):
        run(server.cut_part_end([0, 0, 1], offset=900, unit="mm"))
    assert rig.calls == []                                   # nothing was sketched


def test_cut_part_end_refuses_a_plane_beyond_the_whole_part(monkeypatch):
    rig = Rig(648432.0, 0.0, 0.0, [], probe=0.948)
    rig.queue[2] = -0.5                                      # n.p of the farthest point along -n: part is wholly beyond
    rig.install(monkeypatch)
    with pytest.raises(ValueError, match="beyond the whole part"):
        run(server.cut_part_end([0, 0, 1], offset=-400, unit="mm"))


def test_cut_part_end_errors_when_nothing_was_removed(monkeypatch):
    rig = Rig(648432.0, 648432.0, 0.9, [684e-6])
    rig.install(monkeypatch)
    with pytest.raises(RuntimeError, match="removed no material"):
        run(server.cut_part_end([0, 0, 1], offset=900, unit="mm"))
