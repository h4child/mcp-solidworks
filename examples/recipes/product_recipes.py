"""
Finished-product recipes, moved out of server.py (v5.22.0).

These four coroutines build complete products (an automotive piston, a
piston + connecting-rod assembly, a pedestal-fan propeller) by composing the
server's public tools. They were never registered as MCP tools -- the README's
tool policy keeps the public catalog to reusable CAD operations -- but they
sat inside server.py as ~830 lines every reader had to skip.

They are kept verbatim (only the imports below are new) as worked examples of
multi-tool orchestration and as the reference implementations behind the
live results recorded in docs/reports/RELATORIO_TESTES.md. Run them from a
script on a Windows machine with SolidWorks open, e.g.:

    import asyncio, sys
    sys.path.insert(0, "<repo root>")
    from examples.recipes.product_recipes import create_automotive_piston
    asyncio.run(create_automotive_piston())
"""
import math
import os
import win32com
from typing import Optional

from server import (  # noqa: F401 -- names the recipes below rely on
    _active_doc,
    _run,
    apply_metal_finish,
    close_document,
    close_sketch,
    create_new_assembly,
    create_new_part,
    create_reference_plane,
    create_sketch,
    cut_extrude,
    draw_circle,
    draw_line,
    draw_rectangle,
    extrude_sketch,
    get_document_info,
    insert_component,
    move_copy_body,
    save_document,
    set_appearance,
    set_view,
    shell_body,
    to_meters,
    zoom_to_fit,
)
import server as _server


# ===========================================================================
# Internal project recipes
#
# These implementations remain available to local example/workflow code, but
# deliberately are not decorated with @mcp.tool. The public catalog is for
# reusable CAD operations and parametrized component categories only.
# ===========================================================================

