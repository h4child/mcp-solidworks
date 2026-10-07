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


# ---------------------------------------------------------------------------
# v5.15.0: the part layer measures its own geometry
# ---------------------------------------------------------------------------
# An AST audit of server.py found 95 echoed coordinate/dimension keys across 86
# tools against 9 that read anything back. The assembly layer had been fixed in
# v5.14.0; the part layer still answered every question with its own arguments.

SKETCH_PRIMITIVES = (
    "draw_line", "draw_centerline", "draw_circle", "draw_rectangle",
    "draw_arc", "draw_polygon", "draw_spline", "draw_line_3d",
)

MEASURED_FEATURE_TOOLS = (
    "fillet_edges", "chamfer_edges", "shell_body", "extrude_sketch", "cut_extrude",
)


@pytest.mark.parametrize("name", SKETCH_PRIMITIVES)
def test_every_sketch_primitive_takes_a_tolerance(name):
    """Measuring without a tolerance is not a check, it is a float comparison."""
    assert "tolerance" in inspect.signature(TOOLS[name].fn).parameters, (
        f"{name} cannot state how close is close enough"
    )


@pytest.mark.parametrize("name", SKETCH_PRIMITIVES)
def test_every_sketch_primitive_reads_its_own_geometry_back(name):
    """The whole point: the segment SolidWorks created has to be read, not the
    arguments repeated. _sketch_segment_geometry is the only way to do that."""
    source = _executable_body(TOOLS[name].fn)
    assert "_sketch_segment_geometry" in source, (
        f"{name} never reads back the segment it created, so it cannot tell a "
        f"snapped profile from a correct one"
    )
    assert "_sketch_verdict" in source, (
        f"{name} does not report a verdict (deviation/verified/snapped)"
    )


@pytest.mark.parametrize("name", SKETCH_PRIMITIVES)
def test_every_sketch_primitive_documents_the_snap(name):
    """A caller who does not know the snap exists reads `snapped: true` as
    noise. The docstring is where that is explained."""
    description = (TOOLS[name].description or "").lower()
    assert "measured" in description, f"{name} does not say its result is measured"
    assert "snap" in description or "deviation" in description, (
        f"{name} does not mention the snap or the deviation it reports"
    )


def test_draw_rectangle_no_longer_computes_its_size_from_its_arguments():
    """It returned width = abs(x2 - x1): arithmetic on its own input, true of
    the request and silent about the model."""
    source = _executable_body(TOOLS["draw_rectangle"].fn)
    assert "actual_width" in source and "actual_height" in source, (
        "draw_rectangle still reports only the requested size"
    )
    assert "segment_count" in source, (
        "a rectangle that came back with fewer than 4 segments is not closed, "
        "and extruding it fails or builds the wrong solid"
    )


def test_the_sketch_refusal_no_longer_blames_a_missing_sketch():
    """"Is a sketch active?" sent callers hunting for a sketch that was open
    the whole time. The sketch is checked first now, so the message can say
    what the refusal actually is."""
    source = _executable_body(server._sketch_create)
    assert "ActiveSketch is None" in source, (
        "_sketch_create does not check for the sketch before blaming it"
    )
    assert "ViewZoomTo2" in source, (
        "the screen-space snap is fixed by zooming in; _sketch_create should "
        "retry that way once instead of only reporting the refusal"
    )
    assert "snap" in server._SKETCH_SNAP_REFUSAL.lower()


def test_list_sketch_entities_is_the_part_level_read_back():
    """Without it nothing can ask where sketch geometry ended up: at part level
    the only other read-backs are measure_body and list_faces."""
    assert "list_sketch_entities" in TOOLS
    assert TOOLS["list_sketch_entities"].annotations.readOnlyHint is True
    params = inspect.signature(TOOLS["list_sketch_entities"].fn).parameters
    assert "name" in params, (
        "a sketch can only be verified before extruding if a CLOSED one can be "
        "read by name"
    )
    description = TOOLS["list_sketch_entities"].description.lower()
    assert "construction" in description, (
        "revolve_sketch needs a real centerline; whether a segment is "
        "construction geometry is part of what this has to report"
    )


