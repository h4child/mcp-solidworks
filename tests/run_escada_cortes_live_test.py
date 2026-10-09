"""Live check of the two end-cut joints of the industrial stair (stringer x leg, handrail x rail).

Stages:  contact <folder>   measures the FACE-contact area of both joints (boolean intersection of the
                            two real solids after nudging one into the other by EPS)
         rebuild            lengthens stringer + handrail, cuts both ends with cut_part_end (backup first!)
Needs a running SolidWorks. Folder: C:\\Users\\pcrod\\Documents\\EscadaIndustrial
"""
import asyncio
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import run_escada_industrial_live_test as R  # noqa: E402

OUT = R.OUT
EPS_MM = 0.05
TH = R.TH
S, C = math.sin(TH), math.cos(TH)
LEG_FACE_X = 880.0
STRINGER_NEW_LEN = 1265.0
HANDRAIL_NEW_LEN = R.STRINGER_LEN + 33.0
HANDRAIL_DZ = -R.RAIL_R * S * S / C          # puts the axis through (880, 1900)
PARTS = {"banzo": "Banzo_80x40x3_L1189.SLDPRT", "perna": "Perna_60x60x3_L948.SLDPRT",
         "corrimao": "CorrimaoInclinado_33.7x2.6_L1189.SLDPRT", "trilho": "TuboRedondo_33.7x2.6_L700.SLDPRT"}
# (part file, global position of the part origin in mm) of ONE instance of every member of each joint
JOINTS = {
    "banzo_perna": (("banzo", (R.STRINGER_C0[0], 360.0, R.STRINGER_C0[1])), ("perna", (910.0, 320.0, 8.0)), (1, 0, 0)),
    "corrimao_trilho": (("corrimao", (R.HANDRAIL_E0[0], R.YR, R.HANDRAIL_E0[1])), ("trilho", (880.0, R.YR, 1900.0)), (1, 0, 0)),
}


def _body_of(folder, key, win32com, server):
    """Open the part read-only-ish (no edits), return (doc title, a COPY of its single body)."""
    import shutil
    import tempfile
    # a copy under a unique name: SolidWorks refuses a second open document with the same title
    scratch = os.path.join(tempfile.gettempdir(), "escada_contact")
    os.makedirs(scratch, exist_ok=True)
    path = os.path.join(scratch, f"{os.getpid()}_{key}_{PARTS[key]}")
    shutil.copyfile(os.path.join(folder, PARTS[key]), path)
    app = server._connect()
    doc, errors, _w = server._open_doc6(app, path, 1)
    if doc is None:
        raise RuntimeError(f"cannot open {path} ({errors})")
    body = win32com.client.Dispatch(doc.GetBodies2(0, True)[0])
    return server._doc_title(doc), body.Copy()


def _translate(app, body, mm, win32com, pythoncom):
    # The dynamic SolidWorks 2025 proxy has no IMathUtility.CreateTransform: borrow a live IMathTransform
    # (the view orientation), overwrite its ArrayData with a pure translation and apply it to the body copy.
    tr = app.ActiveDoc.ActiveView.Orientation3
    data = [1, 0, 0, 0, 1, 0, 0, 0, 1, mm[0] / 1000.0, mm[1] / 1000.0, mm[2] / 1000.0, 1.0, 0, 0, 0]
    tr.ArrayData = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in data))
    if not body.ApplyTransform(tr):
        raise RuntimeError("ApplyTransform failed")


def _volume_mm3(body):
    props = body.GetMassProperties(1.0)
    return float(props[3]) * 1e9


async def contact(folder):
    import pythoncom
    import win32com.client
    import server
    await server.connect_solidworks()
    result = {}

    def _impl():
        app = server._connect()
        for name, ((ka, pa), (kb, pb), direction) in JOINTS.items():
            ta, a = _body_of(folder, ka, win32com, server)
            tb, b = _body_of(folder, kb, win32com, server)
            _translate(app, a, pa, win32com, pythoncom)
            _translate(app, b, pb, win32com, pythoncom)
            a2 = a.Copy()
            _translate(app, a2, [d * EPS_MM for d in direction], win32com, pythoncom)

            def inter(x, y):
                bodies, err = x.Operations2(15901, y.Copy(), 0)   # intersect (measured live: 15901 = common, 15902 = cut, 15903 = add)
                vols = [_volume_mm3(win32com.client.Dispatch(item)) for item in (bodies or ())]
                return sum(vols), vols, err

            plain_v, plain_list, plain_err = inter(a, b)            # exact contact: any real overlap?
            vol, vols, err = inter(a2, b)                          # A nudged EPS into B
            result[name] = {"overlap_volume_mm3": plain_v, "overlap_pieces": plain_list, "err_plain": plain_err,
                            "nudge_mm": EPS_MM, "volume_after_nudge_mm3": vol, "pieces": vols, "err": err,
                            "contact_area_mm2": vol / EPS_MM}
            for t in (ta, tb):
                with __import__("contextlib").suppress(Exception):
                    app.CloseDoc(t)
        return result

    out = await server._run(_impl)
    print(json.dumps(out, indent=1))
    return out


async def close_all(server):
    """Close every open document WITHOUT saving (the caller checked nothing is dirty)."""
    import win32com.client

    def _impl():
        app = server._connect()
        titles = [server._doc_title(win32com.client.Dispatch(d)) for d in (app.GetDocuments or ())]
        for t in titles:
            app.CloseDoc(t)
        return len(titles)

    return await server._run(_impl)


