"""
Contract/argument-validation tests for the 47 tools added in the
category-2/3/9/10/11/13/14/18/20/21/23/24 catalog cross-reference round
(see NOVAS_ACOES_IMPLEMENTADAS.md for the full table).

These run WITHOUT SolidWorks: every check here is either static (reading a
tool's source) or pure-Python argument validation that happens before the
first COM call. None of these tools have been tested against a real
SolidWorks session -- see NOVAS_ACOES_IMPLEMENTADAS.md for which ones carry
a "NEEDS LIVE VERIFICATION" disclaimer in their own docstring and why.

Run with:  python -m pytest tests/test_new_tools_round2.py -q
"""

import inspect
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytest.importorskip("win32com.client",
                    reason="the server module needs pywin32 (Windows only)")

import server  # noqa: E402

TOOLS = server.mcp._tool_manager._tools

NEW_TOOL_NAMES = [
    "draw_rectangle_centered", "draw_arc_3point", "draw_ellipse",
    "add_sketch_point", "add_sketch_text", "offset_sketch_entities",
    "convert_sketch_entities", "get_sketch_contours",
    "remove_sketch_relation", "query_sketch_relations", "set_dimension_driven",
    "create_reference_point", "extrude_up_to_entity", "cut_up_to_entity",
    "chamfer_edges_two_distances",
    "list_bodies", "get_body_bounding_box", "delete_body",
    "replace_component", "count_component_instances",
    "detect_components_without_mates", "add_mechanical_mate",
    "add_exploded_step", "collapse_exploded_view",
    "set_component_suppression_by_configuration",
    "compare_bom_configurations", "detect_duplicate_bom_items",
    "check_minimum_clearance", "check_axis_alignment",
    "set_sheet_size", "add_dimension_tolerance", "add_datum_feature_symbol",
    "get_applied_material", "compare_mass_properties",
    "bulk_set_custom_properties",
    "export_bom_to_csv", "validate_bom_completeness", "create_fastener_list",
    "verify_bearing_fit", "verify_bolt_hole_fit",
    "import_model", "export_selected_bodies",
    "detect_unused_sketches", "generate_model_snapshot",
    "compare_model_snapshots",
    "audit_assembly", "generate_assembly_checklist",
]


def test_every_new_tool_is_registered():
    missing = [name for name in NEW_TOOL_NAMES if name not in TOOLS]
    assert not missing, f"not registered as MCP tools: {missing}"


def test_new_tool_count_matches_this_round():
    """Pins the round's size so a tool silently dropped (or double-counted)
    shows up here instead of only as a smaller diff in NOVAS_ACOES_IMPLEMENTADAS.md."""
    assert len(NEW_TOOL_NAMES) == 47


# ---------------------------------------------------------------------------
# Argument validation happens before any COM call
# ---------------------------------------------------------------------------
# Same rule and the same source-scanning approach as
# test_bugfixes.VALIDATE_FIRST: a bad argument must not need a live
# SolidWorks connection to be rejected.

VALIDATE_FIRST = [
    "draw_rectangle_centered", "draw_arc_3point", "draw_ellipse",
    "chamfer_edges_two_distances", "compare_mass_properties",
    "bulk_set_custom_properties", "export_selected_bodies",
    "set_sheet_size",
]


@pytest.mark.parametrize("name", VALIDATE_FIRST)
def test_argument_validation_comes_before_the_first_com_call(name):
    source = inspect.getsource(TOOLS[name].fn)
    body = source[source.index("def _impl():"):]
    first_raise = min(
        (i for i in (body.find("raise ValueError"), body.find("raise RuntimeError"))
         if i != -1), default=-1)
    first_com = min((i for i in (body.find("_active_doc()"),
                                 body.find("_active_drawing()"),
                                 body.find("_active_assembly()"))
                     if i != -1), default=-1)
    assert first_raise != -1, f"{name} validates nothing"
    assert first_com == -1 or first_raise < first_com, (
        f"{name} connects to SolidWorks before validating its arguments"
    )


# ---------------------------------------------------------------------------
# Every tool uses _run and is properly annotated (redundant with
# test_contract.py's generic parametrized checks, kept here as a
# self-contained smoke test scoped to just this round's tools).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
def test_new_tool_is_async_and_uses_run(name):
    fn = TOOLS[name].fn
    assert inspect.iscoroutinefunction(fn), f"{name} is not async"
    source = inspect.getsource(fn)
    assert "_run(" in source, f"{name} never routes its work through _run"


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
def test_new_tool_has_a_real_description(name):
    description = (TOOLS[name].description or "").strip()
    assert len(description) >= 40, (
        f"tool '{name}' has only a {len(description)}-character description"
    )


# ---------------------------------------------------------------------------
# Tools whose COM call is a best-effort guess say so in their own docstring
# ---------------------------------------------------------------------------
# These are exactly the tools NOVAS_ACOES_IMPLEMENTADAS.md lists under
# "precisa de verificacao ao vivo prioritaria". If one of these is fixed and
# confirmed live, update BOTH the docstring and this list in the same change
# -- losing the disclaimer silently is worse than never having added it.

NEEDS_LIVE_VERIFICATION = [
    "create_reference_point", "chamfer_edges_two_distances",
    "offset_sketch_entities",
    "replace_component", "add_mechanical_mate",
    "set_sheet_size", "add_dimension_tolerance", "add_datum_feature_symbol",
    "export_selected_bodies",
]


@pytest.mark.parametrize("name", NEEDS_LIVE_VERIFICATION)
def test_best_effort_com_calls_are_flagged_in_their_own_docstring(name):
    doc = TOOLS[name].fn.__doc__ or ""
    assert "NEEDS LIVE VERIFICATION" in doc or "best-effort" in doc.lower() or "flagged" in doc.lower(), (
        f"'{name}' is listed as needing live verification but its docstring "
        f"does not say so -- a caller reading only the tool description "
        f"would not know to distrust it"
    )
