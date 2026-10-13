"""Live verification of measure_body against analytically known geometry.

Builds a block and a cylinder of known dimensions, assigns a real material to
each, and compares measure_body's volume_m3/mass_kg/density_kg_m3 against the
hand-computed expectation instead of trusting the tool's own success return.
Also exercises the already-documented set_material/density limitation (see
README.md "set_material (EXP") so a reader can see exactly what it looks like
when it fires, rather than taking the documentation on faith.

Run on Windows with SolidWorks installed:  python tests/run_measure_body_live_test.py
"""
import asyncio
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

OUTPUT = ROOT / "tests" / "output"


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    await server.connect_solidworks()

    # --- block, 100 x 50 x 20 mm, AISI 1020 ---------------------------------
    expected_volume_m3 = 0.100 * 0.050 * 0.020  # 1e-4 m3
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-50, -25, 50, 25, "mm")
    await server.close_sketch()
    await server.extrude_sketch(20, unit="mm")

    material = await server.set_material("AISI 1020")
    print("\nset_material(AISI 1020):", material)

    measured = await server.measure_body()
    print("\nmeasure_body() on the 100x50x20mm block:")
    for key in ("volume_m3", "mass_kg", "density_kg_m3", "density_warning"):
        print(f"  {key}: {measured.get(key)}")

    volume_error = abs(measured["volume_m3"] - expected_volume_m3) / expected_volume_m3
    check("block volume matches the analytic 1e-4 m3 within 1%",
          volume_error < 0.01,
          f"measured={measured['volume_m3']:.6e} expected={expected_volume_m3:.6e} "
          f"error={volume_error:.4%}")

    looked_up = await server.lookup_material_properties("AISI 1020")
    print("\nlookup_material_properties(AISI 1020):", looked_up.get("density_kg_m3"))
    expected_density = looked_up.get("density_kg_m3")
    density_at_water = measured.get("density_kg_m3") is not None and \
        abs(measured["density_kg_m3"] - 1000.0) < 1.0
    check("density_warning fires exactly when the density is stuck at water (1000 kg/m3) "
          "-- see README.md 'set_material (EXP'",
          bool(measured.get("density_warning")) == density_at_water,
          f"density_kg_m3={measured.get('density_kg_m3')} warning={measured.get('density_warning')!r}")

    if expected_density and not density_at_water:
        expected_mass = expected_volume_m3 * expected_density
        mass_error = abs(measured["mass_kg"] - expected_mass) / expected_mass
        check("block mass matches volume x looked-up density within 1%",
              mass_error < 0.01,
              f"measured={measured['mass_kg']:.6f} expected={expected_mass:.6f} "
              f"error={mass_error:.4%}")
    else:
        print("[INFO] material density is not actually applied on this install; "
              "mass_kg is a water-density placeholder, not a real weight -- "
              "expected per README, not re-checked against a real mass here.")

    box = measured.get("bounding_box", {}).get("size", {})
    check("bounding box reports the three requested edges (100/50/20 mm, any order)",
          sorted(round(v, 2) for v in (box.get("x"), box.get("y"), box.get("z"))) == [20.0, 50.0, 100.0],
          str(box))

    # --- cylinder, diameter 30mm x height 40mm, AISI 1020 -------------------
    radius_m, height_m = 0.015, 0.040
    expected_volume_m3 = math.pi * radius_m ** 2 * height_m

    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_circle(0, 0, 15, "mm")
    await server.close_sketch()
    await server.extrude_sketch(40, unit="mm")
    await server.set_material("AISI 1020")

    measured_cyl = await server.measure_body()
    print("\nmeasure_body() on the Ø30x40mm cylinder:")
    for key in ("volume_m3", "mass_kg", "density_kg_m3", "density_warning"):
        print(f"  {key}: {measured_cyl.get(key)}")

    volume_error = abs(measured_cyl["volume_m3"] - expected_volume_m3) / expected_volume_m3
    check("cylinder volume matches pi*r^2*h within 1%",
          volume_error < 0.01,
          f"measured={measured_cyl['volume_m3']:.6e} expected={expected_volume_m3:.6e} "
          f"error={volume_error:.4%}")

    box_cyl = measured_cyl.get("bounding_box", {}).get("size", {})
    check("cylinder bounding box is diameter x diameter x height (30/30/40 mm, any order)",
          sorted(round(v, 2) for v in (box_cyl.get("x"), box_cyl.get("y"), box_cyl.get("z"))) == [30.0, 30.0, 40.0],
          str(box_cyl))

    server._shutdown()

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed.")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
