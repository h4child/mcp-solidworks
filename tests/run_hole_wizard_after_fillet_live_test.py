"""Live reproduction + fix verification for the hole_wizard flakiness
documented in .claude/knowledge/verificacao_e_qa.md section 10: after a
global chamfer/fillet (chamfer_edges/fillet_edges affect EVERY edge, there is
no edge selection), the same exact face point from list_faces started
failing hole_wizard's face/point selection non-deterministically between
attempts.

hole_wizard's face step now resolves the face by GEOMETRY
(_planar_face_at_point, the same camera-independent lookup already used for
weldment faces) instead of SelectByID2 by coordinate, which picks from the
current camera view -- the actual source of the non-determinism. This
rebuilds the exact failing scenario (a block, a global fillet on every edge,
then hole_wizard on the top face at its centre) N times in fresh documents:
flaky means "fails sometimes", so passing once would not be convincing, but
passing every one of N independent rebuilds is.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

TRIALS = 3


async def one_trial(trial_index: int):
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-30, -20, 30, 20, "mm")
    await server.close_sketch()
    await server.extrude_sketch(15, unit="mm")
    await server.fillet_edges(1.5, unit="mm")  # global: every edge, including the top face's boundary

    faces = await server.list_faces(surface_type="plane", unit="mm")
    top = max(faces["faces"], key=lambda f: f["area"])
    point = top["pick_point"]

    result = await server.hole_wizard(
        face_x=point["x"], face_y=point["y"], face_z=point["z"],
        hole_x=0, hole_y=0, hole_type="simple", size=6, depth=8, unit="mm",
    )
    validation = await server.validate_model()
    await server.close_document(save=False)
    return result, validation


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    await server.connect_solidworks()

    for i in range(1, TRIALS + 1):
        try:
            result, validation = await one_trial(i)
            print(f"trial {i}: feature={result.get('feature')} "
                  f"valid={validation.get('valid')} errors={validation.get('error_count')}")
            check(f"trial {i}: hole_wizard placed the hole on the filleted face",
                  bool(result.get("feature")), str(result))
            check(f"trial {i}: validate_model reports no errors",
                  validation.get("valid") and validation.get("error_count") == 0,
                  str(validation.get("issues")))
        except Exception as exc:
            check(f"trial {i}: hole_wizard placed the hole on the filleted face", False, str(exc))
            check(f"trial {i}: validate_model reports no errors", False, "trial raised before validation")

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed across {TRIALS} independent rebuilds")
    if passed != len(checks):
        sys.exit(1)

asyncio.run(main())
