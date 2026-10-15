"""
Tests for the texture U/V mapping, best-effort blend/bump, and PBR-extended
appearance reporting added to close three gaps the audit package flagged as
"lacuna de evidencia": UV texture mapping, advanced PBR materials, and
reflection/roughness/relief configuration (see AUDIT_REPORT.md F1 and the
docstrings of apply_texture / list_p2m_appearances).

None of this touches a real SolidWorks session: ``_active_doc`` and
``_run`` are monkeypatched the same way tests/test_escada_tools.py does it,
so these are pure-Python tests of argument plumbing and defensive COM
property probing -- not proof that the real API behaves as documented.
``LIVE_TESTS_REQUIRED.md`` lists what still needs a live SolidWorks to
confirm, in particular the corrected CreateTexture argument order.

Run with:  python -m pytest tests/test_texture_and_pbr_appearance.py -q
"""

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client",
                    reason="the server module needs pywin32 (Windows only)")

import server  # noqa: E402

TOOLS = server.mcp._tool_manager._tools


def call(name, **kwargs):
    return asyncio.run(TOOLS[name].fn(**kwargs))


@pytest.fixture
def fake_run(monkeypatch):
    """Replace _run with a direct, synchronous call -- the real one submits
    to the single-worker COM executor, which these tests don't need and
    which would otherwise serialize them behind each other for no reason."""
    async def _fake_run(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(server, "_run", _fake_run)


# ---------------------------------------------------------------------------
# _try_set_com_property / _read_first_com_property -- plain Python, no COM
# ---------------------------------------------------------------------------

class _HasBlend:
    BlendColor = None


class _HasNothing:
    pass


class _RaisesOnSet:
    @property
    def Blend(self):
        return None

    @Blend.setter
    def Blend(self, value):
        raise RuntimeError("rejected by this installation")


def test_try_set_com_property_uses_the_first_candidate_present():
    obj = _HasBlend()
    used = server._try_set_com_property(obj, ("BlendColor", "Blend"), True)
    assert used == "BlendColor"
    assert obj.BlendColor is True


def test_try_set_com_property_returns_none_when_nothing_matches():
    obj = _HasNothing()
    used = server._try_set_com_property(obj, ("BlendColor", "Blend"), True)
    assert used is None


def test_try_set_com_property_returns_none_when_the_only_candidate_raises():
    obj = _RaisesOnSet()
    used = server._try_set_com_property(obj, ("Blend",), True)
    assert used is None


def test_read_first_com_property_reports_the_matching_name():
    class _Obj:
        Roughness = 0.42

    value, name = server._read_first_com_property(_Obj(), ("Roughness", "Smoothness"))
    assert value == 0.42
    assert name == "Roughness"


def test_read_first_com_property_is_none_none_when_absent():
    value, name = server._read_first_com_property(_HasNothing(), ("Roughness", "Smoothness"))
    assert value is None
    assert name is None


# ---------------------------------------------------------------------------
# apply_texture -- CreateTexture argument order (the actual bug fix) and
# the new independent u_scale/v_scale control
# ---------------------------------------------------------------------------

class _FakeExtension:
    def __init__(self, texture_factory=None):
        self.create_texture_calls = []
        self.set_texture_calls = []
        self._texture_factory = texture_factory or (lambda: _HasNothing())

    def CreateTexture(self, path, width, height, angle):
        self.create_texture_calls.append((path, width, height, angle))
        return self._texture_factory()

    def SetTexture(self, name, texture):
        self.set_texture_calls.append((name, texture))
        return True


class _FakeDoc:
    def __init__(self, extension):
        self.Extension = extension


def _install_fake_doc(monkeypatch, extension):
    doc = _FakeDoc(extension)
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    return doc


def test_apply_texture_passes_width_height_angle_in_the_documented_order(
    tmp_path, monkeypatch, fake_run,
):
    """The bug: CreateTexture used to receive (path, scale, angle, bool) --
    the caller's angle landed in the Height slot and a 0/1 bool landed in
    the Angle slot, so the requested rotation was never actually sent."""
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension()
    _install_fake_doc(monkeypatch, extension)

    call("apply_texture", texture_path=str(texture_file),
         u_scale=2.0, v_scale=3.0, angle=45.0)

    assert len(extension.create_texture_calls) == 1
    path, width, height, angle = extension.create_texture_calls[0]
    assert path == str(texture_file)
    assert width == 2.0
    assert height == 3.0
    assert angle == 45.0


def test_apply_texture_scale_is_back_compat_for_both_axes(tmp_path, monkeypatch, fake_run):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension()
    _install_fake_doc(monkeypatch, extension)

    call("apply_texture", texture_path=str(texture_file), scale=5.0)

    _, width, height, _ = extension.create_texture_calls[0]
    assert width == 5.0
    assert height == 5.0


def test_apply_texture_u_scale_overrides_scale_for_that_axis_only(tmp_path, monkeypatch, fake_run):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension()
    _install_fake_doc(monkeypatch, extension)

    call("apply_texture", texture_path=str(texture_file), scale=5.0, u_scale=9.0)

    _, width, height, _ = extension.create_texture_calls[0]
    assert width == 9.0
    assert height == 5.0


@pytest.mark.parametrize("kwargs", [
    {"u_scale": 0.0}, {"v_scale": 2_000_000}, {"angle": -1}, {"angle": 361},
])
def test_apply_texture_rejects_out_of_range_values_before_touching_solidworks(
    tmp_path, monkeypatch, fake_run, kwargs,
):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")

    def _boom():
        raise AssertionError("must not reach SolidWorks for an invalid argument")
    monkeypatch.setattr(server, "_active_doc", _boom)

    with pytest.raises(ValueError):
        call("apply_texture", texture_path=str(texture_file), **kwargs)


def test_apply_texture_reports_blend_as_not_applied_when_unsupported(
    tmp_path, monkeypatch, fake_run,
):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension(texture_factory=_HasNothing)
    _install_fake_doc(monkeypatch, extension)

    result = call("apply_texture", texture_path=str(texture_file))

    assert result["blend_with_color"]["applied"] is False
    assert result["blend_with_color"]["applied_via_property"] is None


def test_apply_texture_reports_blend_as_applied_when_the_property_exists(
    tmp_path, monkeypatch, fake_run,
):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension(texture_factory=_HasBlend)
    _install_fake_doc(monkeypatch, extension)

    result = call("apply_texture", texture_path=str(texture_file), blend_with_color=True)

    assert result["blend_with_color"]["applied"] is True
    assert result["blend_with_color"]["applied_via_property"] == "BlendColor"


def test_apply_texture_bump_path_must_exist(tmp_path, monkeypatch, fake_run):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")

    def _boom():
        raise AssertionError("must not reach SolidWorks when the bump file is missing")
    monkeypatch.setattr(server, "_active_doc", _boom)

    with pytest.raises(FileNotFoundError):
        call("apply_texture", texture_path=str(texture_file),
             bump_texture_path=str(tmp_path / "missing_bump.png"))


def test_apply_texture_bump_reports_honest_failure_when_unsupported(
    tmp_path, monkeypatch, fake_run,
):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    bump_file = tmp_path / "bump.png"
    bump_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension(texture_factory=_HasNothing)
    _install_fake_doc(monkeypatch, extension)

    result = call("apply_texture", texture_path=str(texture_file),
                  bump_texture_path=str(bump_file))

    assert result["bump"]["path"] == str(bump_file)
    assert result["bump"]["texture_object_created"] is True
    assert result["bump"]["applied"] is False
    assert result["bump"]["applied_via_property"] is None
    # Two CreateTexture calls: the color texture, then the bump texture.
    assert len(extension.create_texture_calls) == 2


def test_apply_texture_without_bump_path_reports_none(tmp_path, monkeypatch, fake_run):
    texture_file = tmp_path / "wood.png"
    texture_file.write_bytes(b"\x89PNG\r\n")
    extension = _FakeExtension()
    _install_fake_doc(monkeypatch, extension)

    result = call("apply_texture", texture_path=str(texture_file))
    assert result["bump"] is None
    assert len(extension.create_texture_calls) == 1


# ---------------------------------------------------------------------------
# list_p2m_appearances -- pbr_extended reports None/None when a channel
# is not exposed by this SolidWorks installation's render-material object
# ---------------------------------------------------------------------------

class _BareMaterial:
    FileName = "chrome.p2m"
    Ambient = 0.3
    Diffuse = 0.8
    Specular = 0.6


class _MaterialWithRoughness(_BareMaterial):
    Roughness = 0.25


def test_list_p2m_appearances_reports_none_when_pbr_channels_are_absent(monkeypatch, fake_run):
    doc = _FakeDoc(extension=None)
    doc.Extension = type("Ext", (), {
        "GetRenderMaterials2": lambda self, option, names: (_BareMaterial(),),
    })()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)

    result = call("list_p2m_appearances")

    pbr = result["appearances"][0]["pbr_extended"]
    assert pbr["roughness"] == {"value": None, "source_property": None}
    assert pbr["reflectivity"] == {"value": None, "source_property": None}
    assert pbr["bump_map_path"] == {"value": None, "source_property": None}


def test_list_p2m_appearances_reports_the_value_and_source_when_present(monkeypatch, fake_run):
    doc = _FakeDoc(extension=None)
    doc.Extension = type("Ext", (), {
        "GetRenderMaterials2": lambda self, option, names: (_MaterialWithRoughness(),),
    })()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)

    result = call("list_p2m_appearances")

    pbr = result["appearances"][0]["pbr_extended"]
    assert pbr["roughness"] == {"value": 0.25, "source_property": "Roughness"}
    assert pbr["reflectivity"] == {"value": None, "source_property": None}
