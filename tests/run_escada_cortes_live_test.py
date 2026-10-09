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


if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "contact":
        asyncio.run(contact(sys.argv[2] if len(sys.argv) > 2 else OUT))



