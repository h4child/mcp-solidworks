"""
Regression tests for the bug briefing B01-B12.

Each test names the bug it pins. These run WITHOUT SolidWorks: they cover the
argument validation, the material-library parsing and the pure policy that
each fix introduced. The parts that genuinely need a live COM session are
marked in the module docstring of tests/run_*_live_test.py instead, and the
live findings are recorded in the comments of the code they explain.

Run with:  python -m pytest tests/test_bugfixes.py -q
"""

import asyncio
import inspect
import io
import logging
import os
import sys
import textwrap
import tokenize

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client",
                    reason="the server module needs pywin32 (Windows only)")

import server  # noqa: E402

TOOLS = server.mcp._tool_manager._tools


def call(name, **kwargs):
    """Invoke a tool's coroutine and return its result."""
    return asyncio.run(TOOLS[name].fn(**kwargs))


def source_of(name):
    return inspect.getsource(TOOLS[name].fn)


def strip_prose(source: str) -> str:
    """Executable code only: comments, docstrings and literals removed.

    A test that asserts `"bodies[0]" not in source` is satisfied by the code
    and broken by the COMMENT explaining that bodies[0] was the bug. The
    proof and the explanation live in the same file, so the scan has to be
    able to tell them apart -- otherwise documenting a fix breaks its test,
    which teaches exactly the wrong lesson.
    """
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source
    # Blank the comment and string ranges IN PLACE, so that everything else
    # keeps its exact spelling: a scan for "bodies[0]" has to still see
    # "bodies[0]" and not a re-spaced "bodies [ 0 ]".
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            index = row - 1
            if index >= len(lines):
                break
            line = lines[index].rstrip("\n")
            begin = start_col if row == start_row else 0
            finish = end_col if row == end_row else len(line)
            lines[index] = (line[:begin] + " " * max(0, finish - begin)
                            + line[finish:] + "\n")
    return "".join(lines)


def code_of(name):
    """A tool's source with its prose stripped out."""
    return strip_prose(source_of(name))


# ---------------------------------------------------------------------------
# B01 -- set_material silently left mass at water density
# ---------------------------------------------------------------------------

