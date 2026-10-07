"""Live verification of the snap-proof profile layer: draw_profile, the gear
built on it, and the knurl whose cells are smaller than a gear tooth.

The first question is still the blunt one -- does the gear actually have teeth?

The profile maths is covered off-line by tests/test_gear_geometry.py. What
this script checks is the part only a running SolidWorks can answer: that
SetAddToDB + several hundred CreateLine2 calls really do land a complete,
closed, un-snapped profile, that it extrudes, and that the solid's measured
volume matches the analytic cross-section of the outline that drew it.

The later sections check the same primitive on a curve that is not a gear (a
180-point cam outline, whose extruded volume is compared with the shoelace area
of the points that were sent) and on create_knurl, whose 0.2 mm cells are three
times tighter than the tooth spacing that collapsed.

Run on Windows with SolidWorks installed:  python tests/run_gear_live_test.py
"""
import asyncio
import math
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

    # --- draw_profile: the same primitive, on a curve that is not a gear ----
    # A cam-like outline, so the check is on the drawing path rather than on
    # gear_geometry: the extruded volume is compared with the shoelace area of
    # the very points that were sent.
    await server.create_new_part()
    cam = [
        [26 * math.cos(math.radians(a)) + 4 * math.cos(math.radians(2 * a)),
         26 * math.sin(math.radians(a))]
        for a in range(0, 360, 2)
    ]
    await server.create_sketch("front")
    profile = await server.draw_profile(cam, closed=True, unit="mm")
    print("\ndraw_profile(180-point cam outline):", {
        key: profile[key] for key in
        ("segments", "points_verified", "max_point_deviation", "snapped", "verified")
    })
    check("every vertex of a 180-point curve landed where it was computed",
          profile["verified"] is True,
          f"worst deviation {profile.get('max_point_deviation')} mm")
    check("one call drew one segment per point",
          profile["segments"] == len(cam))
    await server.close_sketch()
    await server.extrude_sketch(8, unit="mm")
    cam_solid = await server.measure_body()
    expected_mm3 = gg.polygon_area([(x, y) for x, y in cam]) * 8.0
    measured_mm3 = cam_solid["volume_m3"] * 1e9
    check("the extruded cam matches the area of the points that drew it",
          abs(measured_mm3 / expected_mm3 - 1) < 0.01,
          f"{measured_mm3:.1f} vs {expected_mm3:.1f} mm3")
    await server.close_document(save=False)

    # --- create_knurl: the cell profile is 0.2 mm across -------------------
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_circle(0, 0, 12, "mm")
    await server.close_sketch()
    await server.extrude_sketch(30, unit="mm")
    knurl = await server.create_knurl(12, 0, 15, "diamond", 0.8, 0.3, 30, "mm")
    print("\ncreate_knurl:", knurl.get("profile"))
    check("the knurl cells landed where they were computed",
          (knurl.get("profile") or {}).get("verified") is True, str(knurl.get("profile")))
    print("The engraved DEPTH is not readable from the Wrap feature -- look at the "
          "part, or section it, before calling the knurl good.")
    await server.close_document(save=False)

    server._shutdown()
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    print("=" * 70)
    return 0 if passed == len(checks) else 1


sys.exit(asyncio.run(main()))
