"""
COM-free tests for the structure/profile tools (v5.20.0):
create_profile_part, create_tube_part, add_mate_by_name, interference_report,
set_view_direction and their pure helpers. Nothing here talks to SolidWorks;
the orchestration tests replace the tool-to-tool calls with fakes.
"""
import asyncio
import inspect
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client", reason="server needs pywin32")
import server  # noqa: E402

NEW_TOOLS = ("create_profile_part", "create_tube_part", "add_mate_by_name",
             "interference_report", "set_view_direction")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- registration
@pytest.mark.parametrize("name", NEW_TOOLS)
def test_new_tools_are_registered_with_docs_and_annotations(name):
    tool = server.mcp._tool_manager._tools[name]
    assert tool.description and len(tool.description) > 80
    assert tool.annotations is not None


# ------------------------------------------------------------- _validate_shape
def test_validate_rectangle_ok_and_lowercases():
    spec = server._validate_shape({"shape": "Rectangle", "x1": 0, "y1": 0, "x2": 5, "y2": 2}, "o")
    assert spec["shape"] == "rectangle"


@pytest.mark.parametrize("bad, text", [
    ("nope", "must be an object"),
    ({"shape": "star"}, "unknown shape"),
    ({"shape": "rectangle", "x1": 0, "y1": 0, "x2": 1}, "missing y2"),
    ({"shape": "rectangle", "x1": 0, "y1": 0, "x2": 0, "y2": 3}, "zero-area"),
    ({"shape": "circle", "cx": 0, "cy": 0}, "diameter"),
    ({"shape": "circle", "cx": 0, "cy": 0, "diameter": 0}, "positive"),
    ({"shape": "polygon", "points": [[0, 0], [1, 1]]}, "at least 3"),
    ({"shape": "slot", "cx": 0, "cy": 0, "length": 5, "width": 5}, "0 < width < length"),
    ({"shape": "slot", "cx": 0, "cy": 0, "length": 10, "width": 0}, "0 < width < length"),
])
def test_validate_rejects_bad_shapes(bad, text):
    with pytest.raises(ValueError) as exc:
        server._validate_shape(bad, "outline")
    assert text in str(exc.value)


def test_circle_by_radius_is_accepted():
    spec = server._validate_shape({"shape": "circle", "cx": 1, "cy": 1, "radius": 4}, "h")
    assert server._shape_area_mm2(spec) == pytest.approx(math.pi * 16)


# -------------------------------------------------------------- _shape_area_mm2
def test_areas():
    rect = {"shape": "rectangle", "x1": 10, "y1": 5, "x2": 0, "y2": 0}
    assert server._shape_area_mm2(rect) == 50
    slot = {"shape": "slot", "cx": 0, "cy": 0, "length": 30, "width": 10}
    assert server._shape_area_mm2(slot) == pytest.approx(10 * 20 + math.pi * 25)
    tri = {"shape": "polygon", "points": [[0, 0], [4, 0], [0, 3]]}
    assert server._shape_area_mm2(tri) == pytest.approx(6)
    # clockwise winding must give the same positive area
    tri_cw = {"shape": "polygon", "points": [[0, 0], [0, 3], [4, 0]]}
    assert server._shape_area_mm2(tri_cw) == pytest.approx(6)


# ------------------------------------------------------------------ _tube_specs
def _area(spec):
    return server._shape_area_mm2(spec)


def test_rectangular_tube_60x40x3():
    outline, bore = server._tube_specs("rectangular", 60, 40, 3, None)
    assert _area(outline) == 2400
    assert (bore["x2"] - bore["x1"], bore["y2"] - bore["y1"]) == (54, 34)
    assert _area(outline) - _area(bore) == 2400 - 54 * 34


def test_square_tube_ignores_height():
    outline, bore = server._tube_specs("Square", 60, 999, 3, None)
    assert _area(outline) == 3600 and _area(bore) == 54 * 54


def test_round_tube_33_7x2_6():
    outline, bore = server._tube_specs("round", 0, 0, 2.6, 33.7)
    assert outline["diameter"] == 33.7 and bore["diameter"] == pytest.approx(28.5)
    assert _area(outline) - _area(bore) == pytest.approx(math.pi / 4 * (33.7**2 - 28.5**2))


