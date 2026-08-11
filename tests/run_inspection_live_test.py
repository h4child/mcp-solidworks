"""Validate the inspection tools against a real SolidWorks session.

capture_viewport, get_selection and list_faces read live COM state, so a static
contract check cannot prove them: only a running SolidWorks shows whether the
returned geometry is real. This builds an isolated throwaway part with known
dimensions, asserts the reported areas against their analytic values, and then
closes the loop by feeding a discovered pick_point back into a modelling tool.

Use --live only on a test installation; it creates and modifies CAD documents.
Artifacts are written under tests/output/, which is git-ignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "tests" / "output"

# Fixture: a 100 x 50 x 20 mm block with a concentric 20 mm boss standing 40 mm
# tall, so the part carries both planar and cylindrical faces of known size.
BLOCK_X, BLOCK_Y, BLOCK_Z = 100.0, 50.0, 20.0
BOSS_RADIUS, BOSS_HEIGHT = 10.0, 40.0
HOLE_RADIUS, HOLE_DEPTH = 6.0, 15.0
AREA_TOLERANCE = 0.01  # mm^2


class CheckFailed(Exception):
    """A live assertion about SolidWorks-reported geometry did not hold."""


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, abs_tol=AREA_TOLERANCE)


async def run_live(project: Path) -> list[dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    import server

    results: list[dict[str, Any]] = []

    def record(step: str, ok: bool, detail: Any = None) -> None:
        results.append({"step": step, "status": "passed" if ok else "failed", "detail": detail})
        print(f"[{'OK  ' if ok else 'FAIL'}] {step}" + (f" :: {detail}" if detail else ""), flush=True)

    try:
        await server.connect_solidworks()
        record("connect_solidworks", True)

        await server.create_new_part()
        await server.create_sketch("front")
        await server.draw_rectangle(-BLOCK_X / 2, -BLOCK_Y / 2, BLOCK_X / 2, BLOCK_Y / 2, "mm")
        await server.close_sketch()
        await server.extrude_sketch(BLOCK_Z, unit="mm")
        await server.create_sketch("front")
        await server.draw_circle(0, 0, BOSS_RADIUS, "mm")
        await server.close_sketch()
        await server.extrude_sketch(BOSS_HEIGHT, unit="mm")
        await server.rebuild_model()
        record("fixture block + boss", True)

        # --- list_faces -----------------------------------------------------
        faces = await server.list_faces(unit="mm")
        kinds = sorted({face["surface_type"] for face in faces["faces"]})
        record("list_faces", faces["count"] == 8 and kinds == ["cylinder", "plane"],
               f"count={faces['count']} types={kinds}")
        (project / "inspection_faces.json").write_text(
            json.dumps(faces, indent=2, ensure_ascii=False), encoding="utf-8")

        areas = sorted(face["area"] for face in faces["faces"])
        boss_top = math.pi * BOSS_RADIUS ** 2
        expected = sorted([
            BLOCK_Y * BLOCK_Z, BLOCK_Y * BLOCK_Z,            # the two ends
            BLOCK_X * BLOCK_Z, BLOCK_X * BLOCK_Z,            # front and back
            BLOCK_X * BLOCK_Y,                               # base
            BLOCK_X * BLOCK_Y - boss_top,                    # top, pierced by the boss
            2 * math.pi * BOSS_RADIUS * (BOSS_HEIGHT - BLOCK_Z),  # boss wall
            boss_top,                                        # boss top
        ])
        record("face areas match their analytic values",
               len(areas) == len(expected) and all(_close(a, e) for a, e in zip(areas, expected)),
               f"measured={[round(a, 3) for a in areas]}")

        cylinders = [face for face in faces["faces"] if face["surface_type"] == "cylinder"]
        record("cylindrical face reports radius and no bogus normal",
               len(cylinders) == 1
               and _close(cylinders[0].get("radius", 0), BOSS_RADIUS)
               and "normal" not in cylinders[0],
               f"radius={cylinders[0].get('radius') if cylinders else None}")

        # The top face's bounding-box centre falls inside the boss, where there
        # is no material: the pick point must be projected back onto the face,
        # landing on or outside the boss circle rather than in the void.
        top = next(face for face in faces["faces"]
                   if _close(face["area"], BLOCK_X * BLOCK_Y - boss_top))
        top_pick = top["pick_point"]
        record("pick point avoids the void at the bounding-box centre",
               math.hypot(top_pick["x"], top_pick["y"]) >= BOSS_RADIUS - AREA_TOLERANCE,
               f"pick=({top_pick['x']:.3f}, {top_pick['y']:.3f}, {top_pick['z']:.3f})")

        filtered = await server.list_faces(surface_type="cylinder", unit="mm")
        record("list_faces filters by surface_type", filtered["count"] == 1,
               f"count={filtered['count']}")

        # SelectByID2 picks from the current camera, so a face hidden behind the
        # model cannot be reached: from the front view two of these eight points
        # resolve to nothing. Isometric is the orientation the docstring tells
        # callers to use, and every reported point must survive it.
        await server.set_view("isometric")
        unreachable = []
        for face in faces["faces"]:
            point = face["pick_point"]

            def _pick(point=point) -> bool:
                doc = server._active_doc()
                doc.ClearSelection2(True)
                return server._select_by_id(
                    doc, "", "FACE",
                    server.to_meters(point["x"], "mm"),
                    server.to_meters(point["y"], "mm"),
                    server.to_meters(point["z"], "mm"),
                )

            picked = await server._run(_pick)
            current = await server.get_selection(unit="mm")
            resolved = current["count"] == 1 and current["selections"][0]["type"] == "face"
            if not (picked and resolved):
                unreachable.append(round(face["area"], 3))
        record("every pick point resolves to a face from isometric",
               not unreachable, f"unreachable areas={unreachable}")

        # That sweep leaves its last face selected; the checks below describe
        # the selection they make themselves, so start from a clean slate.
        def _clear() -> None:
            server._active_doc().ClearSelection2(True)

        await server._run(_clear)

        # --- capture_viewport -----------------------------------------------
        image = await server.capture_viewport()
        before_shot = project / "inspection_viewport_before.png"
        before_shot.write_bytes(image.data)
        record("capture_viewport returns PNG bytes",
               image.data.startswith(b"\x89PNG") and len(image.data) > 1000,
               f"{len(image.data)} bytes -> {before_shot.name}")

        # --- get_selection ---------------------------------------------------
        empty = await server.get_selection(unit="mm")
        record("get_selection reports an empty selection", empty["count"] == 0,
               f"count={empty['count']}")

        base = max(faces["faces"], key=lambda face: face["area"])
        base_pick = base["pick_point"]

        def _select() -> bool:
            doc = server._active_doc()
            doc.ClearSelection2(True)
            return server._select_by_id(
                doc, "", "FACE",
                server.to_meters(base_pick["x"], "mm"),
                server.to_meters(base_pick["y"], "mm"),
                server.to_meters(base_pick["z"], "mm"),
            )

        record("a list_faces pick point selects its face", await server._run(_select))

        selection = await server.get_selection(unit="mm")
        record("get_selection identifies the selected face",
               selection["count"] == 1 and selection["selections"][0]["type"] == "face",
               json.dumps(selection["selections"], ensure_ascii=False))

        # --- closed loop: discovered geometry drives a real edit -------------
        before_features = {item["name"] for item in (await server.list_features())["features"]}
        await server.create_sketch_on_face(base_pick["x"], base_pick["y"], base_pick["z"], "mm")
        await server.draw_circle(0, 0, HOLE_RADIUS, "mm")
        await server.close_sketch()
        await server.cut_extrude(HOLE_DEPTH, unit="mm")
        await server.rebuild_model()
        after_features = {item["name"] for item in (await server.list_features())["features"]}
        record("cut a hole on the discovered face", bool(after_features - before_features),
               f"new features={sorted(after_features - before_features)}")

        health = await server.validate_model()
        record("model stays valid after the edit", bool(health["valid"]),
               f"errors={health['error_count']} warnings={health['warning_count']}")

        after_cylinders = await server.list_faces(surface_type="cylinder", unit="mm")
        radii = sorted(round(face.get("radius", 0), 3) for face in after_cylinders["faces"])
        record("list_faces sees the new hole", radii == sorted([BOSS_RADIUS, HOLE_RADIUS]),
               f"radii={radii}")

        final = await server.capture_viewport()
        after_shot = project / "inspection_viewport_after.png"
        after_shot.write_bytes(final.data)
        record("capture_viewport after the edit", len(final.data) > 1000,
               f"{len(final.data)} bytes -> {after_shot.name}")

        explicit = project / "inspection_viewport_explicit.png"
        await server.capture_viewport(output_path=str(explicit))
        record("capture_viewport writes an explicit output_path",
               explicit.exists() and explicit.stat().st_size > 0)

    except Exception as exc:  # noqa: BLE001 - the report must survive any failure
        record("unhandled error", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await server.close_document(save=False)
        except Exception:
            pass
        server._shutdown()

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Connect to SolidWorks and validate the inspection tools.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help="Directory for isolated test artifacts.")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live: these checks require a running SolidWorks session.")

    project = args.output_dir.resolve()
    if project == ROOT:
        parser.error("The output directory cannot be the repository root.")
    project.mkdir(parents=True, exist_ok=True)

    results = asyncio.run(run_live(project))
    failed = [item for item in results if item["status"] == "failed"]
    report = project / f"inspection-live-{datetime.now():%Y%m%d-%H%M%S}.json"
    report.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "total": len(results), "failed": len(failed), "results": results},
        indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed === report={report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
