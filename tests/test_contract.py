"""
Contract tests for the SolidWorks MCP server.

The README has told people to run ``python -m unittest tests.test_contract``
since v5.x, and the file did not exist. This is it, as pytest.

What a contract test is for: the public surface of an MCP server is its tool
names, their parameters and their descriptions. A client -- Claude included --
binds to those. Renaming a tool, dropping a parameter or shipping a tool with
no description breaks callers silently, and nothing else in this repository
would notice. So this file freezes the surface and fails on an unannounced
change.

It imports ``server``, which starts the COM executor, so it needs Windows and
pywin32. It does NOT need SolidWorks to be running: no tool is called.

Run with:  python -m pytest tests/test_contract.py -q
"""

import ast
import inspect
import json
import os
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client",
                    reason="the server module needs pywin32 (Windows only)")

import server  # noqa: E402
import alfa_drawing as ad  # noqa: E402

TOOLS = server.mcp._tool_manager._tools
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tool_names.json")


def load_snapshot() -> list:
    with open(SNAPSHOT_PATH, encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Import health
# ---------------------------------------------------------------------------


def test_the_server_imports_without_solidworks_running():
    """Importing must never need a live SolidWorks.

    If it does, nothing about this server can be tested in CI, and a packaging
    mistake only shows up on a user's machine.
    """
    assert server.mcp is not None
    assert TOOLS, "no tools were registered"


def test_unknown_tool_arguments_are_rejected():
    """extra="forbid" is load-bearing.

    With Pydantic's default (extra="ignore"), a caller that writes "x_center"
    instead of "cx" gets no error at all: the field falls back to its default
    and the tool quietly does the wrong thing -- a mispositioned arc, not a
    failure.
    """
    from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
    assert ArgModelBase.model_config.get("extra") == "forbid"


def test_the_version_is_consistent_across_the_packaging_files():
    """pyproject, manifest and README must agree.

    They did not: the README advertised 5.8.0 while the manifest and
    pyproject said 5.9.2, so whoever installed the .mcpb got a different
    version from the one documented.
    """
    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as handle:
        manifest_version = json.load(handle)["version"]
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        pyproject = handle.read()
    assert f'version = "{manifest_version}"' in pyproject, (
        f"pyproject.toml does not declare version {manifest_version}"
    )
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()
    assert f"v{manifest_version}" in readme, (
        f"README.md does not mention v{manifest_version}; it is out of date "
        f"with manifest.json"
    )


def test_the_readme_only_tells_people_to_run_tests_that_exist():
    """The README pointed at tests.test_contract for releases, and the file
    was not in the repository. Anything it names must be real."""
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()
    for module in ("test_contract", "test_alfa_drawing"):
        if module in readme:
            assert os.path.exists(
                os.path.join(os.path.dirname(SNAPSHOT_PATH), f"{module}.py")
            ), f"README references tests/{module}.py, which does not exist"


# ---------------------------------------------------------------------------
# The tool surface
# ---------------------------------------------------------------------------


def test_no_tool_disappeared():
    """A removed or renamed tool breaks every client bound to it.

    When a rename is intended, update tests/tool_names.json in the same
    commit -- that is the announcement.
    """
    missing = sorted(set(load_snapshot()) - set(TOOLS))
    assert not missing, (
        f"{len(missing)} tool(s) in the snapshot are gone from the server: "
        f"{', '.join(missing)}. If this is intended, update "
        f"tests/tool_names.json."
    )


def test_new_tools_are_recorded_in_the_snapshot():
    added = sorted(set(TOOLS) - set(load_snapshot()))
    assert not added, (
        f"{len(added)} new tool(s) are not in the snapshot: "
        f"{', '.join(added)}. Add them to tests/tool_names.json."
    )


def test_the_manifest_tool_list_matches_the_server():
    """The .mcpb manifest advertises a tool list to the client.

    If it drifts from the server, the install shows tools that do not exist
    or hides tools that do.
    """
    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    declared = {entry["name"] for entry in manifest.get("tools", [])}
    if not declared:
        pytest.skip("manifest.json declares no tool list")
    assert not declared - set(TOOLS), (
        f"manifest.json advertises tools the server does not have: "
        f"{', '.join(sorted(declared - set(TOOLS)))}"
    )
    assert not set(TOOLS) - declared, (
        f"the server has tools missing from manifest.json: "
        f"{', '.join(sorted(set(TOOLS) - declared))}"
    )


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_has_a_description(name):
    """An undescribed tool is an uncallable tool.

    The model picks tools by their description; a blank one is a tool that
    gets picked at random or never.
    """
    description = (TOOLS[name].description or "").strip()
    assert len(description) >= 20, (
        f"tool '{name}' has a {len(description)}-character description"
    )


# Tools that touch no COM object at all, and so need no COM thread.
# Deliberately an explicit list: a tool added here by mistake loses the
# apartment-threading guarantee silently, so it has to be a decision.
NO_COM_TOOLS = frozenset({
    "set_units",  # sets a module-level default unit, nothing else
})


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_is_async(name):
    """A synchronous tool blocks the event loop for the whole COM timeout."""
    assert inspect.iscoroutinefunction(TOOLS[name].fn), f"tool '{name}' is not async"


@pytest.mark.parametrize("name", sorted(set(TOOLS) - NO_COM_TOOLS))
def test_every_com_tool_hands_its_work_to_the_com_thread(name):
    """COM objects are apartment-threaded.

    A tool that touches SolidWorks from whichever thread asyncio happened to
    pick is how this server deadlocks or corrupts a document, so the work has
    to go through _run, which pins it to the one thread that called
    CoInitialize.
    """
    source = inspect.getsource(TOOLS[name].fn)
    assert "_run(" in source or "await " in source, (
        f"tool '{name}' never awaits anything. If it genuinely touches no COM "
        f"object, add it to NO_COM_TOOLS with a reason; otherwise route its "
        f"work through _run."
    )


@pytest.mark.parametrize("name", sorted(NO_COM_TOOLS & set(TOOLS)))
def test_the_no_com_exemptions_really_touch_no_com(name):
    """Keeps the exemption list honest.

    An exempt tool that later grows a COM call would otherwise keep its pass
    forever, which is worse than never having had the test.
    """
    source = inspect.getsource(TOOLS[name].fn)
    for marker in ("_active_doc", "_connect(", "win32com", "_active_drawing"):
        assert marker not in source, (
            f"tool '{name}' is exempt from the COM-thread rule but uses "
            f"'{marker}'; remove it from NO_COM_TOOLS"
        )


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_no_tool_takes_a_bare_mutable_default(name):
    """A shared mutable default leaks state between calls."""
    for parameter in inspect.signature(TOOLS[name].fn).parameters.values():
        assert not isinstance(parameter.default, (list, dict, set)), (
            f"tool '{name}' parameter '{parameter.name}' has a mutable default"
        )


def test_tool_names_are_snake_case():
    offenders = [n for n in TOOLS if n != n.lower() or " " in n or "-" in n]
    assert not offenders, f"non-snake_case tool names: {', '.join(offenders)}"


# ---------------------------------------------------------------------------
# ToolAnnotations (readOnlyHint / destructiveHint / idempotentHint)
# ---------------------------------------------------------------------------
#
# Before this, a client had no way to tell measure_body (safe to auto-approve)
# from delete_feature (ask first) or run_macro (unpredictable by definition)
# apart from parsing the docstring -- the MCP spec's own guard-rail for this
# (ToolAnnotations) was declared on zero of the 150 tools. These tests freeze
# that classification the same way test_no_tool_disappeared freezes names: a
# new tool with no annotations, or one whose read-only/destructive claim
# contradicts its own name, fails loudly instead of silently shipping
# unannotated.


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_declares_annotations(name):
    assert TOOLS[name].annotations is not None, (
        f"'{name}' has no ToolAnnotations -- a client can't tell whether it's "
        f"safe to auto-approve or needs confirmation"
    )


@pytest.mark.parametrize("name", sorted(n for n in TOOLS if n.startswith(("get_", "list_"))))
def test_get_and_list_tools_are_read_only(name):
    assert TOOLS[name].annotations.readOnlyHint is True, (
        f"'{name}' looks like a read tool by name but is not annotated readOnlyHint=True"
    )


@pytest.mark.parametrize("name", sorted(n for n in TOOLS if n.startswith("delete_")))
def test_delete_tools_are_destructive_and_not_read_only(name):
    annotations = TOOLS[name].annotations
    assert annotations.readOnlyHint is not True
    assert annotations.destructiveHint is True


def test_run_macro_and_execute_python_are_the_only_open_world_tools():
    """Every other tool's effect is confined to the local SolidWorks session
    and the project folder -- a closed world. These two run arbitrary,
    unreviewed code/macro bodies, which is open-world by definition."""
    open_world = {n for n in TOOLS if TOOLS[n].annotations.openWorldHint}
    assert open_world == {"run_macro", "execute_python"}


# ---------------------------------------------------------------------------
# The drawing layer specifically
# ---------------------------------------------------------------------------

DRAWING_READ_TOOLS = ("get_drawing_layout", "get_view_entities",
                      "get_view_dimensions", "verify_drawing")
DRAWING_WRITE_TOOLS = ("dimension_by_entity_ids", "add_drawing_dimension")


@pytest.mark.parametrize("name", DRAWING_READ_TOOLS + DRAWING_WRITE_TOOLS)
def test_the_drawing_tools_are_registered(name):
    assert name in TOOLS, f"drawing tool '{name}' is not registered"


@pytest.mark.parametrize("name", DRAWING_WRITE_TOOLS)
def test_every_dimension_tool_can_assert_an_expected_value(name):
    """The fix for "the dimension landed on the wrong edge".

    Without expected_mm the tool can only report what it did, never whether
    it was right. Any future dimension tool has to offer the same assertion,
    so it is tested here rather than remembered.
    """
    parameters = inspect.signature(TOOLS[name].fn).parameters
    assert "expected_mm" in parameters, (
        f"'{name}' cannot be asked to verify its own result"
    )
    assert "tolerance_mm" in parameters


@pytest.mark.parametrize("name", DRAWING_WRITE_TOOLS)
def test_no_dimension_tool_defaults_its_text_to_the_sheet_origin(name):
    """place_x=0, place_y=0 put the text in the bottom-left corner of the
    sheet, outside the frame, whenever the model omitted a position."""
    parameters = inspect.signature(TOOLS[name].fn).parameters
    for axis in ("place_x", "place_y"):
        if axis in parameters:
            assert parameters[axis].default is None, (
                f"'{name}' still defaults {axis} to "
                f"{parameters[axis].default!r}; None means "
                f"'place it outside the view for me'"
            )


def test_get_view_entities_is_filterable_and_bounded():
    """A pipe rack view has hundreds of edges.

    Returning them all would blow the context window, so the tool must be
    able to narrow and to cap.
    """
    parameters = inspect.signature(TOOLS["get_view_entities"].fn).parameters
    for expected in ("kinds", "min_length_mm", "max_results"):
        assert expected in parameters, f"get_view_entities lacks '{expected}'"


def test_insert_drawing_view_documents_that_the_position_is_the_centre():
    """IView.Position is the view's geometric centre.

    The old docstring just said "the position on the drawing sheet", and the
    natural reading is bottom-left -- which displaces every view by half its
    width.
    """
    description = TOOLS["insert_drawing_view"].description.lower()
    assert "centre" in description or "center" in description
    assert "scale" in description


def test_the_drawing_read_tools_describe_themselves_as_read_only():
    """Until ToolAnnotations are declared, the docstring is the only place a
    caller can learn that a tool is safe to call."""
    read_only_markers = ("read-only", "read only", "describe", "list")
    for name in DRAWING_READ_TOOLS:
        description = TOOLS[name].description.lower()
        assert any(marker in description for marker in read_only_markers), (
            f"'{name}' does not say that it only reads"
        )


# ---------------------------------------------------------------------------
# The geometry module is wired in
# ---------------------------------------------------------------------------


def test_the_server_uses_the_shared_geometry_module():
    """server.py must not grow its own copy of this logic.

    Two implementations of "do these outlines overlap" is how the verifier
    and the placer end up disagreeing about the same sheet.
    """
    assert server.ad is ad
    assert isinstance(server._ENTITY_REGISTRY, ad.EntityRegistry)


def test_the_issue_codes_are_the_ones_the_report_defines():
    """A finding here and a finding in the technical report must be the same
    finding, or neither can be tracked."""
    for code in ("E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E16"):
        assert code in ad.ISSUE_CODES


def test_the_entity_registry_starts_empty():
    assert server._ENTITY_REGISTRY.ids_for_view("nonexistent.slddrw", "V1") == []


# ---------------------------------------------------------------------------
# The assembly placement layer
# ---------------------------------------------------------------------------
# Same contract as the drawing layer, for the side of the model the drawing
# codes cannot see: a part in the wrong place yields a valid drawing of the
# wrong assembly.

PLACEMENT_TOOLS = ("insert_component", "set_component_transform")


def test_the_assembly_verifier_is_registered_and_reads_only():
    assert "verify_assembly_positions" in TOOLS
    description = TOOLS["verify_assembly_positions"].description.lower()
    assert "read-only" in description or "read only" in description


@pytest.mark.parametrize("name", PLACEMENT_TOOLS)
def test_every_placement_tool_can_assert_its_own_result(name):
    """The fix for "the part landed somewhere else".

    Without a tolerance to check against, a placement tool can only report
    what it was asked to do, never whether it happened. Any future placement
    tool has to offer the same assertion, so it is tested rather than
    remembered.
    """
    assert "tolerance" in inspect.signature(TOOLS[name].fn).parameters, (
        f"'{name}' cannot be asked to verify its own result"
    )


@pytest.mark.parametrize("name", PLACEMENT_TOOLS)
def test_every_placement_tool_documents_that_it_measures_the_result(name):
    """A caller has to know the position in the payload is the measured one,
    not the one that was requested -- otherwise the echo is read as proof."""
    description = TOOLS[name].description.lower()
    assert "actual_position" in description, (
        f"'{name}' does not tell the caller that it reads the position back"
    )
    assert "verified" in description


def test_add_mate_reports_what_the_mate_does_not_constrain():
    """Every supported mate type leaves a degree of freedom open: a concentric
    mate holds the axis and lets the part slide anywhere along it. A mate that
    reports success has not necessarily located anything."""
    description = TOOLS["add_mate"].description.lower()
    assert "degrees_of_freedom_left" in description
    for mate_type in ("coincident", "concentric", "parallel",
                      "perpendicular", "tangent"):
        assert mate_type in server.MATE_DOF_LEFT, (
            f"mate type '{mate_type}' is offered by add_mate but its "
            f"unconstrained degrees of freedom are not documented"
        )


def test_add_mate_no_longer_hardcodes_the_alignment():
    """AddMate5's 2nd parameter is swMateAlign_e (ALIGNED=0, ANTI_ALIGNED=1,
    CLOSEST=2). A literal 0 forces ALIGNED on every call regardless of the
    geometry -- the wrong solve for two faces meant to face each other, and a
    second, separate cause of a part landing tilted even when the face
    selection itself was correct."""
    source = _executable_body(TOOLS["add_mate"].fn)
    assert "mate_code, 0," not in source.replace(" ", "").replace("\n", ""), (
        "AddMate5's Align parameter is hardcoded to 0 (ALIGNED) again"
    )
    assert "align_code" in source
    for option in ("aligned", "anti_aligned", "closest"):
        assert option in server.MATE_ALIGN, f"align option '{option}' is missing"
    assert server.MATE_ALIGN["aligned"] == 0
    assert server.MATE_ALIGN["anti_aligned"] == 1
    assert server.MATE_ALIGN["closest"] == 2


def test_add_mate_measures_the_resulting_geometry():
    """The "fica torta" symptom: a mate that solves to a tilted or crooked fit
    without SolidWorks reporting any error. geometry_check gives a measured
    angle to check against instead of judging it from a picture."""
    description = TOOLS["add_mate"].description.lower()
    assert "geometry_check" in description
    assert "angle" in description
    params = inspect.signature(TOOLS["add_mate"].fn).parameters
    assert "align" in params
    assert params["align"].default == "closest"


def test_the_transform_tools_label_their_matrix_convention():
    """Row-major, confirmed live. Assuming the column convention gives a
    plausible but wrong global point that still selects *a* face, so the
    convention travels in the payload instead of being guessed."""
    for name in ("get_component_transform", "set_component_transform"):
        assert "rotation_convention" in TOOLS[name].description, (
            f"'{name}' does not state its rotation matrix convention"
        )


def test_the_assembly_issue_codes_exist():
    for code in ("M01", "M02", "M03", "M04", "M05"):
        assert code in ad.ISSUE_CODES


def _executable_body(function) -> str:
    """The function's code with its docstring removed.

    Needed because these docstrings quote the bug they fixed: a docstring that
    *explains* why bodies[0] is wrong must not read as bodies[0] being used.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def test_the_multibody_bounding_box_unions_every_body():
    """A weldment has several solid bodies. Measuring only bodies[0] gives the
    box of whichever body comes first, and the origin correction computed from
    it displaces the component by the difference between the two boxes -- which
    is every weldment and every structural-profile part."""
    assert "bodies[0]" not in _executable_body(server._bodies_bounding_box), (
        "the bounding box is being read from the first body only, which is "
        "wrong for every multi-body part"
    )
    assert _executable_body(server._preload_and_insert_component).count(
        "_bodies_bounding_box") == 1, (
        "insert_component no longer measures the source document through "
        "_bodies_bounding_box"
    )


def test_the_assembly_verifier_has_a_mechanism_mode():
    """A mechanism's moving parts are under-constrained ON PURPOSE. Without a
    way to declare them, M01 fires on every one of them and the real finding
    drowns in false positives."""
    params = inspect.signature(TOOLS["verify_assembly_positions"].fn).parameters
    assert "moving_components" in params
    assert params["moving_components"].default is None
    assert "M06" in ad.ISSUE_CODES


@pytest.mark.parametrize("name", ("capture_assembly_pose", "restore_assembly_pose"))
def test_the_mechanism_pose_tools_are_registered(name):
    assert name in TOOLS


def test_capture_assembly_pose_reads_only_and_can_persist():
    description = TOOLS["capture_assembly_pose"].description.lower()
    assert "read-only" in description or "read only" in description
    assert TOOLS["capture_assembly_pose"].annotations.readOnlyHint is True
    params = inspect.signature(TOOLS["capture_assembly_pose"].fn).parameters
    assert "filepath" in params, (
        "a pose that cannot be written to disk does not survive the session, "
        "which is the whole point for a mechanism"
    )


def test_restore_assembly_pose_verifies_what_it_restored():
    """A mate can drag a restored component the moment the rebuild solves, and
    SolidWorks reports nothing when it does."""
    description = TOOLS["restore_assembly_pose"].description.lower()
    assert "deviation" in description
    assert "verified" in description
    assert "tolerance" in inspect.signature(TOOLS["restore_assembly_pose"].fn).parameters


def test_the_pose_round_trip_never_goes_through_euler_angles():
    """Decomposing a rotation matrix to angles and recomposing it is not exact
    (aliased triples map to the same matrix), and a mechanism pose has to come
    back exactly. _apply_pose must take the matrix itself."""
    source = _executable_body(server._apply_pose)
    for forbidden in ("radians", "degrees", "math.cos", "math.sin"):
        assert forbidden not in source, (
            f"_apply_pose uses {forbidden}: the pose is going through an angle "
            f"conversion instead of the matrix SolidWorks reported"
        )