@pytest.mark.parametrize("args", [
    ("rectangular", 60, 40, 0, None), ("rectangular", 60, 40, 20, None),
    ("rectangular", 0, 40, 3, None), ("round", 0, 0, 3, None),
    ("round", 0, 0, 20, 30), ("oval", 10, 10, 1, None)])
def test_tube_specs_rejects_bad_input(args):
    with pytest.raises(ValueError):
        server._tube_specs(*args)


# ------------------------------------------------------------------ _view_basis
def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_view_basis_is_orthonormal_right_handed():
    right, up, back = server._view_basis([-1, -1, 0.8], [0, 0, 1])
    for v in (right, up, back):
        assert _dot(v, v) == pytest.approx(1)
    assert _dot(right, up) == pytest.approx(0) and _dot(right, back) == pytest.approx(0)
    assert _dot(up, back) == pytest.approx(0)
    # right x up == back
    cross = (right[1] * up[2] - right[2] * up[1], right[2] * up[0] - right[0] * up[2],
             right[0] * up[1] - right[1] * up[0])
    assert cross == pytest.approx(back)
    assert up[2] > 0, "model +Z must point up on screen"


def test_view_basis_front_view():
    right, up, back = server._view_basis([0, 0, 1], [0, 1, 0])
    assert right == pytest.approx((1, 0, 0)) and up == pytest.approx((0, 1, 0))


def test_view_basis_rejects_degenerate():
    with pytest.raises(ValueError):
        server._view_basis([0, 0, 0], [0, 0, 1])
    with pytest.raises(ValueError):
        server._view_basis([0, 0, 2], [0, 0, 1])


def test_set_view_direction_validates_before_touching_com():
    with pytest.raises(ValueError):
        run(server.set_view_direction([1, 1]))
    with pytest.raises(ValueError):
        run(server.set_view_direction([1, 1, 1], output_path="x.jpg"))


# ------------------------------------------------------- component selectors
def test_component_plane_selector_flat_and_nested():
    assert server._component_plane_selector("Plano frontal", "Perna-1", "Escada") == \
        "Plano frontal@Perna-1@Escada"
    assert server._component_plane_selector("Plano frontal", "Sub-1/Perna-2", "Escada") == \
        "Plano frontal@Perna-2@Sub-1@Escada"
    with pytest.raises(ValueError):
        server._component_plane_selector("P", " / ", "A")


def test_entity_selector_variants(monkeypatch):
    monkeypatch.setattr(server, "_standard_plane_name", lambda a, w: {"front": "Plano frontal"}[w])
    monkeypatch.setattr(server, "_doc_title", lambda a: "Escada.SLDASM")
    assert server._entity_selector(None, {"plane": "front", "component": "Perna-1"}) == \
        ("PLANE", "Plano frontal@Perna-1@Escada", (0.0, 0.0, 0.0))
    assert server._entity_selector(None, {"plane": "front"})[1] == "Plano frontal"
    assert server._entity_selector(None, {"plane": "Plano Especial"})[1] == "Plano Especial"
    kind, name, pt = server._entity_selector(None, {"face": {"x": 100, "y": 0, "z": 50, "unit": "mm"}})
    assert (kind, name) == ("FACE", "") and pt == pytest.approx((0.1, 0.0, 0.05))
    kind, _, _ = server._entity_selector(None, {"edge": {"x": 1, "y": 2, "z": 3}})
    assert kind == "EDGE"
    with pytest.raises(ValueError):
        server._entity_selector(None, {"component": "x"})
    with pytest.raises(ValueError):
        server._entity_selector(None, "front")


def test_add_mate_by_name_rejects_unknown_type_and_align(monkeypatch):
    class FakeAssy:
        pass
    monkeypatch.setattr(server, "_active_assembly", lambda: FakeAssy())
    with pytest.raises(ValueError, match="Unknown mate_type"):
        run(server.add_mate_by_name("glue", {"plane": "front"}, {"plane": "top"}))
    with pytest.raises(ValueError, match="Unknown align"):
        run(server.add_mate_by_name("coincident", {"plane": "front"}, {"plane": "top"}, align="up"))