def test_insert_component_reports_whether_the_part_is_anchored():
    """SolidWorks fixes the first component of an assembly. A fixed component
    never moves, so every mate against it is solved by moving the OTHER part --
    which reads as "the main part cannot be positioned"."""
    source = _executable_body(TOOLS["insert_component"].fn)
    assert "IsFixed" in source, (
        "insert_component never reads whether the component came out fixed"
    )
    assert "float_component" in source, (
        "the warning should name the way out, not just the symptom"
    )


@pytest.mark.parametrize("name", MEASURED_FEATURE_TOOLS)
def test_every_measured_feature_tool_reads_its_dimension_back(name):
    """SolidWorks CLAMPS a dimension it cannot satisfy and reports no error: a
    fillet radius larger than the geometry allows comes back smaller."""
    source = _executable_body(TOOLS[name].fn)
    assert "_measured_feature_value" in source, (
        f"{name} does not read its dimension back out of the feature"
    )
    assert "tolerance" in inspect.signature(TOOLS[name].fn).parameters


def test_the_feature_dimension_lookup_does_not_hardcode_an_english_name():
    """Feature names are localized -- a Portuguese install names an extrude
    'Ressalto-extrusao1'. A hardcoded 'D1@Boss-Extrude1' reads nothing there and
    would report every feature as unverifiable."""
    source = _executable_body(server._feature_dimension_value)
    assert "Boss-Extrude" not in source and "Fillet1" not in source, (
        "the dimension key is built from a hardcoded English feature name"
    )
    assert '"Name"' in source or "'Name'" in source, (
        "the dimension key should be built from the feature's own name"
    )


def test_move_copy_body_measures_the_displacement_it_caused():
    source = _executable_body(TOOLS["move_copy_body"].fn)
    assert "_bodies_bounding_box" in source, (
        "move_copy_body still only repeats the translation it was given"
    )
    assert "verification_skipped" in source, (
        "with a rotation or a copy the bounding-box centre shift is NOT the "
        "translation; claiming a check there would be a false alarm"
    )


def test_the_piston_joint_centers_no_longer_claim_to_be_a_contract():
    """They are arithmetic on the input dimensions, computed before any geometry
    existed. Published as a 'contract', a reader downstream mates to them as if
    they had been measured."""
    # ast.unparse renders string literals single-quoted, whatever the source
    # used -- asserting on '"contract"' would pass even with the key still there.
    source = _executable_body(TOOLS["create_automotive_piston_with_connecting_rod"].fn)
    assert "'contract'" not in source, (
        "joint_centers still presents computed numbers as a guarantee"
    )
    assert "'measured': False" in source, (
        "joint_centers should say outright that it was not measured"
    )


# ---------------------------------------------------------------------------
# v5.16.0: the motion layer
# ---------------------------------------------------------------------------
# v5.14.0 fixed the forced mate alignment in add_mate and left the identical
# literal 0 in add_advanced_mate -- the tool a mechanism is actually built with,
# since it owns distance, angle, gear, width, symmetric and lock. And none of
# the four mechanism mates reported where the solve left the components, which
# for a mechanism is the whole question: these mates leave movement free on
# purpose, so creating one DRAGS whatever is still free.

MECHANISM_MATE_TOOLS = (
    "add_advanced_mate", "add_cam_follower_mate",
    "add_screw_mate", "add_rack_pinion_mate",
)


