"""Live verification: insert_component's origin correction for SUB-ASSEMBLIES.

Documented gap (RELATORIO_ESCADA_INDUSTRIAL.md, "limitacoes conhecidas"):
insert_component corrects a PART's placement (origin, not bbox-centre) but
not a sub-assembly's, because GetBodies2 (what the correction was computed
from) returns nothing for an assembly document.

Builds two small parts, assembles them into an inner sub-assembly with their
component cloud deliberately OFF-CENTRE from the sub-assembly's own origin
(part A at x=80mm, part B at x=-20mm -> component-cloud centre near x=30mm,
origin still at x=0), saves it, then inserts that sub-assembly into a fresh
top assembly at a requested point. If the correction is not applied, the
sub-assembly's ORIGIN lands at (requested - ~30mm), not at the requested
point -- exactly the kind of silent placement error the escada report
flagged. insert_component's own verified/deviation fields catch this
directly: no extra measurement plumbing needed.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

OUT = ROOT / "tests" / "output"


async def build_simple_part(path: str, size_mm: float):
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-size_mm / 2, -size_mm / 2, size_mm / 2, size_mm / 2, "mm")
    await server.close_sketch()
    await server.extrude_sketch(size_mm, unit="mm")
    await server.save_document(path)
    await server.close_document(save=False)


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    OUT.mkdir(parents=True, exist_ok=True)
    part_a = str(OUT / "subassy_fix_part_a.SLDPRT")
    part_b = str(OUT / "subassy_fix_part_b.SLDPRT")
    inner_assy = str(OUT / "subassy_fix_inner.SLDASM")

    await server.connect_solidworks()

    await build_simple_part(part_a, 20)
    await build_simple_part(part_b, 20)

    await server.create_new_assembly()
    ins_a = await server.insert_component(part_a, x=80, y=0, z=0, unit="mm")
    ins_b = await server.insert_component(part_b, x=-20, y=0, z=0, unit="mm")
    check("part A placed at its requested origin inside the inner assembly",
          ins_a["verified"], str(ins_a.get("deviation")))
    check("part B placed at its requested origin inside the inner assembly",
          ins_b["verified"], str(ins_b.get("deviation")))
    await server.save_document(inner_assy)
    await server.close_document(save=False)

    await server.create_new_assembly()
    requested = (100.0, 50.0, 0.0)
    result = await server.insert_component(inner_assy, x=requested[0], y=requested[1],
                                            z=requested[2], unit="mm")
    print("\ninsert_component(sub-assembly) result:", result)

    correction = result.get("origin_correction", {})
    check("origin_correction was applied for the sub-assembly",
          correction.get("applied") is True, str(correction))
    check("origin_correction was measured from the component union, not bodies",
          correction.get("measured_from") == "component_union", str(correction))
    check("the sub-assembly's ORIGIN landed at the requested point (not its bbox centre)",
          result.get("verified") is True, str(result.get("deviation")))

    await server.close_document(save=False)

    # --- second scenario: a sub-assembly NESTED inside another sub-assembly,
    # proving the fix recurses rather than only handling one level deep ---
    part_c = str(OUT / "subassy_fix_part_c.SLDPRT")
    middle_assy = str(OUT / "subassy_fix_middle.SLDASM")

    await build_simple_part(part_c, 20)

    await server.create_new_assembly()
    await server.insert_component(inner_assy, x=0, y=0, z=0, unit="mm")
    ins_c = await server.insert_component(part_c, x=200, y=0, z=0, unit="mm")
    check("part C placed at its requested origin inside the middle assembly",
          ins_c["verified"], str(ins_c.get("deviation")))
    await server.save_document(middle_assy)
    await server.close_document(save=False)

    await server.create_new_assembly()
    requested2 = (-50.0, 25.0, 10.0)
    result2 = await server.insert_component(middle_assy, x=requested2[0], y=requested2[1],
                                             z=requested2[2], unit="mm")
    print("\ninsert_component(nested sub-assembly) result:", result2)
    correction2 = result2.get("origin_correction", {})
    check("[nested] origin_correction was applied",
          correction2.get("applied") is True, str(correction2))
    check("[nested] origin_correction recursed through the inner sub-assembly",
          correction2.get("measured_from") == "component_union", str(correction2))
    check("[nested] the middle assembly's ORIGIN landed at the requested point",
          result2.get("verified") is True, str(result2.get("deviation")))
    await server.close_document(save=False)

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    if passed != len(checks):
        sys.exit(1)

asyncio.run(main())
