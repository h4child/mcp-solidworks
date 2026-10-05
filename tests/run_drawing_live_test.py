"""
Live validation of the drawing read/dimension/verify layer.

This is the test the unit suite cannot be: it drives a real SolidWorks
session and proves that the COM calls behind get_view_entities,
dimension_by_entity_ids and verify_drawing do what the API documentation
says. Everything in tests/test_alfa_drawing.py is geometry; everything here
is COM.

It reproduces the original complaint exactly: a 2400 mm long plate whose
flange-like short side is 150 mm. The old add_drawing_dimension would
happily dimension the 150 and report success. The assertion below is that
asking for the 2400 mm edge measures 2400, and that asking with
expected_mm=2400 on the WRONG edge is reported as a critical E01 instead of
passing silently.

Needs: SolidWorks open. Works in new, disposable documents and deletes its
own files at the end; it never touches anything already open.

Run with:  python tests/run_drawing_live_test.py
Not collected by pytest (see python_files in pyproject.toml).
"""

import asyncio
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.disable(logging.INFO)

import server  # noqa: E402

# The part: a 2400 x 150 mm plate, 10 mm thick, with a 20 mm hole.
# These are the numbers the assertions check against.
LENGTH_MM = 2400.0
HEIGHT_MM = 150.0
THICKNESS_MM = 10.0
HOLE_DIAMETER_MM = 20.0
HOLE_CENTER = (200.0, 75.0)

results = []

# SolidWorks cannot hold two documents with the same title, so a run that
# aborts and leaves "beam_plate.SLDPRT" open makes every later run fail at
# "Failed to insert front view" -- which looks like a geometry problem and is
# not. A per-run suffix keeps runs independent.
RUN_ID = f"{os.getpid()}"
PART_NAME = f"beam_plate_{RUN_ID}"


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))
    return bool(condition)


async def close_only_ours(pre_existing):
    """Close every document this run created, and nothing else.

    CloseAllDocuments would be simpler and would also throw away whatever the
    user had open, so the titles that were present before the run are
    excluded by name.
    """
    loop = asyncio.get_event_loop()
    app = await loop.run_in_executor(server._executor, server._connect)

    # Close in reverse dependency order. SolidWorks refuses to close a part
    # while an open drawing still references it, and the part is the first
    # thing list_open_documents reports -- so a naive loop retried the part
    # twelve times and closed nothing.
    close_order = {"Drawing": 0, "Assembly": 1, "Part": 2}

    for _attempt in range(12):
        docs = (await server.list_open_documents())["documents"]
        ours = [d for d in docs if d["title"] not in pre_existing]
        if not ours:
            break
        ours.sort(key=lambda d: close_order.get(d.get("type"), 9))
        title = ours[0]["title"]

        def close(title=title):
            # Two separate quirks to get past:
            #  - QuitDoc closes WITHOUT saving; CloseDoc silently refuses a
            #    document with unsaved changes, which is every document this
            #    test creates.
            #  - a DRAWING's GetTitle is "Name - SheetName" ("Desenho13 -
            #    Folha1"), which is what list_open_documents reports, but
            #    QuitDoc and CloseDoc want the document name alone.
            # Everything here runs in ONE executor call: the COM executor has
            # a single worker, so submitting to it from inside it deadlocks.
            candidates = [title]
            if " - " in title:
                candidates.append(title.rsplit(" - ", 1)[0])
            for name in candidates:
                for method in ("QuitDoc", "CloseDoc"):
                    try:
                        getattr(app, method)(name)
                    except Exception:
                        continue
            return True

        await loop.run_in_executor(server._executor, close)

    remaining = [d["title"] for d in (await server.list_open_documents())["documents"]
                 if d["title"] not in pre_existing]
    if remaining:
        print(f"      WARNING: could not close {remaining}; close them by hand "
              f"or the next run will collide on the duplicate title")
    else:
        print("      closed every document this run created")