def test_add_advanced_mate_no_longer_forces_the_alignment():
    """AddMate5's second argument is swMateAlign_e. A literal 0 forces ALIGNED,
    which for two faces meant to face each other is the wrong solution -- and
    SolidWorks reports no error, because it is a valid one."""
    source = _executable_body(TOOLS["add_advanced_mate"].fn)
    assert "MATE_ALIGN" in source, (
        "add_advanced_mate does not resolve its alignment through MATE_ALIGN"
    )
    assert "align_code" in source, "add_advanced_mate still passes a fixed alignment"
    params = inspect.signature(TOOLS["add_advanced_mate"].fn).parameters
    assert "align" in params, "add_advanced_mate exposes no align argument"
    assert params["align"].default == "closest", (
        "the default should be 'closest', which keeps the components near their "
        "current relative pose instead of always forcing one side"
    )


def test_no_mate_tool_passes_a_literal_alignment_to_addmate5():
    """The regression in one sentence: find any AddMate5 call whose second
    argument is a bare number."""
    for name in ("add_mate", "add_advanced_mate"):
        source = _executable_body(TOOLS[name].fn)
        for marker in ("AddMate5(code, 0", "AddMate5(mate_code, 0"):
            assert marker not in source.replace("\n", " "), (
                f"{name} hardcodes the mate alignment again"
            )


@pytest.mark.parametrize("name", MECHANISM_MATE_TOOLS)
def test_every_mechanism_mate_reports_where_it_left_the_components(name):
    """A mate is not asked for a position; it is asked for a relationship, and
    the position is the consequence. Unreported, that is where "I asked for
    movement and it came out crooked" comes from."""
    source = _executable_body(TOOLS[name].fn)
    assert "_capture_picked_components" in source, (
        f"{name} does not record where the components were before the solve"
    )
    assert "_report_component_movement" in source, (
        f"{name} does not report where the solve left them"
    )
    assert "_mechanism_pose_reminder" in source, (
        f"{name} does not tell the caller that the pose it leaves is not one the "
        f"mate holds"
    )


@pytest.mark.parametrize("name", MECHANISM_MATE_TOOLS)
def test_the_mechanism_mates_capture_before_the_mate_consumes_the_selection(name):
    """Creating the mate clears the selection, so the components owning the
    picked entities have to be taken while it is still live."""
    source = _executable_body(TOOLS[name].fn)
    capture = source.index("_capture_picked_components")
    for creator in ("AddMate5", "CreateMateData"):
        if creator in source:
            assert capture < source.index(creator), (
                f"{name} reads the picked components after {creator}, by which "
                f"point the selection is gone"
            )


@pytest.mark.parametrize("name", ("add_cam_follower_mate", "add_screw_mate",
                                  "add_rack_pinion_mate"))
def test_the_ray_mates_do_not_shadow_their_feature_name_set(name):
    """These tools already bind `before` to the set of pre-existing mate-feature
    names, and detect the new feature by difference against it. Binding the pose
    snapshot to the same name breaks that check silently."""
    source = _executable_body(TOOLS[name].fn)
    assert "pose_before" in source, (
        f"{name} should keep the pose snapshot under its own name"
    )
    assert ", before = _capture_picked_components" not in source, (
        f"{name} shadows its feature-name set with the pose snapshot"
    )


def test_create_motion_study_measures_whether_the_assembly_moved():
    """Activating a study switches the assembly into that study's state, and
    nothing in SolidWorks records the pose it had before."""
    source = _executable_body(TOOLS["create_motion_study"].fn)
    assert "_component_positions" in source, (
        "create_motion_study does not measure component positions at all"
    )
    assert "_moved_components" in source, (
        "create_motion_study does not report which components moved"
    )
    description = TOOLS["create_motion_study"].description
    assert "capture_assembly_pose" in description, (
        "the docstring should name the tool that prevents losing the pose"
    )
    assert "tolerance" in inspect.signature(TOOLS["create_motion_study"].fn).parameters


