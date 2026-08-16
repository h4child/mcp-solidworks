"""Live verification of the new/improved read tools: get_view_state,
list_features (suppressed/error_code), list_components (visible)."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    await server.connect_solidworks()

    # --- get_view_state on a part ---
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-50, -30, 50, 30, "mm")
    await server.close_sketch()
    await server.extrude_sketch(20, unit="mm")
    await server.fillet_edges(2, unit="mm")
    await server.rebuild_model()

    await server.set_view("isometric")
    state = await server.get_view_state()
    print("\nget_view_state after set_view('isometric'):", state)
    check("closest_named_view == 'isometric'", state["closest_named_view"] == "isometric")
    check("zoom_scale is a positive float", state["zoom_scale"] > 0)
    check("orientation_matrix has 9 values", len(state["orientation_matrix"]) == 9)
    check("active_configuration reported", bool(state["active_configuration"]))

    await server.set_view("top")
    state_top = await server.get_view_state()
    check("closest_named_view == 'top' after set_view('top')",
          state_top["closest_named_view"] == "top", f"got {state_top['closest_named_view']!r}")

    # Rotate away from any named view -> expect None, not a false match.
    def _rotate():
        doc = server._active_doc()
        doc.ActiveView.RotateAboutCenter(0.3, 0.1)
        return None
    await server._run(_rotate)
    state_custom = await server.get_view_state()
    check("closest_named_view is None after manual rotation",
          state_custom["closest_named_view"] is None, f"got {state_custom['closest_named_view']!r}")

    # --- list_features suppression/error reporting ---
    feats = await server.list_features()
    extrude = next((f for f in feats["features"] if "extru" in f["type"].lower()), None)
    check("list_features reports suppressed field", extrude is not None and "suppressed" in extrude,
          str(extrude))
    check("list_features reports error_code field", extrude is not None and "error_code" in extrude,
          str(extrude))
    check("healthy extrude has error_code 0", extrude is not None and extrude["error_code"] == 0)
    check("healthy extrude is not suppressed", extrude is not None and extrude["suppressed"] is False)

    await server.close_document(save=False)

    # --- list_components visibility reporting ---
    await server.create_new_assembly()
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(-10, -10, 10, 10, "mm")
    await server.close_sketch()
    await server.extrude_sketch(10, unit="mm")
    part_path = ROOT / "tests/output/read_tools_visibility_probe.sldprt"
    await server.save_document(str(part_path))
    await server.close_document(save=False)

    await server.create_new_assembly()
    await server.insert_component(str(part_path), 0, 0, 0, unit="mm")
    comps = await server.list_components()
    check("list_components reports visible field", comps["components"][0].get("visible") is not None,
          str(comps["components"][0]))
    check("newly inserted component is visible", comps["components"][0].get("visible") is True)

    await server.close_document(save=False)
    server._shutdown()

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"{passed}/{len(checks)} checks passed")
    print("=" * 70)
    return 0 if passed == len(checks) else 1


sys.exit(asyncio.run(main()))