SAMPLE_SLDMAT = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <mstns:materials xmlns:mstns="http://www.solidworks.com/sldmaterials">
      <classification name="Steel">
        <material name="Alloy Steel" matid="3">
          <physicalproperties>
            <EX displayname="Elastic Modulus" value="0.21E+12"/>
            <NUXY displayname="Poisson's Ratio" value="0.28"/>
            <DENS displayname="Density" value="0.77E+04"/>
            <SIGYLD displayname="Yield Strength" value="6.20422E+8"/>
          </physicalproperties>
        </material>
        <material name="ASTM A36 Steel" matid="9">
          <physicalproperties>
            <DENS displayname="Density" value="7850"/>
            <SIGYLD displayname="Yield Strength" value="2.5E+8"/>
          </physicalproperties>
        </material>
      </classification>
      <classification name="Aluminium">
        <material name="6061 Alloy" matid="12">
          <physicalproperties>
            <DENS displayname="Density" value="2700"/>
          </physicalproperties>
        </material>
      </classification>
    </mstns:materials>
    """)


@pytest.fixture
def sample_library(tmp_path):
    path = tmp_path / "sample materials.sldmat"
    path.write_text(SAMPLE_SLDMAT, encoding="utf-8")
    server._material_library_cache.clear()
    yield str(path)
    server._material_library_cache.clear()


def test_the_material_library_is_parsed_from_the_sldmat_file(sample_library):
    """The density has to come from somewhere verifiable.

    SetMaterialPropertyName2 was confirmed live to store a material name
    while leaving the density at water, so a name proves nothing. Reading the
    library file gives a number that can be checked.
    """
    materials = server._read_material_library(sample_library)
    assert set(materials) == {"Alloy Steel", "ASTM A36 Steel", "6061 Alloy"}
    assert materials["Alloy Steel"]["density_kg_m3"] == pytest.approx(7700.0)


def test_density_is_read_from_the_DENS_tag_not_a_tag_called_density(sample_library):
    """A .sldmat names its properties by finite-element short code.

    Density is <DENS value="0.77E+04"/>, not <density>. Looking for the
    readable name finds nothing and silently yields no density at all -- the
    first attempt at this parser did exactly that.
    """
    assert server._library_density("Alloy Steel", sample_library) == pytest.approx(7700.0)
    assert server._library_density("6061 Alloy", sample_library) == pytest.approx(2700.0)


def test_scientific_notation_in_the_library_is_parsed(sample_library):
    """0.77E+04 is 7700, and 6.20422E+8 is 620 MPa."""
    entry = server._read_material_library(sample_library)["Alloy Steel"]
    assert entry["elastic_modulus_pa"] == pytest.approx(2.1e11)
    assert entry["yield_strength_pa"] == pytest.approx(6.20422e8)
    assert entry["poisson_ratio"] == pytest.approx(0.28)


def test_an_unknown_material_has_no_density_rather_than_a_wrong_one(sample_library):
    assert server._library_density("Unobtainium", sample_library) is None


def test_the_library_is_cached_per_file(sample_library):
    server._read_material_library(sample_library)
    assert sample_library in server._material_library_cache


def test_a_missing_library_file_fails_loudly(tmp_path):
    server._material_library_cache.clear()
    with pytest.raises(RuntimeError, match="Could not read the material library"):
        server._read_material_library(str(tmp_path / "nope.sldmat"))


def test_lookup_material_properties_exists_and_is_read_only():
    """The tool that lets a caller compute a correct weight while the
    material assignment itself is broken."""
    assert "lookup_material_properties" in TOOLS
    description = TOOLS["lookup_material_properties"].description.lower()
    assert "read-only" in description
    assert "density" in description


def test_set_material_verifies_by_density_not_by_name():
    """The old check compared the material NAME read back from
    GetMaterialPropertyName2. On this build that reads back as "" even when
    the call reports no error, so the check blamed the wrong thing while the
    real symptom -- the density -- went untested."""
    source = source_of("set_material")
    assert "_library_density" in source
    assert "_document_density" in source
    assert "expected_density_kg_m3" in source


def test_measure_body_warns_when_mass_is_computed_at_water_density():
    """A wrong mass with no error is the worst outcome for a BOM."""
    source = inspect.getsource(server._measure_model_doc)
    assert "density_warning" in source
    assert "1000" in source


def test_create_mass_property_goes_through_the_dispatch_helper():
    """doc.Extension.CreateMassProperty() raised DISP_E_MEMBERNOTFOUND every
    time under dynamic dispatch, so the primary mass path was dead code and
    the GetMassProperties2 fallback did all the work -- which is also why
    Density was never available. Confirmed live, 2026-10-04."""
    source = inspect.getsource(server._measure_model_doc)
    assert '_com_member(doc.Extension, "CreateMassProperty")' in source
    assert "doc.Extension.CreateMassProperty()" not in strip_prose(source)


# ---------------------------------------------------------------------------
# B02 -- only the first assembly level was read
# ---------------------------------------------------------------------------


def test_extract_assembly_bom_exists():
    assert "extract_assembly_bom" in TOOLS


def test_list_components_defaults_to_every_level():
    """GetComponents(True) means "top level only".

    Passing True unconditionally hid every part inside a subassembly -- in an
    assembly built from subassemblies, most of the parts.
    """
    parameters = inspect.signature(TOOLS["list_components"].fn).parameters
    assert "top_level_only" in parameters
    assert parameters["top_level_only"].default is False


def test_the_bom_groups_by_path_and_configuration():
    """Keying on the file path alone summed two configurations of one file
    into a single row -- the same plate in 2 mm and 3 mm became one item."""
    source = source_of("extract_assembly_bom")
    assert 'entry["configuration"]' in source
    assert "key = (entry[\"path\"] or entry[\"name\"], entry[\"configuration\"] or \"\")" in source


def test_components_excluded_from_the_bom_are_not_counted():
    source = source_of("extract_assembly_bom")
    assert "excluded_from_bom" in source


def test_the_old_extract_assembly_data_is_left_intact_but_marked():
    """Callers depend on its exact shape, so it keeps its signature; the
    docstring is what redirects new work."""
    parameters = inspect.signature(TOOLS["extract_assembly_data"].fn).parameters
    assert list(parameters) == ["config"]
    assert "SUPERSEDED" in TOOLS["extract_assembly_data"].description


def test_the_assembly_walk_cannot_recurse_for_ever():
    """A broken assembly can contain a circular reference, and SolidWorks
    lets it exist."""
    source = inspect.getsource(server._walk_assembly)
    assert "visited" in source
    assert "max_depth" in source


def test_the_assembly_walk_is_depth_limited_in_the_tool_signature():
    parameters = inspect.signature(TOOLS["extract_assembly_bom"].fn).parameters
    assert parameters["max_depth"].default == 10


# ---------------------------------------------------------------------------
# B03 -- bounding box of the first body only
# ---------------------------------------------------------------------------


def test_the_bounding_box_unions_every_body():
    """A weldment is multibody by construction, so measuring bodies[0] alone
    reported one member's size as the whole frame's."""
    source = inspect.getsource(server._measure_model_doc)
    assert "bodies[0]" not in strip_prose(source)
    assert "bodies_measured" in source
    assert "body_count" in source


# ---------------------------------------------------------------------------
# B06 -- add_gusset legs were hard-coded in the caller's unit
# ---------------------------------------------------------------------------


def test_add_gusset_takes_its_legs_as_parameters():
    """to_meters(50, unit) with unit="m" built a 50-METRE gusset, and there
    was no way to ask for the size the structure needs."""
    parameters = inspect.signature(TOOLS["add_gusset"].fn).parameters
    for name in ("leg1", "leg2", "leg3", "profile_angle"):
        assert name in parameters, f"add_gusset lacks '{name}'"
    assert parameters["leg1"].default == 50
    assert "to_meters(50, unit)" not in code_of("add_gusset")


@pytest.mark.parametrize("kwargs,message", [
    ({"leg1": 0}, "leg1 must be positive"),
    ({"leg2": -5}, "leg2 must be positive"),
    ({"profile_angle": 0}, "profile_angle must be between"),
    ({"profile_angle": 180}, "profile_angle must be between"),
    ({"thickness": 0}, "Thickness must be positive"),
])
def test_add_gusset_rejects_impossible_geometry_before_touching_solidworks(kwargs, message):
    with pytest.raises(ValueError, match=message):
        call("add_gusset", **kwargs)


def test_a_flat_gusset_needs_a_third_leg():
    with pytest.raises(ValueError, match="positive leg3"):
        call("add_gusset", profile="flat", leg3=0)


def test_the_gusset_reports_the_legs_it_used():
    source = source_of("add_gusset")
    assert '"leg1": leg1' in source and '"leg2": leg2' in source


# ---------------------------------------------------------------------------
# B07 -- create_reference_plane ignored the angle
# ---------------------------------------------------------------------------


def test_the_plane_constraint_is_chosen_before_the_com_call():
    """Distance was always tried first and Angle only as a fallback for when
    Distance returned None. Distance nearly always succeeds, so angle=30
    silently produced a plane PARALLEL to the reference while the return
    value still reported "angle": 30."""
    source = source_of("create_reference_plane")
    assert "if angle == 0:" in source
    assert "constraint, value = 16, angle_rad" in source
    # The giveaway of the old shape: an angle attempt guarded by a failed
    # distance attempt.
    assert "if feat is None and angle != 0" not in code_of("create_reference_plane")


def test_an_angled_plane_can_name_the_axis_to_rotate_about():
    parameters = inspect.signature(TOOLS["create_reference_plane"].fn).parameters
    assert "angle_about" in parameters
    assert parameters["angle_about"].default is None


def test_the_plane_reports_the_constraint_it_actually_used():
    source = source_of("create_reference_plane")
    assert '"constraint": "angle" if constraint == 16 else "distance"' in source


def test_the_plane_reads_its_own_position_back():
    """flip=True with reference='right' has been seen to land the plane at
    X=0 instead of the requested offset. Reporting the real origin turns
    that from a silent wrong answer into something checkable."""
    source = source_of("create_reference_plane")
    assert '"origin"' in source and "GetSpecificFeature2" in source


# ---------------------------------------------------------------------------
# B08 -- the pack_and_go fallback broke references and overwrote files
# ---------------------------------------------------------------------------


def test_the_packaging_fallback_refuses_to_rename_without_fixing_references():
    """Renaming a part while the assembly still points at the old name
    produces a copy that opens with missing references -- worse than no copy,
    and the process rule is "always make a working copy"."""
    source = source_of("pack_and_go")
    assert "if prefix or suffix:" in source
    assert "missing" in source


def test_the_packaging_fallback_disambiguates_colliding_names():
    """Two parts of the same name from different folders used to overwrite
    each other silently, in both the flattened and the models/ layout."""
    source = source_of("pack_and_go")
    assert "collisions" in source
    assert "used_names" in source


def test_the_packaging_fallback_never_overwrites_an_existing_file():
    source = source_of("pack_and_go")
    assert "Refusing to overwrite an existing file" in source


def test_an_incomplete_package_is_an_error_not_a_warning():
    source = source_of("pack_and_go")
    assert "the package is incomplete" in source


# ---------------------------------------------------------------------------
# B09 -- export_document could not write DWG
# ---------------------------------------------------------------------------


def test_dwg_is_an_accepted_export_format():
    """Structural-steel offices exchange DWG with AutoCAD constantly, and the
    drawing-only export_flat_pattern_dxf already accepted it."""
    source = source_of("export_document")
    assert '".dwg"' in source
    assert "dwg" in TOOLS["export_document"].description.lower()


def test_an_unknown_export_format_is_rejected_with_the_valid_list():
    with pytest.raises(ValueError, match="Unsupported export format"):
        call("export_document", filepath="C:/tmp/thing.bogus")


def test_the_drawing_only_formats_are_named_together():
    """PDF had its own one-off check; DWG needed the same rule, so both now
    come from one set rather than two copies that can drift."""
    source = source_of("export_document")
    assert "drawing_only" in source


def test_the_export_confirms_a_file_was_actually_written():
    """SaveAs has been seen to report success without producing a file."""
    source = source_of("export_document")
    assert "no file was written" in source


# ---------------------------------------------------------------------------
# B10 -- add_equation ignored solve=False
# ---------------------------------------------------------------------------


def test_add_equation_only_evaluates_when_asked_to():
    """The condition was `if not solve: _evaluate_equations(...)`, i.e.
    exactly backwards: solve=False added the equation and then evaluated it
    anyway, while the return still reported "solved": False."""
    assert "if solve else None" in code_of("add_equation")
    assert "if not solve:" not in code_of("add_equation")


def test_add_equation_and_set_equation_agree_on_what_solve_means():
    """They disagreed, which is how the bug stayed invisible: set_equation
    was right all along."""
    for name in ("add_equation", "set_equation"):
        assert "_evaluate_equations" in source_of(name)
        assert "if not solve" not in code_of(name)


# ---------------------------------------------------------------------------
# B11 -- flatten_sheet_metal toggled instead of ensuring a state
# ---------------------------------------------------------------------------


def test_flatten_sheet_metal_defaults_to_ensuring_the_flat_state():
    """It worked as a switch: calling a tool named "flatten" twice FOLDED the
    part. An agent that retries after a timeout silently undid its own
    work."""
    parameters = inspect.signature(TOOLS["flatten_sheet_metal"].fn).parameters
    assert "state" in parameters
    assert parameters["state"].default == "flat"


def test_flatten_sheet_metal_rejects_an_unknown_state_without_solidworks():
    with pytest.raises(ValueError, match="state must be"):
        call("flatten_sheet_metal", state="banana")


@pytest.mark.parametrize("state", ["flat", "folded", "toggle"])
def test_the_three_documented_states_are_accepted(state):
    """They must get past validation; what happens next needs a document, so
    the failure here has to be about the document, not the argument."""
    try:
        call("flatten_sheet_metal", state=state)
    except ValueError as exc:
        assert "state must be" not in str(exc), f"'{state}' was rejected"
    except Exception:
        pass  # no SolidWorks / no part: not what this test is about


def test_flatten_reports_whether_it_changed_anything():
    """Idempotence is only useful if the caller can see it happened."""
    source = source_of("flatten_sheet_metal")
    assert '"changed": False' in source and '"changed": True' in source


def test_asking_for_folded_does_not_create_a_flat_pattern():
    """A part with no Flat-Pattern feature is already folded; creating one
    would be a change nobody asked for."""
    source = source_of("flatten_sheet_metal")
    assert 'if wanted == "folded":' in source
    assert "no flat-pattern exists" in source


# ---------------------------------------------------------------------------
# Cross-cutting: arguments are validated before COM is touched
# ---------------------------------------------------------------------------

VALIDATE_FIRST = ["flatten_sheet_metal", "add_gusset", "dimension_by_entity_ids",
                  "get_view_entities"]


@pytest.mark.parametrize("name", VALIDATE_FIRST)
def test_argument_validation_comes_before_the_first_com_call(name):
    """Rejecting a typo should not need a SolidWorks connection.

    It also makes the rule testable: with COM first, every validation test
    fails on "no active document" instead of on the thing being tested.
    """
    source = source_of(name)
    body = source[source.index("def _impl():"):]
    first_raise = body.find("raise ValueError")
    first_com = min((i for i in (body.find("_active_doc()"),
                                 body.find("_active_drawing()"),
                                 body.find("_active_assembly()"))
                     if i != -1), default=-1)
    assert first_raise != -1, f"{name} validates nothing"
    assert first_com == -1 or first_raise < first_com, (
        f"{name} connects to SolidWorks before validating its arguments"
    )


# ---------------------------------------------------------------------------
# R3.20 -- structured call log (one JSON line per tool call)
# ---------------------------------------------------------------------------


def test_every_call_is_logged_as_one_json_object_regardless_of_outcome():
    """The log has to come from one place all 150 tools pass through.

    Every tool defines its own `_impl` closure, so the existing human debug
    log always prints "COM call: _impl" -- useless for telling which tool
    was actually called. This logs through ToolManager.call_tool instead
    (patched once in _install_call_logging), so it can't go stale just
    because a new tool's _impl is named the same as everyone else's.
    """
    import json

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(record.getMessage()))

    handler = _Capture()
    server.log_calls.addHandler(handler)
    try:
        asyncio.run(server.mcp._tool_manager.call_tool("set_units", {"unit": "mm"}))
        with pytest.raises(Exception):
            asyncio.run(server.mcp._tool_manager.call_tool(
                "set_units", {"unit": "not-a-unit"}))
    finally:
        server.log_calls.removeHandler(handler)

    assert len(records) == 2
    ok_record, failed_record = records
    assert ok_record["tool"] == "set_units"
    assert ok_record["arguments"] == {"unit": "mm"}
    assert ok_record["ok"] is True
    assert "error" not in ok_record
    assert isinstance(ok_record["duration_ms"], (int, float))

    assert failed_record["ok"] is False
    assert "error" in failed_record and failed_record["error"]


def test_long_argument_values_are_summarized_not_dropped_or_unbounded():
    """A full macro body or an embedded image in the log would make it
    unreadable; dropping the argument entirely would make it useless for
    telling two calls to the same tool apart."""
    long_value = "x" * 5000
    summary = server._summarize_call_argument(long_value)
    assert isinstance(summary, str)
    assert len(summary) < len(long_value)
    assert "5000 chars" in summary


# ---------------------------------------------------------------------------
# R3.22 -- path policy by project root (writes only)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """A configured allowlist of exactly one root, cleaned up afterwards so
    it can never leak into a test that runs after this one."""
    monkeypatch.setenv(server.PROJECT_ROOTS_ENV, str(tmp_path))
    yield tmp_path


def test_with_no_roots_configured_resolution_is_unrestricted(monkeypatch):
    """A single engineer driving their own already-open SolidWorks needs no
    extra configuration for save_document/export_document/pack_and_go to
    keep working exactly as before this existed."""
    monkeypatch.delenv(server.PROJECT_ROOTS_ENV, raising=False)
    resolved = server._resolve_write_path("relative/out.step")
    assert resolved == os.path.abspath("relative/out.step")


def test_a_path_outside_the_configured_root_is_refused(project_root):
    outside = os.path.join(str(project_root.parent), "elsewhere.step")
    with pytest.raises(PermissionError, match="outside the configured project root"):
        server._resolve_write_path(outside)


def test_a_path_inside_the_configured_root_is_allowed(project_root):
    inside = os.path.join(str(project_root), "sub", "part.step")
    assert server._resolve_write_path(inside) == os.path.abspath(inside)


WRITE_TOOLS_WITH_A_DESTINATION = [
    "save_document", "export_document", "export_flat_pattern_dxf",
    "pack_and_go", "capture_standard_views", "capture_viewport",
]


@pytest.mark.parametrize("name", WRITE_TOOLS_WITH_A_DESTINATION)
def test_every_write_destination_tool_uses_the_shared_resolver(name):
    """A write tool that calls bare os.path.abspath instead of
    _resolve_write_path silently bypasses the allowlist for itself alone --
    exactly the kind of one-tool regression freezing the surface is for."""
    source = source_of(name)
    assert "_resolve_write_path(" in source, (
        f"'{name}' takes a write destination but does not route it through "
        f"_resolve_write_path -- it would bypass {server.PROJECT_ROOTS_ENV}"
    )


# ---------------------------------------------------------------------------
# R3.23 -- macros only from a trusted folder
# ---------------------------------------------------------------------------


@pytest.fixture
def macro_file(tmp_path):
    path = tmp_path / "demo.swp"
    path.write_bytes(b"not a real macro, just needs to exist")
    return path


def test_with_no_macro_roots_configured_any_macro_path_resolves(macro_file, monkeypatch):
    """Unchanged from before this existed, for anyone who hasn't configured
    a macro folder: any existing .swp/.swb/.dll by path."""
    monkeypatch.delenv(server.MACRO_ROOTS_ENV, raising=False)
    assert server._resolve_macro_path(str(macro_file)) == os.path.abspath(str(macro_file))


def test_a_macro_outside_the_configured_root_is_refused(macro_file, monkeypatch, tmp_path_factory):
    other_root = tmp_path_factory.mktemp("trusted_macros")
    monkeypatch.setenv(server.MACRO_ROOTS_ENV, str(other_root))
    with pytest.raises(PermissionError, match="outside the configured macro root"):
        server._resolve_macro_path(str(macro_file))


def test_a_macro_inside_the_configured_root_is_allowed(macro_file, monkeypatch):
    monkeypatch.setenv(server.MACRO_ROOTS_ENV, str(macro_file.parent))
    assert server._resolve_macro_path(str(macro_file)) == os.path.abspath(str(macro_file))


def test_the_macro_allowlist_is_separate_from_the_write_path_allowlist():
    """Being allowed to write a BOM export into a project folder says
    nothing about whether code dropped in that same folder should be
    allowed to run -- these must be two different environment variables."""
    assert server.MACRO_ROOTS_ENV != server.PROJECT_ROOTS_ENV


# ---------------------------------------------------------------------------
# R3.24 -- execute_python is registered only when explicitly enabled
# ---------------------------------------------------------------------------
#
# These import a FRESH `server` module in a subprocess, deliberately not
# through tests/conftest.py's fake-COM import (which, for every other test
# in this suite, defaults SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON to "1" so the
# full 150-tool catalog -- including this test file's own
# test_run_macro_and_execute_python_are_the_only_open_world_tools in
# test_contract.py -- is there to test against). Checking the real default
# needs a process that never applies that convenience.

_CHECK_REGISTERED = (
    "import sys; sys.path.insert(0, '.'); "
    "from unittest.mock import MagicMock; "
    "sys.modules.setdefault('pythoncom', MagicMock()); "
    "sys.modules.setdefault('win32com', MagicMock()); "
    "sys.modules.setdefault('win32com.client', MagicMock()); "
    "import server; "
    "print('execute_python' in server.mcp._tool_manager._tools)"
)


def _is_execute_python_registered(env_value):
    import subprocess
    env = dict(os.environ)
    if env_value is None:
        env.pop("SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON", None)
    else:
        env["SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON"] = env_value
    result = subprocess.run([sys.executable, "-c", _CHECK_REGISTERED], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1] == "True"


def test_execute_python_is_not_registered_by_default():
    """Not 'registered but rejects the call' -- genuinely absent from the
    catalog, so a model never even sees it costs it no context tokens."""
    assert _is_execute_python_registered(None) is False


def test_execute_python_is_registered_when_explicitly_enabled():
    assert _is_execute_python_registered("1") is True


@pytest.mark.parametrize("off_value", ["0", "false", "", "no"])
def test_execute_python_stays_unregistered_for_every_falsy_spelling(off_value):
    assert _is_execute_python_registered(off_value) is False


# ---------------------------------------------------------------------------
# R3.4 -- per-tool timeout budget instead of one flat 120s
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_com_timeout():
    """Several tests below intentionally change the module-level override;
    leaving it changed would make every OTHER test in the suite (and every
    other test file importing the same `server` module in this process)
    silently run under a different budget than it was written against."""
    original = server.COM_TIMEOUT_SECONDS
    yield
    server.COM_TIMEOUT_SECONDS = original


@pytest.mark.parametrize("name,expected", [
    ("get_document_info", 15),       # simple read
    ("measure_body", 15),
    ("extract_assembly_bom", 300),   # assembly-scale read
    ("list_components", 300),
    ("export_document", 900),        # batch export
    ("pack_and_go", 900),
    ("connect_solidworks", 240),     # cold launch
    ("add_mate", 60),                # ordinary feature/setter
    ("set_material", 60),
])
def test_each_category_gets_its_own_timeout_budget(name, expected):
    assert server._timeout_budget_for(name) == expected


def test_an_unregistered_name_falls_back_to_the_feature_budget():
    """Not found in the tool registry at all (renamed, or called from
    outside any real tool invocation) can't be proven read-only, so it gets
    the ordinary feature/setter budget rather than being trusted with the
    tight 15s read budget."""
    assert server._timeout_budget_for("not_a_real_tool") == server._FEATURE_TIMEOUT_SECONDS


def test_no_current_tool_falls_back_to_the_plain_default():
    """_run can be reached with no tool name in context at all (nothing has
    called through ToolManager.call_tool yet) -- that case gets the original
    flat default, not a guess."""
    assert server._timeout_budget_for(None) == server._DEFAULT_COM_TIMEOUT_SECONDS


def test_raising_the_global_override_widens_every_category_not_just_the_default():
    """tests/run_live_test_project.py sets server.COM_TIMEOUT_SECONDS directly
    to give a whole live run more room -- that has to keep working, and has
    to apply to every tool it then calls, not just the ones that would
    otherwise fall back to the plain default."""
    server.COM_TIMEOUT_SECONDS = 600
    assert server._timeout_budget_for("get_document_info") == 600
    assert server._timeout_budget_for("export_document") == 600


def test_connect_solidworks_budget_exceeds_its_own_internal_launch_wait():
    """_connect()'s cold-launch retry loop is itself bounded at 120s; the
    outer budget has to be strictly larger, or asyncio.wait_for can fire
    first with a confusing timeout message while the launch is still
    legitimately in progress."""
    # connect_solidworks itself just calls _connect(); the 120s bound lives
    # in _connect's own source.
    connect_source = inspect.getsource(server._connect)
    assert "120" in connect_source
    assert server.TIMEOUT_BUDGET_OVERRIDES["connect_solidworks"] > 120


def test_every_tool_name_used_in_the_assembly_or_batch_sets_actually_exists():
    """A renamed tool left in one of these sets would silently stop getting
    its intended budget and fall through to the generic one instead."""
    for name in server.ASSEMBLY_SCALE_TOOLS | server.BATCH_EXPORT_TOOLS | set(server.TIMEOUT_BUDGET_OVERRIDES):
        assert name in TOOLS, f"'{name}' is in a timeout-budget set but is not a registered tool"

# -------------------------------------------------------------------------
# A tool must not claim a capability it does not have
# ---------------------------------------------------------------------------


def test_the_piston_assembly_does_not_claim_to_be_mated():
    """It creates no mates, and said so nowhere.

    Counted over the whole function body: add_mate 0, add_advanced_mate 0,
    AddMate 0, fix_component 0, set_component_transform 0,
    interference_check 0, insert_component 5. It positions five components by
    computed offsets and stops -- while the docstring called it "the moving
    assembly", which is the one thing it certainly is not. A caller who
    believes the docstring gets a piston and rod that never seat together,
    with nothing reporting a gap.
    """
    code = code_of("create_automotive_piston_assembly")
    for mate_call in ("add_mate(", "add_advanced_mate(", "AddMate"):
        assert mate_call not in code, (
            f"create_automotive_piston_assembly now calls {mate_call} -- "
            f"update its docstring, which states that it creates no mates"
        )
    description = TOOLS["create_automotive_piston_assembly"].description
    assert "NO MATES" in description, (
        "the docstring must state that no mates are created"
    )
    assert "moving assembly represented" not in description, (
        "the docstring still calls this a moving assembly; it has no joints"
    )


def test_the_piston_assembly_points_at_the_mates_a_real_joint_needs():
    """Saying what is missing is only half useful without saying what to do."""
    description = TOOLS["create_automotive_piston_assembly"].description
    for expected in ("concentric", "width", "get_component_transform",
                     "interference_check"):
        assert expected in description, f"the docstring does not mention {expected}"


def test_add_mate_documents_that_it_cannot_reach_a_hidden_face():
    """SelectByID2 picks from the CAMERA.

    An internal face -- a piston pin-boss bore inside the skirt -- is
    unreachable from any orientation, so the call fails or silently grabs the
    outer face in front of it. That is not discoverable from the signature.
    """
    description = TOOLS["add_mate"].description.lower()
    assert "camera" in description
    assert "list_faces" in description


def test_add_mate_documents_that_concentric_leaves_the_axis_free():
    """A concentric mate removes two degrees of freedom and leaves sliding
    along the shared axis free, so the part stops wherever the solver left
    it. This is the usual reason a piston-rod joint looks aligned and is
    not."""
    description = TOOLS["add_mate"].description
    assert "degrees of freedom" in description
    assert "get_component_transform" in description