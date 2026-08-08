"""Run every declared MCP tool against an isolated SolidWorks test project.

Use --dry-run to verify coverage without SolidWorks. Use --live only on a test
installation: several MCP tools intentionally create, save, alter or delete CAD
objects. The resulting JSON report is written under tests/output/.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
OUTPUT_DIR = ROOT / "tests" / "output"


def manifest_tool_names() -> list[str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [item["name"] for item in manifest["tools"]]


def sample_value(parameter: str, project: Path) -> Any:
    """Return representative values for every required argument."""
    values: dict[str, Any] = {
        "filepath": str(project / "base_part.SLDPRT"),
        "source_filepath": str(project / "base_part.SLDPRT"),
        "name": "MCP_Test",
        "text": "MCP integration test",
        "code": "print('solidworks-mcp integration test')",
        "unit": "mm",
        "mate_type": "coincident",
        "point1": {"x": 0, "y": 0, "z": 0, "unit": "mm"},
        "point2": {"x": 10, "y": 0, "z": 0, "unit": "mm"},
        "feature_name": "Boss-Extrude1",
        "profile_sketch": "Sketch1",
        "path_sketch": "Sketch2",
        "sketch_names": ["Sketch1", "Sketch2"],
        "standard": "iso",
        "profile_type": "square tube",
        "size": "20 x 20 x 2",
        "sketch_name": "3DSketch1",
        "body_to_trim": "SolidBody",
        "trim_boundary": "SolidBody",
        "body_name": "SolidBody",
        "component_name": "base_part-1",
        "relation": "coincident",
        "material": "AISI 1020",
        "view_name": "isometric",
    }
    if parameter in values:
        return values[parameter]
    if parameter in {"x1", "y1", "z1", "face_x", "face_y", "face_z", "edge_x", "edge_y", "edge_z"}:
        return 0
    if parameter in {"x2", "y2", "z2", "hole_x", "hole_y"}:
        return 10
    if parameter in {"center_x", "center_y", "place_x", "place_y", "dim_x", "dim_y"}:
        return 50
    if parameter == "radius":
        return 10
    if parameter == "value":
        return 10
    raise KeyError(f"No sample value defined for required argument '{parameter}'.")


def tool_arguments(tool_name: str, function: Any, project: Path) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for parameter in inspect.signature(function).parameters.values():
        if parameter.default is inspect.Parameter.empty:
            arguments[parameter.name] = sample_value(parameter.name, project)
    if tool_name == "export_document":
        arguments["filepath"] = str(project / "base_part.step")
    return arguments


def write_report(project: Path, results: list[dict[str, Any]], mode: str) -> Path:
    project.mkdir(parents=True, exist_ok=True)
    report = project / f"live-test-{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {
        "mode": mode,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "passed": sum(item["status"] == "passed" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "not_run": sum(item["status"] == "not_run" for item in results),
        "results": results,
    }
    report.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


DRAWING_TOOLS = {
    "insert_drawing_view", "insert_section_view", "insert_detail_view", "insert_broken_view",
    "insert_auxiliary_view", "add_drawing_dimension", "add_drawing_annotation", "add_centerline",
    "add_weld_symbol", "add_surface_finish", "add_gdt_symbol", "add_balloon", "insert_bom_table",
    "insert_cut_list_table",
}
ASSEMBLY_TOOLS = {
    "insert_component", "list_components", "fix_component", "float_component", "delete_component",
    "suppress_component", "unsuppress_component", "add_mate", "list_mates", "create_exploded_view",
    "add_advanced_mate", "interference_check", "create_assembly_pattern",
}
DOCUMENT_CREATION_TOOLS = {"create_new_part", "create_new_assembly", "create_new_drawing", "connect_solidworks"}


async def seed_base_part(server: Any, project: Path) -> Path:
    """Create a simple 100 x 50 x 10 mm bracket used by integration cases."""
    project.mkdir(parents=True, exist_ok=True)
    part_path = project / "base_part.SLDPRT"
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-50, -25, 50, 25, "mm")
    await server.close_sketch()
    await server.extrude_sketch(10, False, "mm")
    await server.save_document(str(part_path))
    return part_path


async def prepare_tool_context(server: Any, tool_name: str, part_path: Path) -> None:
    """Place the active document in the minimum context expected by a tool."""
    if tool_name in DOCUMENT_CREATION_TOOLS:
        return
    if tool_name in DRAWING_TOOLS:
        await server.create_new_drawing()
        if tool_name != "insert_drawing_view":
            await server.insert_drawing_view(str(part_path))
        return
    if tool_name in ASSEMBLY_TOOLS:
        await server.create_new_assembly()
        if tool_name != "insert_component":
            await server.insert_component(str(part_path))
        return
    await server.open_document(str(part_path))


async def run_live(project: Path, names: list[str], connection_timeout: int) -> list[dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    import server

    results: list[dict[str, Any]] = []
    try:
        server.COM_TIMEOUT_SECONDS = connection_timeout
        try:
            await server.connect_solidworks()
        except Exception as exc:
            results.append({"tool": "connect_solidworks", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            results.extend(
                {"tool": name, "status": "not_run", "reason": "SolidWorks connection unavailable"}
                for name in names
                if name != "connect_solidworks"
            )
            return results

        try:
            part_path = await seed_base_part(server, project)
        except Exception as exc:
            results.append({"tool": "test_fixture", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            results.extend(
                {"tool": name, "status": "not_run", "reason": "Could not create isolated base part"}
                for name in names
            )
            return results

        for name in names:
            if name == "connect_solidworks":
                results.append({"tool": name, "status": "passed", "result": {"connected": True}})
                continue
            function = getattr(server, name, None)
            if function is None or not inspect.iscoroutinefunction(function):
                results.append({"tool": name, "status": "failed", "error": "Tool not callable in server.py"})
                continue
            try:
                await prepare_tool_context(server, name, part_path)
                result = await function(**tool_arguments(name, function, project))
                results.append({"tool": name, "status": "passed", "result": result})
            except Exception as exc:
                results.append({"tool": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        server._shutdown()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Connect to SolidWorks and invoke every tool.")
    parser.add_argument("--dry-run", action="store_true", help="Validate matrix coverage without invoking SolidWorks.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Directory for isolated test artifacts.")
    parser.add_argument("--connection-timeout", type=int, default=120, help="Seconds to wait for the first SolidWorks connection.")
    args = parser.parse_args()
    if args.live == args.dry_run:
        parser.error("Choose exactly one of --live or --dry-run.")

    project = args.output_dir.resolve()
    if project == ROOT:
        parser.error("The output directory cannot be the repository root.")
    if args.connection_timeout <= 0:
        parser.error("--connection-timeout must be positive.")
    names = manifest_tool_names()
    if len(names) != 96 or len(names) != len(set(names)):
        raise RuntimeError("manifest.json must declare exactly 96 unique tools.")

    if args.dry_run:
        results = [{"tool": name, "status": "not_run", "reason": "dry-run"} for name in names]
        mode = "dry-run"
    else:
        results = asyncio.run(run_live(project, names, args.connection_timeout))
        mode = "live"
    report = write_report(project, results, mode)
    print(f"mode={mode} tools={len(names)} report={report}")
    if args.live and results[0]["status"] == "failed":
        # A timed-out COM call can leave the worker thread blocked. ThreadPoolExecutor
        # workers are non-daemon, so use a controlled process exit after persisting the
        # diagnostic report instead of hanging the test command indefinitely.
        sys.stdout.flush()
        os._exit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