def test_the_mechanism_reminder_points_at_the_pose_tools():
    """No mate can hold a free degree of freedom -- the free DOF IS the
    movement. capture/restore_assembly_pose is the only answer, so the reminder
    has to say so rather than implying another mate would fix it."""
    note = server._mechanism_pose_reminder("a test mate")
    assert "capture_assembly_pose" in note and "restore_assembly_pose" in note
    assert "not another mate" in note or "not another" in note


# ---------------------------------------------------------------------------
# The gear layer
# ---------------------------------------------------------------------------
# A gear is the one shape the generic sketch tools could not build, and it
# failed silently: a smooth disc that rebuilds clean, measures plausibly and
# gets handed over as a gear. These freeze the three things that keep
# create_spur_gear from regressing back into that -- the analytic profile, the
# inference engine being off while it is drawn, and the volume check that
# catches it if either ever stops working.

import gear_geometry as gg  # noqa: E402


def test_the_server_uses_the_shared_gear_geometry_module():
    """Same contract as alfa_drawing: one implementation of the involute.

    A second copy inside server.py could only be tested with SolidWorks open,
    which is how a gear profile drifts from the table printed next to it.
    """
    assert server.gg is gg


def test_the_gear_tool_is_registered_and_not_read_only():
    assert "create_spur_gear" in TOOLS
    annotations = TOOLS["create_spur_gear"].annotations
    assert annotations.readOnlyHint is not True
    assert annotations.destructiveHint is not True   # it adds, it does not remove
    assert annotations.openWorldHint is False


def test_the_gear_tool_warns_the_caller_off_drawing_teeth_by_hand():
    """The docstring is where a model decides whether to use this tool or to
    improvise with draw_line + cut_extrude + circular_pattern. If it does not
    say what goes wrong when you improvise, it gets improvised."""
    description = TOOLS["create_spur_gear"].description.lower()
    assert "smooth" in description
    assert "snap" in description or "inference" in description
    assert "circular_pattern" in description


def test_the_gear_tool_declares_what_it_does_not_cut():
    """Helical, internal, bevel, worm and profile shift are all out of scope.

    A tool that stays quiet about its limits gets used past them.
    """
    description = TOOLS["create_spur_gear"].description.lower()
    for absent in ("helical", "internal", "bevel", "worm", "profile shift"):
        assert absent in description, f"the docstring does not mention {absent}"


def test_the_gear_tool_draws_its_profile_through_the_shared_primitive():
    """The snap-proof drawing path is one implementation, not a copy per tool.

    It started inside create_spur_gear; a gear is just the first caller. A
    second copy is how one of them keeps the inference engine on, or forgets to
    switch it back off, while the tests still pass on the other.
    """
    source = inspect.getsource(TOOLS["create_spur_gear"].fn)
    assert "_draw_profile_points(" in source
    # The docstring names SetAddToDB to explain the mechanism; what must not
    # be here is the CALL.
    assert "SetAddToDB(" not in source, (
        "create_spur_gear should go through _draw_profile_points, not drive "
        "SetAddToDB itself"
    )


def test_the_shared_primitive_draws_with_the_inference_engine_off():
    """SetAddToDB is the load-bearing line of the whole gear/profile layer.

    With it, a tooth-scale point lands where it was asked to; without it, the
    point goes through the screen-space snap and the geometry collapses. It
    must also be switched back off in a finally, or every later draw_* call in
    the session silently stops getting inference and relations -- a worse bug
    than the one it fixes.
    """
    source = inspect.getsource(server._draw_profile_points)
    assert "SetAddToDB(True)" in source
    assert "SetAddToDB(False)" in source
    finally_at = source.index("finally:")
    assert finally_at < source.index("SetAddToDB(False)"), (
        "SetAddToDB must be restored from a finally, not on the happy path only"
    )


def test_the_gear_tool_refuses_a_profile_that_did_not_land():
    """Measuring the vertices is pointless if the result is then extruded anyway.

    A displaced vertex is the original smooth-gear failure, and the solid built
    on it looks plausible -- so this one raises rather than warns.
    """
    source = inspect.getsource(TOOLS["create_spur_gear"].fn)
    assert "did not land where it was computed" in source
    assert "raise RuntimeError" in source


