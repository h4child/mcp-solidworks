"""Live reproduction + fix verification for the cut_extrude(through_all=True,
both_directions=True) bug documented in .claude/knowledge/verificacao_e_qa.md
section 8: the native swEndCondThroughAllBoth end condition only cut one
side of a boss, with validate_model reporting zero errors.

Builds the exact failing shape -- a Ø20mm hole centered on a 70mm-long
cylindrical boss -- cuts it with through_all+both_directions, and measures
the resulting bore's cylindrical face: it has to span the full 70mm, not 35.
Run twice (two different boss lengths/diameters) so the fix is not pinned to
one magic number.
"""
import asyncio
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


async def build_and_cut(boss_length_mm, boss_radius_mm, hole_radius_mm):
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_circle(x=0, y=0, radius=boss_radius_mm, unit="mm")
    await server.close_sketch()
    await server.extrude_sketch(boss_length_mm, both_directions=True, unit="mm")

    await server.create_sketch("front")
    await server.draw_circle(x=0, y=0, radius=hole_radius_mm, unit="mm")
    await server.close_sketch()
    cut = await server.cut_extrude(through_all=True, both_directions=True, unit="mm")

    validation = await server.validate_model()
    faces = await server.list_faces(surface_type="cylinder", unit="mm")
    candidates = [f for f in faces["faces"] if abs(f.get("radius", -999) - hole_radius_mm) < 0.05]
    bore = candidates[0] if candidates else None
    hole_length_mm = None
    if bore is not None:
        circumference_mm = 2 * math.pi * bore["radius"]
        hole_length_mm = bore["area"] / circumference_mm

    await server.close_document(save=False)
    return cut, validation, hole_length_mm


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    await server.connect_solidworks()

    for boss_length, boss_radius, hole_radius in ((70, 15, 10), (40, 12, 6)):
        cut, validation, hole_length_mm = await build_and_cut(boss_length, boss_radius, hole_radius)
        print(f"\nboss_length={boss_length}mm hole_radius={hole_radius}mm -> "
              f"cut={cut}\nvalidation valid={validation.get('valid')} "
              f"error_count={validation.get('error_count')}\n"
              f"measured hole length={hole_length_mm}")
        check(f"[{boss_length}mm] validate_model reports no errors",
              validation.get("valid") and validation.get("error_count") == 0,
              str(validation.get("issues")))
        check(f"[{boss_length}mm] cut_extrude warns about the synthesized depth",
              bool(cut.get("warnings")), str(cut.get("warnings")))
        check(f"[{boss_length}mm] bore cylindrical face found",
              hole_length_mm is not None,
              "" if hole_length_mm is not None else "no matching cylindrical face")
        if hole_length_mm is not None:
            check(f"[{boss_length}mm] bore spans the FULL boss length (not half)",
                  abs(hole_length_mm - boss_length) < 0.5,
                  f"expected ~{boss_length}mm, measured {hole_length_mm:.3f}mm")

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    if passed != len(checks):
        sys.exit(1)

asyncio.run(main())
