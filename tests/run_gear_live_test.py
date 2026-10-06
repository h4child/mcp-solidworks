"""Live verification of create_spur_gear: does the gear actually have teeth?

The profile maths is covered off-line by tests/test_gear_geometry.py. What
this script checks is the part only a running SolidWorks can answer: that
SetAddToDB + several hundred CreateLine2 calls really do land a complete,
closed, un-snapped profile, that it extrudes, and that the solid's measured
volume matches the analytic cross-section of the outline that drew it.

Run on Windows with SolidWorks installed:  python tests/run_gear_live_test.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
import gear_geometry as gg  # noqa: E402

OUTPUT = ROOT / "tests/output"


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    await server.connect_solidworks()

    # --- the ordinary case: module 2, 20 teeth, 10 mm wide, 8 mm bore -------
    await server.create_new_part()
    gear = await server.create_spur_gear(module=2, teeth=20, thickness=10,
                                         bore_diameter=8, unit="mm")
    print("\ncreate_spur_gear(m=2, z=20, b=10, bore=8):")
    for key in ("gear", "profile", "volume_mm3", "size_check"):
        print(f"  {key}: {gear[key]}")
    print(f"  warnings: {gear['warnings']}")

    check("the teeth are in the solid", gear["teeth_present"] is True,
          f"volume ratio {gear['volume_mm3']['ratio_measured_to_expected']}")
    check("the solid matches the analytic gear volume", gear["verified"] is True)
    check("the outline was drawn whole",
          gear["profile"]["sketch_segments"] == gear["profile"]["segments_per_tooth"] * 20,
          f"{gear['profile']['sketch_segments']} segments")
    check("the bounding box is the tip diameter and the face width",
          gear["size_check"] and gear["size_check"]["matches"] is True,
          str(gear["size_check"]))
    check("the pitch diameter is module * teeth",
          abs(gear["gear"]["pitch_diameter"] - 40.0) < 1e-6)
    check("the centre distance for an equal pair is reported",
          abs(gear["meshing"]["center_distance_with_equal_gear"] - 40.0) < 1e-6)
    check("a root fillet was cut", gear["profile"]["root_fillet_radius"] > 0,
          gear["profile"]["root_fillet"])

    # The feature tree has to be clean too: a profile SolidWorks accepted but
    # could not rebuild is a gear that disappears on the next edit.
    validation = await server.validate_model()
    print("\nvalidate_model:", validation)
    check("the part rebuilds with no errors", not validation.get("errors"),
          str(validation.get("errors")))

    # An independent read of the same solid, not the tool's own payload.
    measured = await server.measure_body()
    tip = gear["gear"]["tip_diameter"]
    size = measured["bounding_box"]["size"]
    check("measure_body agrees on the tip diameter",
          abs(size["x"] - tip) < 0.5 and abs(size["y"] - tip) < 0.5,
          f"{size} vs tip diameter {tip}")

    await server.set_view("front")
    await server.zoom_to_fit()
    shots = await server.capture_standard_views(str(OUTPUT), "spur_gear_m2_z20")
    print("\nviews:", shots)
    print("Look at the front view: 20 distinct teeth, flat tips, filleted roots.")
    await server.save_document(str(OUTPUT / "spur_gear_m2_z20.sldprt"))
    await server.close_document(save=False)

    # --- a mating gear: same module, different tooth count -----------------
    await server.create_new_part()
    pinion = await server.create_spur_gear(module=2, teeth=40, thickness=10,
                                           bore_diameter=12, unit="mm")
    check("a 40-tooth gear of the same module also comes out toothed",
          pinion["teeth_present"] is True and pinion["verified"] is True,
          f"ratio {pinion['volume_mm3']['ratio_measured_to_expected']}")
    check("the 40-tooth gear is twice the pitch diameter",
          abs(pinion["gear"]["pitch_diameter"] - 80.0) < 1e-6)
    expected_centre = gg.center_distance(2.0, 20, 40)
    print(f"\nthe 20/40 pair meshes at {expected_centre} mm between centres, "
          f"ratio {40 / 20}")
    await server.close_document(save=False)

    # --- the warnings that keep a bad gear from being reported as good -----
    await server.create_new_part()
    undercut = await server.create_spur_gear(module=2, teeth=12, thickness=10, unit="mm")
    check("an undercut pinion is built but flagged",
          any("undercut" in w for w in undercut["warnings"]),
          str(undercut["warnings"]))
    check("the undercut pinion still has its teeth", undercut["teeth_present"] is True)
    await server.close_document(save=False)

    # --- the comparison that motivated the tool ---------------------------
    # The same gear attempted the generic way: one tooth gap cut, patterned.
    # Expected to come out smooth, or to fail outright. It is run so the
    # difference is on the record rather than asserted from memory.
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_circle(0, 0, 22, "mm")
    await server.close_sketch()
    await server.extrude_sketch(10, unit="mm")
    blank = await server.measure_body()
    profile = gg.spur_gear_outline(2.0, 20)
    try:
        await server.create_sketch("front")
        # One tooth gap, drawn the way a model would improvise it.
        gap = profile.points[:profile.points_per_tooth]
        for start, end in zip(gap, gap[1:]):
            await server.draw_line(*start, *end, unit="mm")
        await server.close_sketch()
        await server.cut_extrude(through_all=True, unit="mm")
        await server.circular_pattern("Cortar-Extrudar1", "z", 20, 360)
        hand_made = await server.measure_body()
        toothless = hand_made["volume_m3"] > blank["volume_m3"] * 0.98
        print(f"\nthe hand-drawn attempt left {hand_made['volume_m3'] * 1e9:.1f} mm3 "
              f"of a {blank['volume_m3'] * 1e9:.1f} mm3 blank")
        check("the hand-drawn attempt is the smooth disc this tool replaces",
              toothless, "it came out toothed -- re-read the premise in the README")
    except Exception as exc:
        print(f"\nthe hand-drawn attempt failed outright: {exc}")
        check("the hand-drawn attempt does not silently produce a gear", True,
              "it raised instead of returning a disc")
    await server.close_document(save=False)

    server._shutdown()
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    print("=" * 70)
    return 0 if passed == len(checks) else 1


sys.exit(asyncio.run(main()))