async def build_part(path):
    """A plate with a hole, in one sketch, saved to disk.

    A drawing view needs the model on disk, so this saves before returning.
    """
    await server.create_new_part()
    await server.create_sketch("front")
    await server.draw_rectangle(0, 0, LENGTH_MM, HEIGHT_MM)
    await server.draw_circle(HOLE_CENTER[0], HOLE_CENTER[1], HOLE_DIAMETER_MM / 2)
    await server.close_sketch()
    await server.extrude_sketch(THICKNESS_MM)
    measured = await server.measure_body()
    await server.save_document(path)
    return measured


async def main():
    print("=" * 72)
    print("LIVE DRAWING VALIDATION -- read, dimension by entity, verify")
    print("=" * 72)
    await server.connect_solidworks()

    workdir = tempfile.mkdtemp(prefix="mcp_draw_live_")
    part_path = os.path.join(workdir, f"{PART_NAME}.SLDPRT")

    # Anything already open belongs to the user; this run must not close it.
    pre_existing = {d["title"] for d in
                    (await server.list_open_documents())["documents"]}
    if pre_existing:
        print(f"\nnote: {len(pre_existing)} document(s) already open; they will "
              f"be left alone")

    print(f"\nworking in {workdir}")

    # ---------------------------------------------------------------- part
    print("\n[1] build the 2400 x 150 plate with a 20 mm hole")
    measured = await build_part(part_path)
    box = (measured.get("bounding_box") or {}).get("size", {})
    check("part bounding box is 2400 x 150 x 10",
          abs(box.get("x", 0) - LENGTH_MM) < 1 and abs(box.get("y", 0) - HEIGHT_MM) < 1,
          f"got x={box.get('x')} y={box.get('y')} z={box.get('z')}")
    check("measure_body reports a body count (B03)",
          measured.get("body_count") is not None,
          f"body_count={measured.get('body_count')}")
    if measured.get("density_warning"):
        print(f"         note: {measured['density_warning'][:90]}...")

    # ------------------------------------------------------------- drawing
    print("\n[2] create the drawing and insert a view")
    await server.create_new_drawing()
    # 1:1 cannot fit 2400 mm on any sheet: the tool must say so rather than
    # echo the scale it was given.
    view = await server.insert_drawing_view(part_path, "front", x=150, y=150, scale=1.0)
    check("insert_drawing_view returns the view name (B05)",
          bool(view.get("view_name")), f"view_name={view.get('view_name')!r}")
    check("insert_drawing_view reports the EFFECTIVE scale (B05)",
          view.get("effective_scale") is not None,
          f"requested={view.get('requested_scale')} effective={view.get('effective_scale')}")
    check("insert_drawing_view returns a real outline (B05)",
          bool(view.get("outline_mm")), f"outline_mm={view.get('outline_mm')}")
    codes = [i["code"] for i in view.get("issues", [])]
    # SolidWorks refuses a view scale it cannot honour and falls back to the
    # SHEET scale, so the view ends up fitting after all. What must not
    # happen is the old behaviour: swallowing the refusal inside
    # try/except: pass and reporting the requested scale anyway. E08 (scale
    # refused or not normalised) and E07 (does not fit) are both correct
    # answers here; silence is not.
    requested = view.get("requested_scale")
    effective = view.get("effective_scale")
    check("a refused view scale is reported, not swallowed (B05)",
          bool(codes) or effective == f"{requested:g}:1",
          f"requested={requested} effective={effective} issues={codes}")
    check("the result says whether the scale was actually applied",
          view.get("scale_applied") is not None,
          f"scale_applied={view.get('scale_applied')}")

    # Put it at a scale that fits, so the rest of the test works on a sane sheet.
    print("\n[3] re-insert at a scale that fits")
    await server.create_new_drawing()
    view = await server.insert_drawing_view(part_path, "front", x=200, y=150, scale=0.1)
    view_name = view.get("view_name")
    print(f"      view={view_name!r} scale={view.get('effective_scale')} "
          f"size={view.get('size_mm')}")

    # ------------------------------------------------------------- layout
    print("\n[4] get_drawing_layout")
    layout = await server.get_drawing_layout()
    sheet = layout["sheets"][0]
    check("the sheet format is identified (ISO or ANSI)",
          sheet.get("format") is not None,
          f"format={sheet.get('format')} size={sheet.get('size_mm')}")
    check("the usable area is reported", bool(sheet.get("usable_area_mm")),
          f"usable={sheet.get('usable_area_mm')}")
    check("the view is listed with an outline",
          any(v.get("outline_mm") for v in sheet["views"]),
          f"{len(sheet['views'])} view(s)")

    # ----------------------------------------------------------- entities
    print("\n[5] get_view_entities -- the read that removes the guessing")
    ents = await server.get_view_entities(view_name=view_name)
    entities = ents["entities"]
    print(f"      {ents['matched']} entities: {ents['counts_by_kind']}")

    lengths = sorted({e.get("length_model_mm") for e in entities
                      if e.get("length_model_mm")}, reverse=True)
    check("the 2400 mm edge is found with its MODEL length, not its sheet length",
          any(abs((l or 0) - LENGTH_MM) < 1 for l in lengths),
          f"longest lengths found: {lengths[:4]}")
    check("the 150 mm edge is also found",
          any(abs((l or 0) - HEIGHT_MM) < 1 for l in lengths),
          f"lengths: {lengths[:6]}")

    circles = [e for e in entities if e["kind"] == "circle"]
    check("the hole is found as a CIRCLE, not a degenerate chord (B04)",
          bool(circles),
          f"{len(circles)} circle(s), diameters="
          f"{[c.get('diameter_model_mm') for c in circles]}")
    if circles:
        check("the hole's diameter is 20 mm",
              any(abs(c.get("diameter_model_mm", 0) - HOLE_DIAMETER_MM) < 0.5
                  for c in circles))

    long_edge = next((e for e in entities
                      if abs((e.get("length_model_mm") or 0) - LENGTH_MM) < 1), None)
    short_edge = next((e for e in entities
                       if abs((e.get("length_model_mm") or 0) - HEIGHT_MM) < 1), None)

    # ---------------------------------------------------- the actual bug
    print("\n[6] dimension the 2400 mm edge, asserting the value (THE bug)")
    if long_edge is None:
        check("the long edge was available to dimension", False,
              "cannot run the central assertion")
    else:
        dim = await server.dimension_by_entity_ids(
            entity_ids=[long_edge["id"]], expected_mm=LENGTH_MM)
        print(f"      measured {dim.get('value')} {dim.get('unit')} "
              f"at {dim.get('text_position_mm')}, side={dim.get('side')}")
        check("the dimension measures 2400, not 150",
              abs((dim.get("value") or 0) - LENGTH_MM) < 1,
              f"value={dim.get('value')}")
        check("no critical issue is raised for a correct dimension",
              not [i for i in dim.get("issues", []) if i["severity"] == "critical"],
              f"issues={[i['code'] for i in dim.get('issues', [])]}")
        check("the text is NOT at the sheet origin (E05)",
              dim.get("text_position_mm") not in ([0, 0], [0.0, 0.0]),
              f"text at {dim.get('text_position_mm')}")

    print("\n[7] the assertion must FAIL LOUDLY on the wrong edge")
    if short_edge is None:
        check("the short edge was available", False)
    else:
        dim_wrong = await server.dimension_by_entity_ids(
            entity_ids=[short_edge["id"]], expected_mm=LENGTH_MM)
        wrong_codes = [i["code"] for i in dim_wrong.get("issues", [])]
        check("dimensioning the 150 mm edge while expecting 2400 reports E01",
              "E01" in wrong_codes,
              f"value={dim_wrong.get('value')} issues={wrong_codes}")
        check("ok is False when the value does not match",
              dim_wrong.get("ok") is False, f"ok={dim_wrong.get('ok')}")

    print("\n[8] dimension the hole")
    if circles:
        dim_hole = await server.dimension_by_entity_ids(
            entity_ids=[circles[0]["id"]], expected_mm=HOLE_DIAMETER_MM,
            tolerance_mm=0.5)
        print(f"      measured {dim_hole.get('value')} {dim_hole.get('unit')}")
        check("the hole dimension reads 20 mm (diameter)",
              abs((dim_hole.get("value") or 0) - HOLE_DIAMETER_MM) < 0.5
              or abs((dim_hole.get("value") or 0) - HOLE_DIAMETER_MM / 2) < 0.5,
              f"value={dim_hole.get('value')} -- 20 if diameter, 10 if radius")

    print("\n[9] get_view_dimensions reads back what was created")
    dims = await server.get_view_dimensions(view_name=view_name)
    print(f"      {dims['count']} dimension(s)")
    check("the created dimensions are readable",
          dims["count"] > 0,
          f"values={[d.get('value_mm') for d in dims['dimensions']]}")
    check("none of them is dangling",
          not [d for d in dims["dimensions"] if d.get("dangling")],
          f"dangling={[d['id'] for d in dims['dimensions'] if d.get('dangling')]}")
    # The attachment read is what the coverage check depends on. It silently
    # returned nothing because the COM identity was being used as a dict key
    # and PyIUnknown is unhashable -- so verify_drawing flagged edges that
    # were plainly dimensioned.
    check("no dimension reports a read error",
          not [d for d in dims["dimensions"] if d.get("read_error")],
          f"errors={[d.get('read_error') for d in dims['dimensions'] if d.get('read_error')]}")
    check("each dimension resolves to the entity it is attached to",
          all(d.get("attached_count") for d in dims["dimensions"])
          and not any("unregistered" in (d.get("attached") or [])
                      for d in dims["dimensions"]),
          f"attached={[d.get('attached') for d in dims['dimensions']]}")
    dimensioned_ids = {ref for d in dims["dimensions"]
                       for ref in (d.get("attached") or []) if ref}

    print("\n[10] verify_drawing")
    report = await server.verify_drawing()
    print(f"      status={report['status']}  critical={report['critical']}  "
          f"warnings={report['warnings']}")
    for issue in report["issues"][:8]:
        print(f"        {issue['code']} {issue['severity']}: {issue['message'][:80]}")
    check("verify_drawing returns a structured report",
          "status" in report and isinstance(report.get("issues"), list))
    # The regression this pins: verify_drawing reported "no dimension" for
    # every edge, including the three just dimensioned, because the
    # dimension-to-entity mapping came back empty.
    missing_refs = {ref for i in report["issues"] if i["code"] == "E02"
                    for ref in i["refs"]}
    wrongly_flagged = dimensioned_ids & missing_refs
    check("an entity that HAS a dimension is not reported as missing one",
          not wrongly_flagged,
          f"wrongly flagged: {sorted(wrongly_flagged)}"
          if wrongly_flagged else f"{len(dimensioned_ids)} dimensioned, "
          f"{len(missing_refs)} flagged, no overlap")
    check("verify_drawing inspected the view",
          any(v["view"] == view_name for v in report["views_checked"]),
          f"views_checked={report['views_checked']}")

    # --------------------------------------------------- facade still works
    print("\n[11] add_drawing_dimension (the point-based facade) still works")
    if long_edge:
        p1, p2 = long_edge["p1_sheet_mm"], long_edge["p2_sheet_mm"]
        mid = [(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2]
        try:
            facade = await server.add_drawing_dimension(
                x1=mid[0], y1=mid[1], expected_mm=LENGTH_MM)
            check("the facade resolves a sheet point to the right entity",
                  abs((facade.get("value") or 0) - LENGTH_MM) < 1,
                  f"value={facade.get('value')} entity={facade.get('entity_ids')}")
        except Exception as exc:
            check("the facade resolves a sheet point to the right entity", False,
                  f"raised: {exc}")

    print("\n[12] DWG export (B09)")
    dwg_path = os.path.join(workdir, "beam_plate.dwg")
    try:
        out = await server.export_document(dwg_path)
        check("a drawing exports to DWG",
              os.path.isfile(dwg_path),
              f"{out.get('size_bytes')} bytes, {out.get('sheets')} sheet(s)")
    except Exception as exc:
        check("a drawing exports to DWG", False, f"raised: {exc}")

    # ------------------------------------------------------------- cleanup
    print("\n[13] cleanup")
    await close_only_ours(pre_existing)
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
    print(f"      removed {workdir}")

    # -------------------------------------------------------------- report
    print("\n" + "=" * 72)
    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [n for n, ok, _d in results if not ok]
    print(f"RESULT: {passed}/{len(results)} checks passed")
    if failed:
        print("\nFAILED:")
        for name in failed:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
