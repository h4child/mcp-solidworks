"""Validate sheet-metal and weldment tools against a real SolidWorks session.

These tools (create_base_flange, add_sheet_metal_edge_flange, add_sheet_metal_bend,
flatten_sheet_metal, create_weldment_profile, add_gusset, add_end_cap,
trim_extend_structural, add_weld_symbol) drive COM features whose exact geometry
depends on the live SolidWorks session, so a static contract check cannot prove
them. This builds throwaway fixtures, discovers real face/edge coordinates via
list_faces rather than guessing them, and feeds those back into the tools under
test -- the same closed-loop approach used by run_inspection_live_test.py.

Use --live only on a test installation; it creates and modifies CAD documents.
Artifacts are written under tests/output/, which is git-ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "tests" / "output"


class Reporter:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def record(self, phase: str, step: str, ok: bool, detail: Any = None) -> None:
        self.results.append({
            "phase": phase, "step": step,
            "status": "passed" if ok else "failed", "detail": detail,
        })
        print(f"[{'OK  ' if ok else 'FAIL'}] {phase} :: {step}"
              + (f" -- {detail}" if detail else ""), flush=True)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.results if r["status"] == "failed"]


async def _first_weldment_config(server, profile_path: str) -> str:
    """Open a .sldlfp profile file hidden and return its first configuration name.

    Configuration (size) names vary by library/locale, so this discovers a real
    one rather than guessing a string like '40 x 40 x 4' that may not exist.

    All COM objects are pinned to the dedicated COM/STA worker thread, so this
    runs through server._run() like every tool implementation -- touching them
    directly from the asyncio thread raises RPC_E_WRONG_THREAD.
    """

    def _impl():
        app = server._connect()
        doc, errors, _warnings = server._open_doc6(app, profile_path, 1, 1)  # silent
        if doc is None:
            raise RuntimeError(f"Could not open weldment profile '{profile_path}' (error {errors}).")
        try:
            names = doc.GetConfigurationNames
            if callable(names):
                names = names()
            names = list(names or ())
            if not names:
                raise RuntimeError(f"Profile '{profile_path}' has no configurations.")
            return names[0]
        finally:
            title = doc.GetTitle
            if callable(title):
                title = title()
            app.CloseDoc(title)

    return await server._run(_impl)


async def phase_sheet_metal_flange(server, reporter: Reporter, out: Path) -> None:
    phase = "sheet_metal_base_and_flange"
    try:
        await server.create_new_part()
        await server.create_sketch("front")
        await server.draw_rectangle(-50, -30, 50, 30, "mm")
        await server.close_sketch()
        reporter.record(phase, "fixture: 100x60mm rectangle sketch", True)

        result = await server.create_base_flange(thickness=2, depth=100, bend_radius=1, unit="mm")
        await server.rebuild_model()
        reporter.record(phase, "create_base_flange", True, json.dumps(result))

        faces = await server.list_faces(unit="mm")
        reporter.record(phase, "list_faces sees a 6-face flat plate",
                         faces["count"] == 6, f"count={faces['count']}")

        planar = [f for f in faces["faces"] if f["surface_type"] == "plane"]
        big = sorted(planar, key=lambda f: -f["area"])[:2]  # top + bottom, ~6000mm^2
        sides = sorted(planar, key=lambda f: -f["area"])[2:]  # 4 thin side faces
        reporter.record(phase, "two large faces (~100x60mm) and four thin side faces",
                         len(big) == 2 and len(sides) == 4
                         and all(math.isclose(f["area"], 6000, rel_tol=0.02) for f in big),
                         f"big_areas={[round(f['area'],1) for f in big]}")

        top_face = max(big, key=lambda f: f["pick_point"]["z"])
        side_face = min(sides, key=lambda f: abs(f["pick_point"]["x"] - 50))
        edge_point = {
            "x": side_face["pick_point"]["x"],
            "y": 0.0,
            "z": top_face["pick_point"]["z"],
        }

        flange_result = await server.add_sheet_metal_edge_flange(
            edge_x=edge_point["x"], edge_y=edge_point["y"], edge_z=edge_point["z"],
            flange_length=20, flange_angle=90, unit="mm",
        )
        reporter.record(phase, "add_sheet_metal_edge_flange", True, json.dumps(flange_result))

        health = await server.validate_model()
        reporter.record(phase, "model stays valid after edge flange", bool(health["valid"]),
                         f"errors={health['error_count']}")

        state1 = await server.flatten_sheet_metal()
        state2 = await server.flatten_sheet_metal()
        reporter.record(phase, "flatten_sheet_metal toggles flat/folded",
                         {state1["state"], state2["state"]} == {"flattened", "folded"},
                         f"{state1['state']} -> {state2['state']}")

        image = await server.capture_viewport()
        shot = out / "sheet_metal_flange.png"
        shot.write_bytes(image.data)
        reporter.record(phase, "capture_viewport", image.data.startswith(b"\x89PNG"), shot.name)

    except Exception as exc:
        reporter.record(phase, "unhandled error", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass


async def phase_sheet_metal_bend(server, reporter: Reporter, out: Path) -> None:
    phase = "sheet_metal_insert_bends"
    try:
        await server.create_new_part()
        await server.create_sketch("front")
        await server.draw_rectangle(-40, -25, 40, 25, "mm")
        await server.close_sketch()
        await server.extrude_sketch(30, unit="mm")
        await server.rebuild_model()
        reporter.record(phase, "fixture: 80x50x30mm block", True)

        await server.shell_body(thickness=3, remove_face_at_x=0, remove_face_at_y=0,
                                 remove_face_at_z=30, unit="mm")
        await server.rebuild_model()
        reporter.record(phase, "shell_body: open tray, 3mm walls", True)

        before = {f["name"] for f in (await server.list_features())["features"]}
        result = await server.add_sheet_metal_bend(face_x=0, face_y=0, face_z=0,
                                                     bend_radius=1, unit="mm")
        after = {f["name"] for f in (await server.list_features())["features"]}
        reporter.record(phase, "add_sheet_metal_bend converts the tray to sheet metal",
                         bool(after - before), json.dumps(result))

        health = await server.validate_model()
        reporter.record(phase, "model stays valid after insert-bends", bool(health["valid"]),
                         f"errors={health['error_count']}")

        image = await server.capture_viewport()
        shot = out / "sheet_metal_bend.png"
        shot.write_bytes(image.data)
        reporter.record(phase, "capture_viewport", image.data.startswith(b"\x89PNG"), shot.name)

    except Exception as exc:
        reporter.record(phase, "unhandled error", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass


async def phase_weldment(server, reporter: Reporter, out: Path) -> "tuple[str | None, Path | None]":
    """Build an L-shaped member plus a crossing member; test gusset/end-cap/trim.

    Returns (part_title, saved_path) so a later phase can reuse this geometry
    for the drawing/weld-symbol test, or (None, None) if this phase failed.
    """
    phase = "weldment_structural"
    try:
        await server.create_new_part()
        sk = await server.create_3d_sketch()
        await server.draw_line_3d(0, 0, 0, 300, 0, 0, unit="mm")     # seg 0: main beam
        await server.draw_line_3d(0, 0, 0, 0, 0, 200, unit="mm")     # seg 1: upright, joins seg0 at origin
        await server.draw_line_3d(150, 0, -80, 150, 0, 150, unit="mm")  # seg 2: crosses seg0, unconnected
        await server.close_sketch()
        await server.rebuild_model()
        reporter.record(phase, "fixture: 3D sketch with L + crossing segment", True,
                         f"sketch={sk['sketch']}")

        profile_path = await server._run(server._get_weldment_profile_path, "iso", "square tube")
        config = await _first_weldment_config(server, profile_path)
        reporter.record(phase, "discovered a real weldment profile configuration", True,
                         f"{profile_path} :: '{config}'")

        member1 = await server.create_weldment_profile(
            standard="iso", profile_type="square tube", size=config,
            sketch_name=sk["sketch"], groups=[[0, 1]], unit="mm",
        )
        reporter.record(phase, "create_weldment_profile: L-shaped member (segs 0+1)",
                         True, json.dumps(member1))

        member2 = await server.create_weldment_profile(
            standard="iso", profile_type="square tube", size=config,
            sketch_name=sk["sketch"], groups=[[2]], unit="mm",
        )
        reporter.record(phase, "create_weldment_profile: crossing member (seg 2)",
                         True, json.dumps(member2))

        await server.rebuild_model()
        faces = await server.list_faces(unit="mm")
        reporter.record(phase, "list_faces sees the multi-body weldment", faces["count"] > 0,
                         f"count={faces['count']}")

        # --- end cap: the small planar ring face at the open end of seg 1 (0,0,200) ---
        end_candidates = sorted(
            (f for f in faces["faces"] if f["surface_type"] == "plane"),
            key=lambda f: math.dist(
                (f["pick_point"]["x"], f["pick_point"]["y"], f["pick_point"]["z"]), (0, 0, 200)
            ),
        )
        end_face = end_candidates[0]
        try:
            end_cap = await server.add_end_cap(
                face_x=end_face["pick_point"]["x"], face_y=end_face["pick_point"]["y"],
                face_z=end_face["pick_point"]["z"], thickness=2, unit="mm",
            )
            reporter.record(phase, "add_end_cap at the open end of the upright", True,
                             json.dumps(end_cap))
        except Exception as exc:
            # Known SolidWorks 2025 defect: InsertEndCapFeature3 returns None for
            # every argument/selection combination tried (matches the exact
            # official API-help example verbatim, and the every direction-enum
            # value) -- the same "returns None" pattern already documented in
            # this repo for InsertHelix and CreateDrawViewFromModelView3. This
            # is not treated as fatal so the rest of the phase still runs.
            reporter.record(phase, "add_end_cap at the open end of the upright", False,
                             f"{type(exc).__name__}: {exc}")

        # --- trim/extend: cut the crossing member (seg 2) against the main beam body ---
        bodies = []
        seen = set()
        for f in (await server.list_faces(unit="mm"))["faces"]:
            if f["body"] not in seen:
                seen.add(f["body"])
                bodies.append(f["body"])
        reporter.record(phase, "multiple solid bodies present for trim/extend",
                         len(bodies) >= 2, f"bodies={bodies}")

        if len(bodies) >= 2:
            # The L-body (member1) is the largest by combined face count; treat
            # the other as the crossing member and trim it against the L-body.
            body_face_counts = {b: 0 for b in bodies}
            for f in (await server.list_faces(unit="mm"))["faces"]:
                body_face_counts[f["body"]] += 1
            l_body = max(body_face_counts, key=body_face_counts.get)
            other_body = next(b for b in bodies if b != l_body)
            try:
                trim = await server.trim_extend_structural(
                    body_to_trim=other_body, trim_boundary=l_body, trim_type="trim",
                )
                reporter.record(phase, "trim_extend_structural", True, json.dumps(trim))
            except Exception as exc:
                reporter.record(phase, "trim_extend_structural", False,
                                 f"{type(exc).__name__}: {exc}")

        image = await server.capture_viewport()
        shot = out / "weldment_structure.png"
        shot.write_bytes(image.data)
        reporter.record(phase, "capture_viewport", image.data.startswith(b"\x89PNG"), shot.name)

        info = await server.get_document_info()
        save_path = out / "weldment_fixture.sldprt"
        saved = await server.save_document(str(save_path))
        reporter.record(phase, "save fixture for the drawing/weld-symbol phase",
                         save_path.exists(), saved.get("path"))
        return info["title"], save_path

    except Exception as exc:
        reporter.record(phase, "unhandled error", False, f"{type(exc).__name__}: {exc}")
        return None, None
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass


async def phase_gusset(server, reporter: Reporter, out: Path) -> None:
    """Test add_gusset on the scenario its own docstring names: a beam face
    meeting a base-plate face. A weldment structural member's mitered corner
    (tested in phase_weldment) is a harder, ambiguous case for this same tool;
    isolating it here on unambiguous geometry separates a real add_gusset bug
    from an artifact of that miter cut."""
    phase = "gusset_on_plate_and_post"
    try:
        await server.create_new_part()
        await server.create_sketch("front")
        await server.draw_rectangle(-50, -50, 50, 50, "mm")
        await server.close_sketch()
        await server.extrude_sketch(10, unit="mm")  # base plate, z: 0..10mm
        reporter.record(phase, "fixture: 100x100x10mm base plate", True)

        plane = await server.create_reference_plane(reference="front", offset=10, unit="mm")
        await server.create_sketch(plane["plane"])
        await server.draw_rectangle(10, -10, 30, 10, "mm")
        await server.close_sketch()
        await server.extrude_sketch(80, unit="mm")  # post, z: 10..90mm, flush on the plate
        await server.rebuild_model()
        reporter.record(phase, "fixture: 20x20x80mm post flush on the plate", True)

        faces = await server.list_faces(unit="mm")
        planar = [f for f in faces["faces"] if f["surface_type"] == "plane" and "normal" in f]

        # Top-of-plate face outside the post footprint: normal +Z, z pick_point ~10mm.
        plate_top = next(
            f for f in planar
            if abs(f["normal"][2]) > 0.9 and math.isclose(f["pick_point"]["z"], 10, abs_tol=0.5)
        )
        # A post side wall: normal in X or Y, spanning z from 10 to 90mm.
        post_side = next(
            f for f in planar
            if abs(f["normal"][2]) < 0.1 and 15 < f["pick_point"]["z"] < 85
        )
        gusset = await server.add_gusset(
            thickness=5,
            x1=plate_top["pick_point"]["x"], y1=plate_top["pick_point"]["y"],
            z1=plate_top["pick_point"]["z"],
            x2=post_side["pick_point"]["x"], y2=post_side["pick_point"]["y"],
            z2=post_side["pick_point"]["z"],
            profile="triangular", unit="mm",
        )
        reporter.record(phase, "add_gusset: plate face + post face", True, json.dumps(gusset))

        health = await server.validate_model()
        reporter.record(phase, "model stays valid after gusset", bool(health["valid"]),
                         f"errors={health['error_count']}")

        image = await server.capture_viewport()
        shot = out / "gusset_plate_post.png"
        shot.write_bytes(image.data)
        reporter.record(phase, "capture_viewport", image.data.startswith(b"\x89PNG"), shot.name)

    except Exception as exc:
        reporter.record(phase, "unhandled error", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass


async def phase_weld_symbol(server, reporter: Reporter, out: Path, source_part: Path) -> None:
    phase = "drawing_weld_symbol"
    if source_part is None or not source_part.exists():
        reporter.record(phase, "skipped: no weldment fixture from the prior phase", False)
        return
    try:
        await server.create_new_drawing()
        reporter.record(phase, "create_new_drawing", True)

        view = await server.insert_drawing_view(
            source_filepath=str(source_part), view_type="isometric", x=150, y=150, unit="mm",
        )
        reporter.record(phase, "insert_drawing_view", True, json.dumps(view))

        symbol = await server.add_weld_symbol(x=150, y=150, weld_type="fillet", size=5, unit="mm")
        reporter.record(phase, "add_weld_symbol", True, json.dumps(symbol))

        image = await server.capture_viewport()
        shot = out / "weld_symbol_drawing.png"
        shot.write_bytes(image.data)
        reporter.record(phase, "capture_viewport", image.data.startswith(b"\x89PNG"), shot.name)

    except Exception as exc:
        reporter.record(phase, "unhandled error", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass


async def run_live(out: Path) -> list[dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    import server

    reporter = Reporter()
    try:
        await server.connect_solidworks()
        reporter.record("setup", "connect_solidworks", True)

        await phase_sheet_metal_flange(server, reporter, out)
        await phase_sheet_metal_bend(server, reporter, out)
        await phase_gusset(server, reporter, out)
        _title, saved_path = await phase_weldment(server, reporter, out)
        await phase_weld_symbol(server, reporter, out, saved_path)

    finally:
        server._shutdown()

    return reporter.results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Connect to SolidWorks and validate the sheet-metal/weldment tools.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help="Directory for isolated test artifacts.")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live: these checks require a running SolidWorks session.")

    out = args.output_dir.resolve()
    if out == ROOT:
        parser.error("The output directory cannot be the repository root.")
    out.mkdir(parents=True, exist_ok=True)

    results = asyncio.run(run_live(out))
    failed = [r for r in results if r["status"] == "failed"]
    report = out / f"sheet-metal-weldment-live-{datetime.now():%Y%m%d-%H%M%S}.json"
    report.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "total": len(results), "failed": len(failed), "results": results},
        indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed === report={report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