def test_mate_codes_match_swmatetype():
    m = server._NAMED_MATE_TYPES
    assert (m["coincident"], m["concentric"], m["perpendicular"], m["parallel"],
            m["tangent"], m["distance"], m["angle"], m["lock"]) == (0, 1, 2, 3, 4, 5, 6, 16)


def test_add_mate_by_name_has_allow_move():
    assert "allow_move" in inspect.signature(server.add_mate_by_name).parameters


# ---------------------------------------------- orchestration with fakes (no COM)
class Recorder:
    def __init__(self, measured_scale=1.0, material_fails=False):
        self.calls = []
        self.scale = measured_scale
        self.material_fails = material_fails
        self.expected_m3 = None

    def install(self, monkeypatch):
        async def create_new_part():
            self.calls.append(("new",)); return {"title": "Peca1"}

        async def create_sketch(plane):
            self.calls.append(("sketch", plane))

        async def draw_rectangle(x1, y1, x2, y2, unit, tol):
            self.calls.append(("rect", x1, y1, x2, y2)); return {}

        async def draw_circle(x, y, r, unit, tol):
            self.calls.append(("circle", x, y, r)); return {}

        async def draw_profile(points, closed, unit, tol):
            self.calls.append(("poly", len(points))); return {}

        async def close_sketch():
            self.calls.append(("close_sketch",))

        async def extrude_sketch(depth, both, merge, unit, tol):
            self.calls.append(("extrude", depth, both)); return {"verified": True}

        async def move_copy_body(body, dx, dy, dz, rx=0, ry=0, rz=0, unit=None):
            self.calls.append(("rotate", body, rx, ry, rz))

        async def set_material(m):
            self.calls.append(("material", m))
            if self.material_fails:
                raise RuntimeError("density unchanged\nmore")

        async def set_custom_property(n, v):
            self.calls.append(("prop", n, v))

        async def measure_body():
            return {"volume_m3": self.expected_m3 * self.scale, "mass_kg": 1.0,
                    "bounding_box": {"size": {}}}

        async def save_document(path):
            self.calls.append(("save", path)); return {"path": path}

        async def close_document(save=False):
            self.calls.append(("close", save)); return {}

        monkeypatch.setattr(server, "_first_solid_body_name", lambda doc: "Corpo1")
        monkeypatch.setattr(server, "_active_doc", lambda: None)

        async def fake_run(fn):
            return fn()
        monkeypatch.setattr(server, "_run", fake_run)
        for name, fn in list(locals().items()):
            if name not in ("self", "monkeypatch", "fake_run") and callable(fn):
                monkeypatch.setattr(server, name, fn)


def test_create_profile_part_flow_and_volume_check(monkeypatch):
    rec = Recorder()
    rec.expected_m3 = (680 * 220 - 3 * 100) * 4 * 1e-9  # not exactly; set below
    rec.install(monkeypatch)
    outline = {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 680, "y2": 220}
    holes = [{"shape": "circle", "cx": 20, "cy": 20, "diameter": 10}]
    rec.expected_m3 = (680 * 220 - math.pi * 25) * 4 * 1e-9
    res = run(server.create_profile_part("C:/x/Degrau.sldprt", outline, 4, holes, unit="mm"))
    assert res["volume_check"]["ok"] and res["volume_check"]["ratio"] == pytest.approx(1.0)
    kinds = [c[0] for c in rec.calls]
    assert kinds == ["new", "sketch", "rect", "circle", "close_sketch", "extrude",
                     "material", "save", "close"]
    assert res["closed"] and res["material_assigned"] and not res["warnings"]


def test_create_profile_part_flags_a_wrong_volume(monkeypatch):
    rec = Recorder(measured_scale=0.9)
    rec.expected_m3 = 100 * 100 * 8 * 1e-9
    rec.install(monkeypatch)
    res = run(server.create_profile_part(
        "C:/x/Base.sldprt", {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 100, "y2": 100}, 8, unit="mm"))
    assert res["volume_check"]["ok"] is False
    assert any("differs" in w for w in res["warnings"])