def test_the_gear_tool_does_not_pattern_a_tooth():
    """The failure mode this tool replaces, frozen as a test.

    One seed tooth gap cut and patterned around the blank is exactly what
    produced the smooth disc: the seed profile is the fragile part, and the
    pattern multiplies it. The whole outline is drawn at once instead.
    """
    source = inspect.getsource(TOOLS["create_spur_gear"].fn)
    # The docstring names circular_pattern to say why it is not used, so this
    # looks for the call, not the word.
    assert "circular_pattern(" not in source
    assert "linear_pattern(" not in source
    assert "cut_extrude(" in source  # only for the optional shaft bore


def test_the_gear_tool_measures_whether_the_teeth_are_really_there():
    """verified alone is not enough: a wrong-sized gear and a smooth disc are
    different failures, and only one of them is worth refusing to call a gear.
    """
    source = inspect.getsource(TOOLS["create_spur_gear"].fn)
    assert "teeth_present" in source
    assert "measure_body" in source
    parameters = inspect.signature(TOOLS["create_spur_gear"].fn).parameters
    assert "volume_tolerance" in parameters
    description = TOOLS["create_spur_gear"].description.lower()
    assert "teeth_present" in description
    assert "measured, not assumed" in description


def test_the_gear_tool_gets_room_to_draw_a_whole_profile():
    """20-30 segments per tooth is over a thousand COM calls on a big gear.

    On the ordinary 60s feature budget that is cut off halfway through, which
    leaves an open profile and a confusing timeout instead of a gear.
    """
    assert server.TIMEOUT_BUDGET_OVERRIDES.get("create_spur_gear", 0) >= 300


def test_every_knowledge_file_the_server_advertises_exists():
    """A resource that 404s is worse than one that was never offered: the
    model asks for the file, gets a "nao encontrado" string back, and carries
    on without the knowledge it just decided it needed.
    """
    missing = [
        filename
        for _, filename, _, _ in server._KNOWLEDGE_RESOURCES
        if not os.path.exists(os.path.join(ROOT, ".claude", "knowledge", filename))
    ]
    assert not missing, f"advertised but absent: {', '.join(missing)}"


def test_the_gear_knowledge_is_advertised_and_routed():
    """Both clients have to be able to find it: Claude Code auto-loads
    .claude/CLAUDE.md's table, an MCP-only client reads the resource list.
    """
    assert any(filename == "engrenagens.md"
               for _, filename, _, _ in server._KNOWLEDGE_RESOURCES)
    with open(os.path.join(ROOT, ".claude", "CLAUDE.md"), encoding="utf-8") as handle:
        routing = handle.read()
    assert "knowledge/engrenagens.md" in routing


# ---------------------------------------------------------------------------
# draw_profile: the primitive the whole class of computed profiles needs
# ---------------------------------------------------------------------------


def test_draw_profile_is_registered_and_annotated():
    assert "draw_profile" in TOOLS
    annotations = TOOLS["draw_profile"].annotations
    assert annotations.readOnlyHint is not True
    assert annotations.openWorldHint is False


def test_draw_profile_explains_the_failure_it_prevents():
    """A model picks between draw_line-in-a-loop, draw_spline and this one.

    If the docstring does not say what the snap radius is measured in, the
    choice gets made on convenience and the geometry collapses silently.
    """
    description = TOOLS["draw_profile"].description.lower()
    assert "pixel" in description
    assert "draw_spline" in description
    assert "silently" in description or "silent" in description


def test_draw_profile_measures_every_vertex():
    source = inspect.getsource(TOOLS["draw_profile"].fn)
    assert "_draw_profile_points(" in source
    parameters = inspect.signature(TOOLS["draw_profile"].fn).parameters
    assert "tolerance" in parameters
    assert "verify_points" in parameters
    description = TOOLS["draw_profile"].description
    assert "MEASURED, NOT ECHOED" in description


