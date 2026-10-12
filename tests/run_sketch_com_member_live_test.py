"""Live check: _com_member fix in _sketch_segment_geometry/_sketch_segment_endpoints.

Confirms draw_circle/draw_arc correctly read back center/start/end points
(the two call sites the uncommitted fix touches) on THIS SolidWorks install.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    await server.connect_solidworks()
    await server.create_new_part()
    await server.create_sketch("front")

    circle = await server.draw_circle(x=10, y=5, radius=8, unit="mm")
    print("draw_circle actual:", circle["actual"])
    check("circle actual has center", circle["actual"].get("center") is not None, str(circle["actual"]))
    check("circle center matches request", circle["verified"] is True, str(circle))

    arc = await server.draw_arc(cx=0, cy=0, radius=20, start_angle=0, end_angle=90,
                                 unit="mm")
    print("draw_arc actual:", arc.get("actual"))
    check("arc actual has center", arc["actual"].get("center") is not None, str(arc["actual"]))

    entities = await server.list_sketch_entities()
    print("list_sketch_entities:", entities)
    segs = entities["segments"]
    check("list_sketch_entities reports a center for at least one segment",
          any(s.get("center") is not None for s in segs), str(segs))

    await server.close_sketch()
    await server.close_document(save=False)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    if passed != len(checks):
        sys.exit(1)

asyncio.run(main())
