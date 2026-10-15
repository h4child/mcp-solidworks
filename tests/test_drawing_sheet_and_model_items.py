"""
Tests for set_sheet_scale and insert_model_items -- the two tools added to
close the "folha/tamanho/escala" write-side gap and the "cota automatica"
gap found in AUDITORIA_COBERTURA_FUNCIONAL.md (category O, items O02/O07).

Both call real SolidWorks API methods (ISheet::SetScale,
IDrawingDoc::InsertModelAnnotations2) that are NOT reused from an
already-confirmed-live code path the way get_drawing_layout's read side
is -- see the "UNVERIFIED" note in each tool's docstring and
LIVE_TESTS_REQUIRED.md. These tests only confirm the Python-level argument
plumbing and validation against mocked COM objects, never real SolidWorks
behavior.

Run with:  python -m pytest tests/test_drawing_sheet_and_model_items.py -q
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
    async def _fake_run(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(server, "_run", _fake_run)


# ---------------------------------------------------------------------------
# set_sheet_scale
# ---------------------------------------------------------------------------

class _FakeSheet:
    def __init__(self, scale_ok=True):
        self.scale_calls = []
        self._scale_ok = scale_ok

    def SetScale(self, scale1, scale2, update_view_scale, scale_from_parent):
        self.scale_calls.append((scale1, scale2, update_view_scale, scale_from_parent))
        return self._scale_ok


class _FakeDrawingDoc:
    def __init__(self, doc_type=3):
        self._type = doc_type
        self.rebuild_calls = 0

    @property
    def GetType(self):
        return self._type

    def ForceRebuild3(self, top_only):
        self.rebuild_calls += 1
        return True


@pytest.mark.parametrize("kwargs", [{"scale1": 0}, {"scale2": -1}, {"scale1": 0, "scale2": 0}])
def test_set_sheet_scale_rejects_non_positive_scale(monkeypatch, fake_run, kwargs):
    def _boom():
        raise AssertionError("must not reach SolidWorks for an invalid scale")
    monkeypatch.setattr(server, "_active_doc", _boom)

    with pytest.raises(ValueError):
        call("set_sheet_scale", **kwargs)


def test_set_sheet_scale_targets_the_named_sheet(monkeypatch, fake_run):
    doc = _FakeDrawingDoc()
    sheet = _FakeSheet()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    monkeypatch.setattr(server, "_sheet_and_views", lambda d, name: (sheet, []))
    monkeypatch.setattr(server, "_sheet_properties",
                         lambda s: {"sheet_scale": (1.0, 5.0), "paper_size_code": 0,
                                    "first_angle": True, "width_m": 0.0, "height_m": 0.0})

    result = call("set_sheet_scale", sheet_name="Sheet2", scale1=1.0, scale2=5.0)

    assert sheet.scale_calls == [(1.0, 5.0, True, False)]
    assert result["applied"] is True
    assert result["sheet"] == "Sheet2"
    assert result["scale_read_back"] == [1.0, 5.0]
    assert doc.rebuild_calls == 1


def test_set_sheet_scale_uses_the_current_sheet_when_name_omitted(monkeypatch, fake_run):
    doc = _FakeDrawingDoc()
    sheet = _FakeSheet()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    monkeypatch.setattr(server, "_com_member", lambda obj, member: sheet if member == "GetCurrentSheet" else "Sheet1")
    monkeypatch.setattr(server.win32com.client, "Dispatch", lambda x: x)
    monkeypatch.setattr(server, "_sheet_properties",
                         lambda s: {"sheet_scale": (1.0, 2.0), "paper_size_code": 0,
                                    "first_angle": True, "width_m": 0.0, "height_m": 0.0})

    result = call("set_sheet_scale", scale1=1.0, scale2=2.0)

    assert sheet.scale_calls == [(1.0, 2.0, True, False)]
    assert result["sheet"] == "Sheet1"


def test_set_sheet_scale_reports_honest_failure(monkeypatch, fake_run):
    doc = _FakeDrawingDoc()
    sheet = _FakeSheet(scale_ok=False)
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    monkeypatch.setattr(server, "_sheet_and_views", lambda d, name: (sheet, []))

    result = call("set_sheet_scale", sheet_name="Sheet1", scale1=1.0, scale2=3.0)

    assert result["applied"] is False
    assert "scale_read_back" not in result
    assert doc.rebuild_calls == 0


# ---------------------------------------------------------------------------
# insert_model_items
# ---------------------------------------------------------------------------

class _FakeDrawingDocForModelItems(_FakeDrawingDoc):
    def __init__(self, insert_ok=True):
        super().__init__()
        self.activated = []
        self.insert_calls = []
        self._insert_ok = insert_ok

    def ActivateView(self, name):
        self.activated.append(name)

    def InsertModelAnnotations2(self, flag, detailing_option, datum_flag, use_dim_layer, layer_name):
        self.insert_calls.append((flag, detailing_option, datum_flag, use_dim_layer, layer_name))
        return self._insert_ok


def test_insert_model_items_requires_at_least_one_flag(monkeypatch, fake_run):
    def _boom():
        raise AssertionError("must not reach SolidWorks with nothing requested")
    monkeypatch.setattr(server, "_active_doc", _boom)

    with pytest.raises(ValueError):
        call("insert_model_items", view_name="Front", dimensions=False)


def test_insert_model_items_combines_flags_and_activates_the_view(monkeypatch, fake_run):
    doc = _FakeDrawingDocForModelItems()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    monkeypatch.setattr(server, "_find_view", lambda d, name: object())

    result = call("insert_model_items", view_name="Front", dimensions=True, notes=True)

    assert doc.activated == ["Front"]
    assert doc.insert_calls == [(1 | 2, 0, 0, False, "")]
    assert result["applied"] is True
    assert result["flag"] == 3
    assert result["requested"] == {"dimensions": True, "notes": True}


def test_insert_model_items_raises_with_available_views_when_missing(monkeypatch, fake_run):
    doc = _FakeDrawingDocForModelItems()
    monkeypatch.setattr(server, "_active_doc", lambda: doc)

    def _find_view(d, name):
        raise RuntimeError(f"No drawing view named '{name}'. Available: Front, Top.")
    monkeypatch.setattr(server, "_find_view", _find_view)

    with pytest.raises(RuntimeError, match="Available: Front, Top"):
        call("insert_model_items", view_name="Nope")

    assert doc.activated == []
    assert doc.insert_calls == []


def test_insert_model_items_reports_honest_failure(monkeypatch, fake_run):
    doc = _FakeDrawingDocForModelItems(insert_ok=False)
    monkeypatch.setattr(server, "_active_doc", lambda: doc)
    monkeypatch.setattr(server, "_find_view", lambda d, name: object())

    result = call("insert_model_items", view_name="Front")

    assert result["applied"] is False