# Project recipe kept as an internal reference implementation.  It is not an
# MCP tool: public tools must describe reusable CAD operations or parametrized
# component categories, not finished products.
async def create_automotive_piston(
    bore_diameter: float = 86,
    height: float = 75,
    skirt_thickness: float = 3.5,
    crown_thickness: float = 6,
    ring_width: float = 3,
    ring_depth: float = 1.5,
    wrist_pin_diameter: float = 22,
    unit: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Create a parametric automotive piston as a new SolidWorks part.

    The model has a crowned cylindrical skirt, three compression/oil-ring
    grooves, a hollow underside, and a transverse wrist-pin bore. All values
    are dimensional; use ``unit`` to override the MCP default. If ``save_path``
    is provided, the part is saved before features are created so an incomplete
    model remains available for diagnosis if SolidWorks rejects a later feature.
    """

    if bore_diameter <= 0 or height <= 0 or wrist_pin_diameter <= 0:
        raise ValueError("bore_diameter, height, and wrist_pin_diameter must be positive.")
    if min(skirt_thickness, crown_thickness, ring_width, ring_depth) <= 0:
        raise ValueError("Wall, crown, ring width, and ring depth must be positive.")
    if wrist_pin_diameter >= bore_diameter:
        raise ValueError("wrist_pin_diameter must be smaller than bore_diameter.")
    if 3 * ring_width + crown_thickness >= height:
        raise ValueError("height is too small for the requested crown and three ring grooves.")
    if skirt_thickness + ring_depth >= bore_diameter / 2:
        raise ValueError("Skirt thickness and ring depth leave no material in the piston wall.")

    completed: list[str] = []
    stage = "initialization"
    radius = bore_diameter / 2
    crown_start = height - crown_thickness
    ring_gap = ring_width * 1.25
    ring_bottoms = [
        crown_start - ring_width,
        crown_start - ring_width - ring_gap - ring_width,
        crown_start - 2 * ring_width - 2 * ring_gap - ring_width,
    ]

    try:
        stage = "new part"
        part = await create_new_part()
        completed.append(stage)

        if save_path:
            stage = "initial save"
            await save_document(save_path)
            completed.append(stage)

        stage = "extruded piston skirt"
        await create_sketch("front")
        await draw_circle(0, 0, radius, unit)
        await close_sketch()
        await extrude_sketch(height, unit=unit)
        completed.append(stage)

        # Offset planes let each annular cut start at its intended axial height.
        # This avoids relying on a revolved cut's local-plane orientation.
        stage = "three ring grooves"
        # The groove is the gap between two circles whose radii differ by
        # 2 * ring_depth -- 3 mm by default. Both go through the sketch
        # inference engine, and the second circle's defining point lands that
        # close to the first one's: inside the snap radius at an unlucky zoom,
        # the two radii collapse, the annulus has no width, and the cut removes
        # nothing. draw_circle measures its own radius and centre, so the
        # failure is detectable -- it just was not being read. It is now.
        groove_checks = []
        for ring_bottom in reversed(ring_bottoms):
            plane = await create_reference_plane("front", ring_bottom, unit=unit)
            await create_sketch(plane["plane"])
            outer = await draw_circle(0, 0, radius + ring_depth, unit)
            inner = await draw_circle(0, 0, radius - ring_depth, unit)
            for which, circle in (("outer", outer), ("inner", inner)):
                if circle.get("verified") is False:
                    raise RuntimeError(
                        f"the {which} circle of the ring groove at {ring_bottom} "
                        f"{unit or _server._default_unit} is not the circle that was asked for "
                        f"({circle.get('deviation')}). The groove would come out with "
                        f"the wrong depth, or with none at all, and the piston would "
                        f"still look right: "
                        + " | ".join(circle.get("warnings", ())))
            measured_width = None
            outer_radius = (outer.get("actual") or {}).get("radius")
            inner_radius = (inner.get("actual") or {}).get("radius")
            if outer_radius is not None and inner_radius is not None:
                measured_width = outer_radius - inner_radius
                if measured_width < ring_depth:
                    raise RuntimeError(
                        f"the ring groove at {ring_bottom} {unit or _server._default_unit} "
                        f"measures {measured_width:.4g} radially where "
                        f"{2 * ring_depth:.4g} was asked for: the two circles came out "
                        f"closer together than requested, so the groove is too shallow "
                        f"to be a ring groove."
                    )
            groove_checks.append({
                "axial_position": ring_bottom,
                "requested_radial_width": 2 * ring_depth,
                "measured_radial_width": measured_width,
            })
            await close_sketch()
            await cut_extrude(ring_width, unit=unit)
        completed.append(stage)

        # Open the underside with the supported shell feature, preserving the
        # crown and a controlled skirt wall behind the ring grooves.
        stage = "hollow underside"
        await shell_body(
            skirt_thickness,
            remove_face_at_x=0,
            remove_face_at_y=0,
            remove_face_at_z=0,
            unit=unit,
        )
        completed.append(stage)

        # A right-plane through cut creates the transverse wrist-pin bore
        # without needing to sketch directly on the cylindrical skirt.
        stage = "wrist-pin bore"
        await create_sketch("right")
        await draw_circle(0, height * 0.48, wrist_pin_diameter / 2, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        completed.append(stage)

        stage = "appearance and view"
        await set_appearance(185, 190, 200, "body")
        await set_view("isometric")
        await zoom_to_fit()
        completed.append(stage)

        if save_path:
            stage = "final save"
            await save_document()
            completed.append(stage)

        return {
            "document": part["title"],
            "save_path": os.path.abspath(save_path) if save_path else None,
            "dimensions": {
                "bore_diameter": bore_diameter,
                "height": height,
                "wrist_pin_diameter": wrist_pin_diameter,
                "unit": unit or _server._default_unit,
            },
            "features": ["extruded skirt/crown", "three ring grooves", "hollow underside", "wrist-pin bore"],
            "ring_grooves": groove_checks,
            "completed_stages": completed,
        }
    except Exception as exc:
        try:
            active = await get_document_info()
        except Exception:
            active = {"title": "(no active document)", "type": "Unknown"}
        raise RuntimeError(
            f"Automotive piston creation stopped during '{stage}'. "
            f"Completed stages: {', '.join(completed) or '(none)'}. "
            f"Active document: {active}. Root cause: {exc}"
        ) from exc


# Internal project recipe; intentionally excluded from the public MCP catalog.
async def create_automotive_piston_assembly(
    bore_diameter: float = 86,
    piston_height: float = 75,
    connecting_rod_length: float = 135,
    wrist_pin_diameter: float = 22,
    crank_bore_diameter: float = 42,
    rod_width: float = 16,
    unit: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Create an automotive piston-and-connecting-rod assembly, UNMATED.

    Builds the piston with three ring grooves and a hollow skirt, a transverse
    wrist pin, an I-style connecting rod, a plain big-end bearing and a
    separate rod cap. Each component is saved as an editable SolidWorks part
    and inserted into a final ``.SLDASM``.

    WHAT THIS DOES NOT DO. It creates NO MATES. The five components are
    positioned by computed x/y/z offsets and nothing constrains them to each
    other, so:

      - it is not a moving assembly. There is no kinematic joint; nothing in
        it can rotate or slide.
      - whether the rod actually seats between the piston's pin bosses
        depends entirely on how each part sits relative to its OWN origin.
        Any mismatch shows up as a gap, and nothing here measures it.
      - no interference check and no fit verification is performed.

    Treat the result as a positioned reference model, not a working joint.
    For a real joint, mate the components yourself -- concentric on the pin
    against both the boss bore and the rod eye, plus a width mate to centre
    the rod in the gap between the bosses -- and then confirm the outcome
    with get_component_transform and interference_check. A concentric mate
    removes only two degrees of freedom and leaves sliding along the shared
    axis FREE, which is the usual reason a piston-rod joint looks aligned
    and is not. See .claude/knowledge/montagens_mecanicas_reais.md.

    ``connecting_rod_length`` is the distance between the wrist-pin and
    crank-pin centers.  All dimensions use ``unit`` (mm by default).  Pass
    ``save_path`` for the final assembly; component parts are saved beside it.
    """

    values = {
        "bore_diameter": bore_diameter,
        "piston_height": piston_height,
        "connecting_rod_length": connecting_rod_length,
        "wrist_pin_diameter": wrist_pin_diameter,
        "crank_bore_diameter": crank_bore_diameter,
        "rod_width": rod_width,
    }
    if any(value <= 0 for value in values.values()):
        raise ValueError("All assembly dimensions must be positive.")
    if wrist_pin_diameter >= bore_diameter:
        raise ValueError("wrist_pin_diameter must be smaller than bore_diameter.")
    if crank_bore_diameter >= bore_diameter:
        raise ValueError("crank_bore_diameter must be smaller than bore_diameter.")
    if connecting_rod_length < piston_height:
        raise ValueError("connecting_rod_length must be at least piston_height.")
    if rod_width >= bore_diameter:
        raise ValueError("rod_width must be smaller than bore_diameter.")

    completed: list[str] = []
    stage = "initialization"
    output_dir = os.path.dirname(os.path.abspath(save_path)) if save_path else os.path.join(os.getcwd(), "tests", "output")
    assembly_path = os.path.abspath(save_path) if save_path else os.path.join(output_dir, "automotive_piston_connecting_rod_assembly.SLDASM")
    base_name = os.path.splitext(os.path.basename(assembly_path))[0]
    part_paths = {
        "piston": os.path.join(output_dir, f"{base_name}_piston.SLDPRT"),
        "connecting_rod": os.path.join(output_dir, f"{base_name}_connecting_rod.SLDPRT"),
        "wrist_pin": os.path.join(output_dir, f"{base_name}_wrist_pin.SLDPRT"),
        "bearing": os.path.join(output_dir, f"{base_name}_big_end_bearing.SLDPRT"),
        "rod_cap": os.path.join(output_dir, f"{base_name}_rod_cap.SLDPRT"),
    }

    radius = bore_diameter / 2
    # The rod is made on the front plane, which keeps its beam in the visible
    # silhouette below the piston rather than hiding it along the skirt axis.
    # The small end overlaps the lower center of the piston crown, and the big
    # end hangs one connecting-rod length below it.
    pin_center_y = -(radius * 0.45)
    crank_center_y = pin_center_y - connecting_rod_length
    small_end_outer = wrist_pin_diameter / 2 + 5
    big_end_outer = crank_bore_diameter / 2 + 5
    beam_half_width = max(4.0, wrist_pin_diameter * 0.18)
    front_offset = rod_width * 2.0

    async def _save_colored_part(path: str, rgb: tuple[int, int, int]) -> None:
        await set_appearance(*rgb, "body")
        await save_document(path)
        # Components are deliberately closed while the next part is built.
        # SolidWorks reloads them silently during assembly insertion, avoiding
        # a growing set of visible document windows on low-memory machines.
        await close_document(save=False)

    try:
        os.makedirs(output_dir, exist_ok=True)

        stage = "piston component"
        await create_automotive_piston(
            bore_diameter=bore_diameter,
            height=piston_height,
            wrist_pin_diameter=wrist_pin_diameter,
            unit=unit,
            save_path=part_paths["piston"],
        )
        await close_document(save=False)
        completed.append(stage)

        # The rod is built on the front plane: its small end joins the piston
        # silhouette and its big end is one rod length below it.
        stage = "connecting-rod component"
        await create_new_part()
        await create_sketch("front")
        await draw_circle(0, pin_center_y, small_end_outer, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_circle(0, crank_center_y, big_end_outer, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_rectangle(-beam_half_width, crank_center_y, beam_half_width, pin_center_y, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_circle(0, pin_center_y, wrist_pin_diameter / 2 + 0.5, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_circle(0, crank_center_y, crank_bore_diameter / 2, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        await _save_colored_part(part_paths["connecting_rod"], (65, 90, 125))
        completed.append(stage)

        stage = "wrist-pin component"
        await create_new_part()
        await create_sketch("front")
        await draw_circle(0, pin_center_y, wrist_pin_diameter / 2, unit)
        await close_sketch()
        await extrude_sketch(bore_diameter + 6, both_directions=True, unit=unit)
        await _save_colored_part(part_paths["wrist_pin"], (165, 175, 190))
        completed.append(stage)

        stage = "big-end bearing component"
        await create_new_part()
        await create_sketch("front")
        await draw_circle(0, crank_center_y, crank_bore_diameter / 2 - 0.8, unit)
        await draw_circle(0, crank_center_y, crank_bore_diameter / 2 - 3.3, unit)
        await close_sketch()
        await extrude_sketch(rod_width + 2, both_directions=True, unit=unit)
        await _save_colored_part(part_paths["bearing"], (185, 140, 55))
        completed.append(stage)

        # A separate outer collar represents the removable big-end rod cap.
        # It is offset to one side of the rod in the final assembly, making the
        # cap visually distinct instead of being hidden inside the rod body.
        stage = "rod-cap component"
        await create_new_part()
        await create_sketch("front")
        await draw_circle(0, crank_center_y, big_end_outer + 2.5, unit)
        await draw_circle(0, crank_center_y, crank_bore_diameter / 2 - 0.3, unit)
        await close_sketch()
        await extrude_sketch(4, both_directions=True, unit=unit)
        await _save_colored_part(part_paths["rod_cap"], (95, 100, 110))
        completed.append(stage)

        stage = "assembly insertion"
        assembly = await create_new_assembly()
        # insert_component reads each component's pose back out of SolidWorks
        # and returns actual_position/deviation/verified. Nothing here was
        # reading it: a component that did not land where it was placed left
        # the assembly looking plausible, and the five offsets below are the
        # only thing holding this assembly together -- there are no mates. So
        # every placement is checked, and the measured poses are reported.
        placements = [
            # The piston itself begins at the Front plane. The rod group is
            # shifted a controlled positive distance toward the viewing side of
            # that plane so its beam is visible below the skirt instead of
            # being occluded by it. The long wrist pin still spans this offset
            # and the piston body.
            ("piston", {}),
            ("connecting_rod", {"z": front_offset}),
            ("wrist_pin", {"z": front_offset}),
            ("bearing", {"z": front_offset}),
            ("rod_cap", {"x": rod_width + 4, "z": front_offset}),
        ]
        component_positions = []
        misplaced = []
        for key, offsets in placements:
            placed = await insert_component(part_paths[key], unit=unit, **offsets)
            component_positions.append({
                "part": key,
                "component": placed.get("name"),
                "requested_position": placed.get("requested_position"),
                "actual_position": placed.get("actual_position"),
                "deviation": placed.get("deviation"),
                "fixed": placed.get("fixed"),
                "verified": placed.get("verified"),
            })
            if placed.get("verified") is not True:
                misplaced.append(
                    f"{key} ({placed.get('name')}): {placed.get('deviation')}")
        if misplaced:
            raise RuntimeError(
                "components did not land where they were placed, and this assembly "
                "has no mates to pull them back: "
                + " | ".join(misplaced)
                + ". Do not build on this assembly -- check it with "
                "verify_assembly_positions and get_component_transform."
            )
        completed.append(stage)

        stage = "assembly save and view"
        await save_document(assembly_path)
        await set_view("isometric")
        await zoom_to_fit()
        await save_document()
        completed.append(stage)

        return {
            "assembly": assembly["title"],
            "save_path": assembly_path,
            "components": part_paths,
            "component_positions": component_positions,
            "position_check": {
                "method": ("every insert_component pose was read back out of "
                           "SolidWorks and compared with the requested offset"),
                "all_verified": True,
                "next_step": (
                    "verify_assembly_positions(require_constrained=False) for the "
                    "assembly-wide checks. Expect M01 on all five (this assembly is "
                    "deliberately unmated) and M03 on connecting_rod, wrist_pin and "
                    "bearing: those three are modelled in place and share the insert "
                    "origin on purpose, so sharing a position is correct here, not a "
                    "stack of parts in the wrong spot"),
            },
            "features": [
                "piston crown/skirt with three ring grooves",
                "hollow underside and wrist-pin bore",
                "wrist pin",
                "I-style connecting rod with small and big ends",
                "plain big-end bearing",
                "separate connecting-rod cap",
            ],
            "completed_stages": completed,
        }
    except Exception as exc:
        try:
            active = await get_document_info()
        except Exception:
            active = {"title": "(no active document)", "type": "Unknown"}
        raise RuntimeError(
            f"Automotive piston assembly creation stopped during '{stage}'. "
            f"Completed stages: {', '.join(completed) or '(none)'}. "
            f"Components saved in '{output_dir}'. Active document: {active}. "
            f"Root cause: {exc}"
        ) from exc


# Internal project recipe; intentionally excluded from the public MCP catalog.
async def create_automotive_piston_with_connecting_rod(
    bore_diameter: float = 86,
    piston_height: float = 75,
    connecting_rod_length: float = 135,
    wrist_pin_diameter: float = 22,
    crank_bore_diameter: float = 42,
    rod_width: float = 14,
    unit: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Create a clear single-part automotive piston and connecting-rod model.

    The tool produces the complete upright silhouette shown in engine reference
    drawings: a cylindrical piston with crown and three ring grooves at the
    top, a small-end pad below the skirt, a straight connecting-rod beam, and
    a circular big-end pad.  It is a
    *single-body conceptual/reference model* intended for visual design and
    demonstrations; use ``create_automotive_piston_assembly`` when separately
    editable piston, pin, bearing, cap, and rod component files are required.
    """

    if min(bore_diameter, piston_height, connecting_rod_length, wrist_pin_diameter, crank_bore_diameter, rod_width) <= 0:
        raise ValueError("All dimensions must be positive.")
    if wrist_pin_diameter >= bore_diameter or crank_bore_diameter >= bore_diameter:
        raise ValueError("Pin and crank-bore diameters must be smaller than bore_diameter.")
    if connecting_rod_length < piston_height:
        raise ValueError("connecting_rod_length must be at least piston_height.")

    completed: list[str] = []
    stage = "initialization"
    radius = bore_diameter / 2
    small_end_outer = wrist_pin_diameter / 2 + 5
    big_end_outer = crank_bore_diameter / 2 + 5
    beam_half_width = max(5.0, wrist_pin_diameter * 0.30)
    # The wrist-pin center belongs inside the lower third of the skirt, not
    # below it. Keeping the rod and the pin on this shared centerline makes
    # the joint physically continuous in every generated reference model.
    small_end_y = max(
        small_end_outer + 1.0,
        min(piston_height * 0.34, piston_height - small_end_outer - 8.0),
    )
    big_end_y = small_end_y - connecting_rod_length
    ring_offsets = (piston_height - 18, piston_height - 12, piston_height - 6)

    try:
        stage = "new part"
        part = await create_new_part()
        completed.append(stage)
        if save_path:
            stage = "initial save"
            await save_document(save_path)
            completed.append(stage)

        # The Top-plane extrusion makes the piston axis vertical in the final
        # model: crown/ring pack at the top and rod under the skirt.
        stage = "upright piston crown and skirt"
        await create_sketch("top")
        await draw_circle(0, 0, radius, unit)
        await close_sketch()
        await extrude_sketch(piston_height, unit=unit)
        completed.append(stage)

        # A real piston is open underneath: retain a controlled crown and
        # skirt wall, then let the rod occupy the internal pin-boss region.
        stage = "hollow piston underside"
        await shell_body(
            max(4.0, bore_diameter * 0.045),
            remove_face_at_x=0,
            remove_face_at_y=0,
            remove_face_at_z=0,
            unit=unit,
        )
        completed.append(stage)

        stage = "three ring grooves"
        for offset in ring_offsets:
            plane = await create_reference_plane("top", offset, unit=unit)
            await create_sketch(plane["plane"])
            await draw_circle(0, 0, radius + 1.5, unit)
            await draw_circle(0, 0, radius - 1.5, unit)
            await close_sketch()
            await cut_extrude(3, unit=unit)
        completed.append(stage)

        stage = "crown valve reliefs"
        # Start at the actual crown face. Starting below it would leave an
        # uncut cap, hiding the valve reliefs in the top view.
        crown_plane = await create_reference_plane("top", piston_height, unit=unit)
        await create_sketch(crown_plane["plane"])
        valve_offset = radius * 0.36
        await draw_circle(-valve_offset, 0, radius * 0.18, unit)
        await draw_circle(valve_offset, 0, radius * 0.18, unit)
        await close_sketch()
        await cut_extrude(2.4, unit=unit)
        completed.append(stage)

        # The rod lives on the front silhouette so it remains visible in the
        # isometric model view.  Each boss intersects the next feature and
        # SolidWorks merges them into one robust conceptual body.
        stage = "connecting rod silhouette"
        await create_sketch("front")
        await draw_circle(0, small_end_y, small_end_outer, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_circle(0, big_end_y, big_end_outer, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_rectangle(-beam_half_width, big_end_y, beam_half_width, small_end_y, unit)
        await close_sketch()
        await extrude_sketch(rod_width, both_directions=True, unit=unit)
        completed.append(stage)

        stage = "wrist-pin and crank bores"
        await create_sketch("front")
        await draw_circle(0, small_end_y, wrist_pin_diameter / 2 + 0.6, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        await create_sketch("front")
        await draw_circle(0, big_end_y, crank_bore_diameter / 2, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        completed.append(stage)

        stage = "separate wrist pin"
        await create_sketch("front")
        await draw_circle(0, small_end_y, wrist_pin_diameter / 2, unit)
        await close_sketch()
        await extrude_sketch(bore_diameter - 8, both_directions=True, merge=False, unit=unit)
        completed.append(stage)

        stage = "presentation cleanup"

        def _hide_reference_geometry():
            doc = _active_doc()
            doc.ClearSelection2(True)
            feature = doc.FirstFeature
            selected = 0
            while feature is not None:
                if feature.GetTypeName2 == "RefPlane":
                    feature.Select2(True, 0)
                    selected += 1
                feature = feature.GetNextFeature
            if selected:
                doc.BlankRefGeom()
            doc.ClearSelection2(True)
            return selected

        await _run(_hide_reference_geometry)
        await apply_metal_finish("chrome", "body")
        await set_view("isometric")
        await zoom_to_fit()
        completed.append(stage)

        if save_path:
            stage = "final save"
            await save_document()
            completed.append(stage)

        return {
            "document": part["title"],
            "save_path": os.path.abspath(save_path) if save_path else None,
            "model_type": "multibody automotive piston-and-rod reference",
            "features": [
                "piston skirt", "three ring grooves", "two crown valve reliefs",
                "hollow underside", "separate wrist pin", "small-end and big-end bores",
                "narrow-web connecting rod",
            ],
            # These are the centres this routine was BUILT AROUND -- arithmetic on
            # its own arguments, computed before any geometry existed. They used
            # to be published under a key called "contract", asserting that both
            # rod bores and the wrist pin really sit on them, which nothing here
            # ever checked. A reader downstream then mates to these numbers as if
            # they had been measured. They have not: if any stage drifted, the
            # mate is built on a false premise, which is exactly how an assembly
            # ends up with a crooked gap nobody can source.
            "joint_centers": {
                "wrist_pin": {"x": 0.0, "y": small_end_y, "z": 0.0},
                "crank_pin": {"x": 0.0, "y": big_end_y, "z": 0.0},
                "unit": unit or _server._default_unit,
                "measured": False,
                "design_intent": (
                    "The centres this routine aimed the small-end bore, big-end "
                    "bore and transverse wrist pin at. COMPUTED from the input "
                    "dimensions, never read back from the model. Confirm them "
                    "against the real geometry (list_faces for the bore axes, "
                    "measure_body for the overall box) before mating anything to "
                    "these coordinates."
                ),
            },
            "completed_stages": completed,
        }
    except Exception as exc:
        try:
            active = await get_document_info()
        except Exception:
            active = {"title": "(no active document)", "type": "Unknown"}
        raise RuntimeError(
            f"Integrated piston-and-rod creation stopped during '{stage}'. "
            f"Completed stages: {', '.join(completed) or '(none)'}. "
            f"Active document: {active}. Root cause: {exc}"
        ) from exc


# Internal project recipe; intentionally excluded from the public MCP catalog.
async def create_pedestal_fan_propeller(
    blade_radius: float = 190,
    hub_radius: float = 32,
    hub_depth: float = 28,
    blade_thickness: float = 4,
    pitch_angle: float = 8,
    unit: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Create a three-blade pedestal-fan propeller with aerodynamic pitch.

    The three broad, rounded-tip blades are copies of one seed body at exactly
    120-degree intervals around the hub. This makes their geometry, volume,
    and radial mass distribution identical. The seed is tilted by
    ``pitch_angle`` about its radial root axis before copying. The default is
    intentionally subtle so the rotor reads like a household-fan blade rather
    than three plates projected forward from the hub.
    Dimensions use ``unit`` (mm by default).
    """

    if min(blade_radius, hub_radius, hub_depth, blade_thickness) <= 0:
        raise ValueError("blade_radius, hub_radius, hub_depth, and blade_thickness must be positive.")
    if blade_radius <= hub_radius * 2:
        raise ValueError("blade_radius must be more than twice hub_radius.")
    if not 5 <= abs(pitch_angle) <= 45:
        raise ValueError("pitch_angle must be between 5 and 45 degrees in magnitude.")

    completed: list[str] = []
    stage = "initialization"

    async def _name_outermost_solid_body(name: str) -> str:
        def _impl():
            doc = _active_doc()
            bodies = doc.GetBodies2(0, False) or ()  # 0 = swSolidBody
            if not bodies:
                raise RuntimeError("No solid body was available to name.")
            # GetBodies2 has no creation-order guarantee. After the hub and
            # the separate blade are extruded, the fan blade is the body with
            # the greatest in-plane distance from the shaft axis; naming the
            # final item in this COM array can accidentally select the hub.
            def _radial_extent(raw_body) -> float:
                box = win32com.client.Dispatch(raw_body).GetBodyBox()
                if not box:
                    return -1.0
                return max(
                    math.hypot(x, y)
                    for x in (box[0], box[3])
                    for y in (box[1], box[4])
                )

            body = win32com.client.Dispatch(max(bodies, key=_radial_extent))
            if _radial_extent(body) <= to_meters(hub_radius * 1.1, unit):
                raise RuntimeError(
                    "Could not identify the new fan blade outside the hub radius."
                )
            body.Name = name
            return name

        return await _run(_impl)

    try:
        stage = "new part"
        part = await create_new_part()
        completed.append(stage)
        if save_path:
            stage = "initial save"
            await save_document(save_path)
            completed.append(stage)

        stage = "hub"
        await create_sketch("top")
        await draw_circle(0, 0, hub_radius, unit)
        await close_sketch()
        await extrude_sketch(hub_depth, both_directions=True, unit=unit)
        completed.append(stage)

        # A pedestal-fan blade is a broad, tapered airfoil-like paddle, not a
        # narrow propeller wedge.  The root arc stays inside the hub for a
        # strong visual mount; the large rounded outer arc produces the soft
        # blade tip seen on real household-fan rotors.
        #
        # The outer tip is approximated with short, equal line segments. This
        # gives the visual rounded profile of a fan blade while avoiding the
        # fragile open-contour condition that can occur when COM creates a
        # mixed line-and-arc profile. Copies of this one solid body guarantee
        # identical blade volume and mass distribution.
        stage = "balanced rounded pitched seed blade"
        root_center_x = hub_radius * 0.58
        root_center_y = hub_radius * 0.05
        root_radius = hub_radius * 0.46
        root_leading_angle = 265
        root_trailing_angle = 95
        tip_radius = blade_radius * 0.24
        tip_center_x = blade_radius - tip_radius
        tip_angle = 58

        def _point_on_arc(cx: float, cy: float, radius: float, angle: float) -> tuple[float, float]:
            radians = math.radians(angle)
            return (
                cx + radius * math.cos(radians),
                cy + radius * math.sin(radians),
            )

        root_leading = _point_on_arc(root_center_x, root_center_y, root_radius, root_leading_angle)
        root_trailing = _point_on_arc(root_center_x, root_center_y, root_radius, root_trailing_angle)
        tip_profile = [
            _point_on_arc(tip_center_x, 0, tip_radius, angle)
            for angle in (-tip_angle, -tip_angle / 2, 0, tip_angle / 2, tip_angle)
        ]
        root_inner = _point_on_arc(root_center_x, root_center_y, root_radius, 180)
        blade_points = [root_leading, *tip_profile, root_trailing, root_inner]
        await create_sketch("top")
        for index, start in enumerate(blade_points):
            end = blade_points[(index + 1) % len(blade_points)]
            await draw_line(*start, *end, unit=unit)
        await close_sketch()
        await extrude_sketch(blade_thickness, both_directions=True, merge=False, unit=unit)
        await _name_outermost_solid_body("FanBladeSeed")

        # The Top sketch plane is normal to the model Y axis, so Y is the
        # physical hub/shaft axis. Pitch the seed about its radial X direction,
        # then create two equal 120-degree increments about Y. This rotates the
        # complete pitched blade geometry into the remaining radial positions.
        await move_copy_body("FanBladeSeed", rx=pitch_angle, unit=unit)
        await move_copy_body("FanBladeSeed", ry=120, copy=True, num_copies=2, unit=unit)
        completed.append(stage)

        stage = "shaft bore"
        await create_sketch("top")
        await draw_circle(0, 0, hub_radius * 0.22, unit)
        await close_sketch()
        await cut_extrude(through_all=True, both_directions=True, unit=unit)
        completed.append(stage)

        stage = "appearance and view"

        def _hide_pattern_reference_axes():
            doc = _active_doc()
            doc.ClearSelection2(True)
            selected = 0
            feature = doc.FirstFeature
            while feature is not None:
                if feature.GetTypeName2 == "RefAxis":
                    feature.Select2(True, 0)
                    selected += 1
                feature = feature.GetNextFeature
            if selected:
                doc.BlankRefGeom()
            doc.ClearSelection2(True)
            return selected

        await _run(_hide_pattern_reference_axes)
        await apply_metal_finish("chrome", "body")
        await set_view("isometric")
        await zoom_to_fit()
        completed.append(stage)
        if save_path:
            stage = "final save"
            await save_document()
            completed.append(stage)

        return {
            "document": part["title"],
            "save_path": os.path.abspath(save_path) if save_path else None,
            "blade_count": 3,
            "aerodynamic_pitch_degrees": pitch_angle,
            "blade_spacing_degrees": [0, 120, 240],
            "rotational_balance": {
                "method": "one seed blade copied by 120 and 240 degrees",
                "mass_distribution": "three identical blades at equal radius",
                "balance_axis": "hub/shaft axis",
            },
            "features": [
                "central hub",
                "shaft bore",
                "three identical rounded-tip pitched fan blades",
            ],
            "completed_stages": completed,
        }
    except Exception as exc:
        try:
            active = await get_document_info()
        except Exception:
            active = {"title": "(no active document)", "type": "Unknown"}
        raise RuntimeError(
            f"Pedestal fan propeller creation stopped during '{stage}'. "
            f"Completed stages: {', '.join(completed) or '(none)'}. "
            f"Active document: {active}. Root cause: {exc}"
        ) from exc
