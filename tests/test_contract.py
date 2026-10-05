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

import inspect
import json
import os
import sys

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