def expected_volumes():
    a_str = 80 * 40 - 74 * 34
    a_rail = math.pi / 4 * (33.7 ** 2 - 28.5 ** 2)
    l_str = (LEG_FACE_X - R.STRINGER_C0[0]) / C
    l_rail = (LEG_FACE_X - R.HANDRAIL_E0[0]) / C
    return {"banzo": a_str * l_str, "corrimao": a_rail * l_rail}


async def rebuild():
    """Lengthen stringer + handrail IN PLACE (edit the extrusion depth of the existing part) and cut both
    upper ends on x = 880 with cut_part_end. Editing the part in place keeps the assembly references
    resolved; re-creating the file from scratch made SolidWorks 2025 suppress the components (measured live)."""
    import server
    await server.connect_solidworks()
    exp = expected_volumes()
    report = {}
    print("closed docs:", await close_all(server), flush=True)
    jobs = [
        ("banzo", STRINGER_NEW_LEN, (R.STRINGER_C0[0], 360.0, R.STRINGER_C0[1]), 0.0),
        ("corrimao", HANDRAIL_NEW_LEN, (R.HANDRAIL_E0[0], R.YR, R.HANDRAIL_E0[1]), HANDRAIL_DZ),
    ]
    for key, new_len, origin, dz in jobs:
        path = os.path.join(OUT, PARTS[key])
        await server.open_document(path)

        def _lengthen():
            doc = server._active_doc()
            feat = doc.FirstFeature
            while feat is not None:
                if server._com_member(feat, "GetTypeName2") == "Extrusion":
                    break
                feat = feat.GetNextFeature
            param = doc.Parameter(f"D1@{feat.Name}")
            old = param.SystemValue * 1000.0
            param.SystemValue = new_len / 1000.0
            doc.EditRebuild3
            vol = server._measure_model_doc(doc, 1000.0)["volume_m3"] * 1e9
            return old, vol

        old_len, vol_blank = await server._run(_lengthen)
        print(f"[{key}] extrusion {old_len:.4f} -> {new_len:.4f} mm, blank volume {vol_blank:.1f} mm3", flush=True)
        if dz:
            body = await server._run(lambda: server._first_solid_body_name(server._active_doc()))
            mv = await server.move_copy_body(body, 0, 0, dz, unit="mm")
            print(f"[{key}] moved dz={dz:.4f}: center shift={mv.get('measured_center_shift')} verified={mv.get('verified')}", flush=True)
        cut = await server.cut_part_end([1, 0, 0], offset=LEG_FACE_X, origin=list(origin), unit="mm", save=True)
        ratio = cut["volume_after_mm3"] / exp[key]
        print(f"[{key}] cut: removed={cut['removed_mm3']:.1f} after={cut['volume_after_mm3']:.1f} expected={exp[key]:.1f} "
              f"ratio={ratio:.5f} end_on_plane={cut['end_on_plane']} face={cut['cut_face_area_mm2']} bodies={cut['bodies_after']} "
              f"warnings={cut['warnings']}", flush=True)
        report[key] = dict(cut, expected_after_mm3=exp[key], ratio=ratio)
        await server.close_document(False)
    Path(OUT, "_cortes_log.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


async def assembly():
    """Reopen the main assembly, check every reference, run interference_report, save, export the PNG."""
    import server
    await server.connect_solidworks()
    await server.open_document(os.path.join(OUT, "EscadaIndustrial.SLDASM"))
    await server.rebuild_model(True, False)
    await server.save_document()
    print("[saved assembly after rebuild]", flush=True)
    await server.connect_solidworks()
    import win32com.client

    def _impl():
        assy = server._active_assembly()
        out = []
        for raw in server._com_member(assy, "GetComponents", False) or ():
            c = win32com.client.Dispatch(raw)
            box = server._com_member(c, "GetBox", False, False)
            out.append(dict(name=str(c.Name2), suppressed=bool(server._com_member(c, "IsSuppressed")),
                            resolved=server._com_member(c, "GetModelDoc2") is not None,
                            box=[round(v * 1000, 3) for v in box] if box else None,
                            path=str(server._com_member(c, "GetPathName"))))
        return out

    comps = await server._run(_impl)
    print("components:", len(comps), "unresolved:", sum(not r["resolved"] for r in comps),
          "suppressed:", sum(r["suppressed"] for r in comps), flush=True)
    for r in comps:
        if "Banzo" in r["name"] or "Corrimao" in r["name"]:
            print("  ", r["name"], r["box"], flush=True)
    rep = await server.interference_report()
    Path(OUT, "_interference.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print("volumetric:", rep["volumetric_count"], "surface_contact:", rep["surface_contact_count"], flush=True)
    for p in rep["volumetric"]:
        print("  VOL", p, flush=True)
    for p in rep["surface_contact"]:
        if any(("Banzo" in n or "Corrimao" in n or "Perna" in n or "Trilho" in n) for n in p["components"]):
            print("  CONTACT", p["components"], flush=True)
    Path(OUT, "_verify_boxes.json").write_text(json.dumps(comps, indent=1), encoding="utf-8")
    v = await server.set_view_direction([-1, -1, 0.8], [0, 0, 1], True, os.path.join(OUT, "EscadaIndustrial_isometrica.png"))
    print("png:", v.get("image"), v.get("image_bytes"), flush=True)
    await server.save_document()
    print("[saved]", flush=True)


if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "rebuild":
        asyncio.run(rebuild())
    if stage == "assembly":
        asyncio.run(assembly())
    if stage == "contact":
        asyncio.run(contact(sys.argv[2] if len(sys.argv) > 2 else OUT))