def test_draw_profile_gets_room_for_a_large_profile():
    assert server.TIMEOUT_BUDGET_OVERRIDES.get("draw_profile", 0) >= 300


def test_the_spatial_index_finds_the_nearest_stored_vertex():
    """The verification is only as good as this lookup.

    Called directly -- it touches no COM -- because an index that silently
    misses turns "this vertex moved 0.3 mm" into "this vertex is fine".
    """
    stored = [(0.0, 0.0), (0.010, 0.0), (0.0, 0.010)]
    nearest = server._nearest_stored_point(stored, 1e-6)
    point, distance = nearest(0.010, 0.0)
    assert point == (0.010, 0.0)
    assert distance == pytest.approx(0.0)
    # Just inside and just outside the tolerance cell, from the same origin.
    _, close = nearest(0.0, 5e-7)
    assert close == pytest.approx(5e-7)
    _, far = nearest(0.005, 0.005)
    assert far == float("inf"), "a vertex nowhere near anything must not match"


def test_the_spatial_index_is_empty_safe():
    nearest = server._nearest_stored_point([], 1e-6)
    assert nearest(0.0, 0.0)[1] == float("inf")


# ---------------------------------------------------------------------------
# The silent-failure audit that came with it
# ---------------------------------------------------------------------------


def test_the_knurl_draws_its_cells_with_inference_off():
    """A knurl cell is 0.2 mm across with vertices 0.1 mm apart -- three times
    tighter than the gear-tooth spacing that was confirmed to collapse. It was
    drawn straight through SketchManager, and the only check was that the Wrap
    feature came back non-None, so a flattened cell engraved nothing and
    reported success."""
    source = inspect.getsource(TOOLS["create_knurl"].fn)
    assert "_draw_profile_points(" in source
    assert "SketchManager.CreateLine(" not in source
    assert "did not land where it was computed" in source


def test_the_knurl_admits_the_depth_is_not_measured():
    """Honesty about what the new check does NOT cover: the profile is
    verified, the engraved depth is not readable from the Wrap feature."""
    source = inspect.getsource(TOOLS["create_knurl"].fn)
    assert "not measured" in source
    description = TOOLS["create_knurl"].description
    assert "MEASURED" in description


def test_draw_spline_checks_its_interior_points():
    """The ends are the two points the snap is least likely to move, so
    checking only them called a deformed curve verified."""
    source = inspect.getsource(TOOLS["draw_spline"].fn)
    assert "_spline_interior_check(" in source
    description = TOOLS["draw_spline"].description
    assert "INTERIOR" in description
    assert "draw_profile" in description


def test_the_spline_interior_check_says_so_when_it_cannot_read_them():
    """Unreadable must not be reported as correct -- that is the whole bug
    pattern this release is about."""
    source = inspect.getsource(server._spline_interior_check)
    assert "UNVERIFIED" in source
    assert 'result["verified"] = None' in source


def test_the_piston_reads_back_its_ring_groove_circles():
    """The groove is the gap between two circles 3 mm apart in radius, both
    drawn through the inference engine. draw_circle measures its own radius;
    nothing was reading it, so a collapsed groove removed no material and the
    piston still looked right."""
    source = inspect.getsource(TOOLS["create_automotive_piston"].fn)
    assert 'circle.get("verified") is False' in source
    assert "measured_radial_width" in source


def test_the_piston_assembly_verifies_where_every_component_landed():
    """The five offsets are the only thing holding this assembly together --
    it has no mates by design -- so an unread placement is the whole assembly
    unverified."""
    source = inspect.getsource(TOOLS["create_automotive_piston_assembly"].fn)
    assert "component_positions" in source
    assert 'placed.get("verified") is not True' in source
    assert "raise RuntimeError" in source