def test_create_profile_part_keeps_part_when_material_fails(monkeypatch):
    rec = Recorder(material_fails=True)
    rec.expected_m3 = 50 * 50 * 5 * 1e-9
    rec.install(monkeypatch)
    res = run(server.create_profile_part(
        "C:/x/P.sldprt", {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 50, "y2": 50}, 5, unit="mm"))
    assert res["material_assigned"] is False and ("prop", "Material", "AISI 1020") in rec.calls
    assert ("save", "C:/x/P.sldprt") in rec.calls


def test_create_profile_part_closes_own_doc_on_failure(monkeypatch):
    rec = Recorder()
    rec.expected_m3 = 1
    rec.install(monkeypatch)

    async def boom(depth, both, merge, unit, tol):
        raise RuntimeError("extrude refused")
    monkeypatch.setattr(server, "extrude_sketch", boom)
    with pytest.raises(RuntimeError, match="extrude refused"):
        run(server.create_profile_part(
            "C:/x/P.sldprt", {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 5, "y2": 5}, 5))
    assert rec.calls[-1] == ("close", False)


def test_create_profile_part_rotation_goes_through_move_copy_body(monkeypatch):
    rec = Recorder()
    rec.expected_m3 = 80 * 40 * 100 * 1e-9
    rec.install(monkeypatch)
    run(server.create_profile_part(
        "C:/x/R.sldprt", {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 80, "y2": 40}, 100,
        rotate={"axis": "y", "angle": -42.3}, unit="mm"))
    assert ("rotate", "Corpo1", 0, -42.3, 0) in rec.calls


def test_create_profile_part_validates_before_creating_anything(monkeypatch):
    rec = Recorder()
    rec.expected_m3 = 1
    rec.install(monkeypatch)
    good = {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 5, "y2": 5}
    for kwargs in (dict(depth=0), dict(depth=5, plane="diagonal"), dict(depth=5, save_path="a.txt"),
                   dict(depth=5, rotate={"axis": "q", "angle": 1}),
                   dict(depth=5, holes=[{"shape": "circle", "cx": 0}])):
        kw = dict(save_path="C:/x/a.sldprt", outline=good)
        kw.update(kwargs)
        with pytest.raises(ValueError):
            run(server.create_profile_part(**kw))
    assert rec.calls == [], "no SolidWorks call may happen when the request is invalid"


@pytest.mark.parametrize("axis, plane", [("z", "front"), ("x", "right"), ("y", "top")])
def test_create_tube_part_hollow_volume(monkeypatch, axis, plane):
    rec = Recorder()
    rec.expected_m3 = (60 * 40 - 54 * 34) * 1189 * 1e-9
    rec.install(monkeypatch)
    res = run(server.create_tube_part("C:/x/T.sldprt", "rectangular", 1189, 3,
                                      width=60, height=40, axis=axis, unit="mm"))
    assert ("sketch", plane) in rec.calls
    assert [c for c in rec.calls if c[0] == "rect"].__len__() == 2  # outline + bore
    assert res["volume_check"]["ok"] and res["section"] == "rectangular" and res["axis"] == axis


def test_create_tube_part_round_uses_circles_and_rejects_bad_axis(monkeypatch):
    rec = Recorder()
    rec.expected_m3 = math.pi / 4 * (33.7**2 - 28.5**2) * 700 * 1e-9
    rec.install(monkeypatch)
    res = run(server.create_tube_part("C:/x/R.sldprt", "round", 700, 2.6, outer_diameter=33.7, unit="mm"))
    assert len([c for c in rec.calls if c[0] == "circle"]) == 2 and res["volume_check"]["ok"]
    with pytest.raises(ValueError):
        run(server.create_tube_part("C:/x/R.sldprt", "round", 700, 2.6, outer_diameter=33.7, axis="w"))


def test_default_unit_conversion_in_expected_volume(monkeypatch):
    """Dimensions given in metres must still be compared in mm3."""
    rec = Recorder()
    rec.expected_m3 = 0.1 * 0.1 * 0.01
    rec.install(monkeypatch)
    res = run(server.create_profile_part(
        "C:/x/M.sldprt", {"shape": "rectangle", "x1": 0, "y1": 0, "x2": 0.1, "y2": 0.1}, 0.01, unit="m"))
    assert res["volume_check"]["ok"], res["volume_check"]
