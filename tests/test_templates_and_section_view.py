"""
Tests for the System Options default-template tools, create-from-template,
save-as-template, the generic RunCommand escape hatch, and the (explicitly
unverified) dynamic section-view toggle.

None of this touches a real SolidWorks session: ``_connect``/``_active_doc``
and ``_run`` are monkeypatched the same way tests/test_escada_tools.py and
tests/test_texture_and_pbr_appearance.py already do it. See
LIVE_TESTS_REQUIRED.md for what still needs a live SolidWorks session,
especially set_section_view/clear_section_view, whose COM method names are
guesses, not documented API.

Run with:  python -m pytest tests/test_templates_and_section_view.py -q
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
# get_default_templates / set_default_templates
# ---------------------------------------------------------------------------

class _FakeApp:
    def __init__(self, prefs=None):
        self.prefs = dict(prefs or {})
        self.set_calls = []

    def GetUserPreferenceStringValue(self, index):
        return self.prefs.get(index, "")

    def SetUserPreferenceStringValue(self, index, value):
        self.set_calls.append((index, value))
        self.prefs[index] = value
        return True


def test_get_default_templates_reports_existing_and_missing(tmp_path, monkeypatch, fake_run):
    part_tpl = tmp_path / "Part.prtdot"
    part_tpl.write_text("x")
    app = _FakeApp(prefs={8: str(part_tpl), 9: "", 10: str(tmp_path / "missing.drwdot")})
    monkeypatch.setattr(server, "_connect", lambda: app)

    result = call("get_default_templates")

    assert result["part"] == str(part_tpl)
    assert result["part_exists"] is True
    assert result["assembly"] is None
    assert result["drawing_exists"] is False


def test_set_default_templates_rejects_missing_file(tmp_path, monkeypatch, fake_run):
    def _boom():
        raise AssertionError("must not reach SolidWorks for a missing file")
    monkeypatch.setattr(server, "_connect", _boom)

    with pytest.raises(FileNotFoundError):
        call("set_default_templates", part=str(tmp_path / "nope.prtdot"))


def test_set_default_templates_rejects_wrong_extension(tmp_path, monkeypatch, fake_run):
    wrong = tmp_path / "Part.asmdot"
    wrong.write_text("x")

    def _boom():
        raise AssertionError("must not reach SolidWorks for a wrong extension")
    monkeypatch.setattr(server, "_connect", _boom)

    with pytest.raises(ValueError, match="prtdot"):
        call("set_default_templates", part=str(wrong))


def test_set_default_templates_requires_at_least_one_argument(monkeypatch, fake_run):
    app = _FakeApp()
    monkeypatch.setattr(server, "_connect", lambda: app)
    with pytest.raises(ValueError):
        call("set_default_templates")


def test_set_default_templates_applies_and_verifies(tmp_path, monkeypatch, fake_run):
    part_tpl = tmp_path / "Part.prtdot"
    part_tpl.write_text("x")
    app = _FakeApp()
    monkeypatch.setattr(server, "_connect", lambda: app)

    result = call("set_default_templates", part=str(part_tpl))

    assert app.set_calls == [(8, str(part_tpl))]
    assert result["part"]["verified"] is True


# ---------------------------------------------------------------------------
# create_document_from_template
# ---------------------------------------------------------------------------

class _FakeDocForType:
    def __init__(self, doc_type):
        self._type = doc_type

    @property
    def GetType(self):
        return self._type


class _FakeAppForNewDoc:
    def __init__(self, created_type):
        self.ActiveDoc = _FakeDocForType(created_type)
        self.new_document_calls = []

    def NewDocument(self, path, paper_size, width, height):
        self.new_document_calls.append((path, paper_size, width, height))
        return object()


def test_create_document_from_template_rejects_missing_file(tmp_path, monkeypatch, fake_run):
    def _boom():
        raise AssertionError("must not reach SolidWorks for a missing template")
    monkeypatch.setattr(server, "_connect", _boom)

    with pytest.raises(FileNotFoundError):
        call("create_document_from_template", template_path=str(tmp_path / "missing.prtdot"))


def test_create_document_from_template_rejects_unknown_extension(tmp_path, monkeypatch, fake_run):
    bad = tmp_path / "thing.txt"
    bad.write_text("x")

    def _boom():
        raise AssertionError("must not reach SolidWorks for an unknown extension")
    monkeypatch.setattr(server, "_connect", _boom)

    with pytest.raises(ValueError):
        call("create_document_from_template", template_path=str(bad))


def test_create_document_from_template_detects_type_mismatch(tmp_path, monkeypatch, fake_run):
    tpl = tmp_path / "Part.prtdot"
    tpl.write_text("x")
    app = _FakeAppForNewDoc(created_type=2)  # Assembly, not Part
    monkeypatch.setattr(server, "_connect", lambda: app)

    with pytest.raises(RuntimeError, match="Assembly"):
        call("create_document_from_template", template_path=str(tpl))


def test_create_document_from_template_succeeds_and_passes_the_resolved_path(tmp_path, monkeypatch, fake_run):
    tpl = tmp_path / "Part.prtdot"
    tpl.write_text("x")
    app = _FakeAppForNewDoc(created_type=1)  # Part
    monkeypatch.setattr(server, "_connect", lambda: app)
    monkeypatch.setattr(server, "_doc_title", lambda doc: "Part1")

    result = call("create_document_from_template", template_path=str(tpl))

    assert result["type"] == "Part"
    assert app.new_document_calls[0][0] == str(tpl)


# ---------------------------------------------------------------------------
# save_as_template
# ---------------------------------------------------------------------------

class _FakeDocForSaveAs:
    def __init__(self, doc_type, save_ok=True):
        self._type = doc_type
        self._save_ok = save_ok
        self.saved_to = None

    @property
    def GetType(self):
        return self._type

    def SaveAs(self, path):
        self.saved_to = path
        return self._save_ok


def test_save_as_template_rejects_extension_not_matching_doc_type(tmp_path, monkeypatch, fake_run):
    doc = _FakeDocForSaveAs(doc_type=1)  # Part
    monkeypatch.setattr(server, "_active_doc", lambda: doc)

    with pytest.raises(ValueError, match="prtdot"):
        call("save_as_template", filepath=str(tmp_path / "Thing.asmdot"))


def test_save_as_template_succeeds_for_matching_extension(tmp_path, monkeypatch, fake_run):
    target = tmp_path / "Company.prtdot"
    doc = _FakeDocForSaveAs(doc_type=1)  # Part

    def _fake_saveas(path):
        doc.saved_to = path
        target.write_text("template contents")
        return True
    doc.SaveAs = _fake_saveas

    monkeypatch.setattr(server, "_active_doc", lambda: doc)

    result = call("save_as_template", filepath=str(target))

    assert result["path"] == str(target)
    assert doc.saved_to == str(target)


# ---------------------------------------------------------------------------
# run_command_by_id
# ---------------------------------------------------------------------------

def test_run_command_by_id_passes_the_id_through(monkeypatch, fake_run):
    calls = []

    class _App:
        def RunCommand(self, command_id, use_default):
            calls.append((command_id, use_default))
            return True

    monkeypatch.setattr(server, "_connect", lambda: _App())

    result = call("run_command_by_id", command_id=12345)

    assert calls == [(12345, "")]
    assert result == {"command_id": 12345, "accepted": True}


# ---------------------------------------------------------------------------
# _try_call_com_method / set_section_view / clear_section_view
# ---------------------------------------------------------------------------

class _HasOnlySectionViewOn:
    def __init__(self):
        self.calls = []

    def SectionViewOn(self, *args):
        self.calls.append(args)
        return True


class _HasNoSectionViewMethods:
    pass


def test_try_call_com_method_uses_the_first_candidate_present():
    obj = _HasOnlySectionViewOn()
    name, result = server._try_call_com_method(obj, ("SectionViewOn3", "SectionViewOn2", "SectionViewOn"), 1, 2)
    assert name == "SectionViewOn"
    assert result is True
    assert obj.calls == [(1, 2)]


def test_try_call_com_method_returns_none_when_nothing_matches():
    name, result = server._try_call_com_method(_HasNoSectionViewMethods(), ("SectionViewOn3", "SectionViewOn"))
    assert name is None
    assert result is None


def test_set_section_view_rejects_unknown_plane(monkeypatch, fake_run):
    def _boom():
        raise AssertionError("must not reach SolidWorks for an invalid plane")
    monkeypatch.setattr(server, "_active_doc", _boom)

    with pytest.raises(ValueError):
        call("set_section_view", plane="diagonal")


def test_set_section_view_reports_honest_failure_when_unsupported(monkeypatch, fake_run):
    class _Doc:
        Extension = _HasNoSectionViewMethods()
    monkeypatch.setattr(server, "_active_doc", lambda: _Doc())

    result = call("set_section_view", plane="front")

    assert result["applied"] is False
    assert result["applied_via_method"] is None


def test_set_section_view_reports_success_when_a_candidate_works(monkeypatch, fake_run):
    extension = _HasOnlySectionViewOn()

    class _Doc:
        Extension = extension
    monkeypatch.setattr(server, "_active_doc", lambda: _Doc())

    result = call("set_section_view", plane="right", offset=10.0)

    assert result["applied"] is True
    assert result["applied_via_method"] == "SectionViewOn"
    assert len(extension.calls) == 1


def test_clear_section_view_reports_honest_failure_when_unsupported(monkeypatch, fake_run):
    class _Doc:
        Extension = _HasNoSectionViewMethods()
    monkeypatch.setattr(server, "_active_doc", lambda: _Doc())

    result = call("clear_section_view")

    assert result == {"applied_via_method": None, "applied": False}
