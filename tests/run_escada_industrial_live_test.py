"""Live build of the industrial stair (parts stage). Resumable: parts already on disk are skipped.

Usage:  python tests/run_escada_industrial_live_test.py parts [part_key ...]
Needs a running SolidWorks. Output folder: C:\\Users\\pcrod\\Documents\\EscadaIndustrial
"""
import asyncio
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = r"C:\Users\pcrod\Documents\EscadaIndustrial"
RISE, RUN = 200.0, 220.0
HYP = math.hypot(RISE, RUN)          # 297.3
STRINGER_LEN = 4 * HYP               # 1189.2
ANGLE = math.degrees(math.atan2(RISE, RUN))  # 42.27 deg
RAIL_R = 33.7 / 2


def parts_table():
    P = {}
    P["Degrau"] = dict(kind="profile", file="Degrau_680x220x4.SLDPRT",
        outline=dict(shape="rectangle", x1=0, y1=-340, x2=220, y2=340), depth=4, plane="front",
        expect_bbox=(220, 680, 4))
    P["Console"] = dict(kind="profile", file="Console_triangular_100x92x8.SLDPRT",
        # XZ polygon (x, z) -> top-plane sketch (x, -z); extrudes along +Y (8 mm)
        outline=dict(shape="polygon", points=[[0, 0], [100, 0], [100, 50], [0, 92]]), depth=8, plane="top",
        expect_bbox=(100, 8, 92))
    P["ChapaPiso"] = dict(kind="profile", file="ChapaPiso_700x700x4.SLDPRT",
        outline=dict(shape="rectangle", x1=0, y1=-350, x2=700, y2=350), depth=4, plane="front",
        expect_bbox=(700, 700, 4))
    holes = [dict(shape="circle", cx=sx * 35, cy=sy * 35, diameter=12) for sx in (-1, 1) for sy in (-1, 1)]
    P["ChapaBase"] = dict(kind="profile", file="ChapaBase_100x100x8.SLDPRT",
        outline=dict(shape="rectangle", x1=-50, y1=-50, x2=50, y2=50), holes=holes, depth=8, plane="front",
        expect_bbox=(100, 100, 8))
    P["Longarina"] = dict(kind="tube", file="TuboFrameLongitudinal_60x40x3_L700.SLDPRT",
        section="rectangular", length=700, wall=3, width=40, height=60, axis="x", expect_bbox=(700, 60, 40))
    P["Travessa"] = dict(kind="tube", file="TuboFrameTransversal_60x40x3_L580.SLDPRT",
        section="rectangular", length=580, wall=3, width=60, height=40, axis="y", expect_bbox=(60, 580, 40))
    P["Perna"] = dict(kind="tube", file="Perna_60x60x3_L948.SLDPRT",
        section="square", length=948, wall=3, width=60, axis="z", expect_bbox=(60, 60, 948))
    P["Poste"] = dict(kind="tube", file="Poste_30x30x2_L950.SLDPRT",
        section="square", length=950, wall=2, width=30, axis="z", expect_bbox=(30, 30, 950))
    P["TrilhoLateral"] = dict(kind="tube", file="TuboRedondo_33.7x2.6_L700.SLDPRT",
        section="round", length=700, wall=2.6, outer_diameter=33.7, axis="x", expect_bbox=(700, 33.7, 33.7))
    P["TrilhoTraseiro"] = dict(kind="tube", file="TuboRedondo_33.7x2.6_L733.7.SLDPRT",
        section="round", length=2 * (350 + RAIL_R), wall=2.6, outer_diameter=33.7, axis="y",
        expect_bbox=(33.7, 733.7, 33.7))
    P["Banzo"] = dict(kind="tube", file="Banzo_80x40x3_L1189.SLDPRT",
        section="rectangular", length=STRINGER_LEN, wall=3, width=80, height=40, axis="x",
        tilt=dict(axis="y", angle=-ANGLE), expect_bbox=None)
    P["Corrimao"] = dict(kind="tube", file="CorrimaoInclinado_33.7x2.6_L1189.SLDPRT",
        section="round", length=STRINGER_LEN, wall=2.6, outer_diameter=33.7, axis="x",
        tilt=dict(axis="y", angle=-ANGLE), expect_bbox=None)
    return P


def bbox_size_mm(res):
    bb = res.get("bounding_box_mm") or {}
    s = bb.get("size") or {}
    return (s.get("x"), s.get("y"), s.get("z")), bb


async def stage_parts(keys):
    import server
    P = parts_table()
    os.makedirs(OUT, exist_ok=True)
    await server.connect_solidworks()
    log = []
    for key in keys or P:
        spec = P[key]
        path = os.path.join(OUT, spec["file"])
        if os.path.exists(path):
            print(f"[skip] {key}: exists", flush=True)
            continue
        if spec["kind"] == "profile":
            res = await server.create_profile_part(
                path, spec["outline"], spec["depth"], spec.get("holes"), plane=spec["plane"],
                rotate=spec.get("rotate"), unit="mm")
        else:
            kw = {k: spec[k] for k in ("section", "length", "wall", "width", "height", "outer_diameter", "axis", "tilt") if k in spec}
            res = await server.create_tube_part(save_path=path, unit="mm", **kw)
        size, bb = bbox_size_mm(res)
        exp = spec.get("expect_bbox")
        ok_box = exp is None or all(abs(a - b) < 0.05 for a, b in zip(sorted(size), sorted(exp)))
        print(f"[{'OK' if res['volume_check']['ok'] and ok_box else 'CHECK'}] {key}: size={size} "
              f"vol={res['volume_mm3']:.1f} ratio={res['volume_check']['ratio']:.5f} "
              f"min={bb.get('min_m')} max={bb.get('max_m')} nwarn={len(res['warnings'])}", flush=True)
        log.append(dict(key=key, file=path, **{k: res[k] for k in ("volume_mm3", "volume_check", "bounding_box_mm", "warnings")}))
    lp = Path(OUT, "_parts_log.jsonl")
    with lp.open("a", encoding="utf-8") as fh:
        for item in log:
            fh.write(json.dumps(item, default=str) + "\n")


# ---------------------------------------------------------------- assemblies
TH = math.radians(ANGLE)
UX, UZ = math.cos(TH), math.sin(TH)           # axis direction (x, z)
NX, NZ = -math.sin(TH), math.cos(TH)          # normal (up-left)
# Stringer: lower-end centre C0 so that the lower-bottom corner touches z=0 and the top-edge line is
# z = 108 + (200/220) x (passes 88 mm below every tread underside at the tread's front edge).
_T0z = 40 * NZ + (-40 * NZ) * 0 + 40 * NZ - 0   # placeholder, recomputed below
B0z_offset = -40 * NZ
C0z = 40 * NZ                                   # B0.z = C0z - 40*NZ = 0
T0z = C0z + 40 * NZ
T0x = (T0z - 108.0) / (RISE / RUN)
C0x = T0x - 40 * NX                             # T0 = C0 + 40 n
# upper-bottom corner must stay left of the leg face X=880
Bx_top = (C0x - 40 * NX) + STRINGER_LEN * UX
C0x -= max(0.0, Bx_top - 879.99)
STRINGER_C0 = (C0x, C0z)
# Handrail: upper end centre E (z=1900); forward-most point of its end disc touches plane X=880
Ex = 880.0 - RAIL_R * math.sin(TH)
Ez = 1900.0
HANDRAIL_E0 = (Ex - STRINGER_LEN * UX, Ez - STRINGER_LEN * UZ)
YR = 350 + RAIL_R                                # rail axis offset (outer face of posts)


def layout():
    """name -> (part key, [(x, y, z), ...]) for every sub-assembly (insertion point = part origin)."""
    L = {}
    L["SubBanzos"] = [("Banzo", [(STRINGER_C0[0], s * 360.0, STRINGER_C0[1]) for s in (1, -1)])]
    L["SubDegraus"] = [("Degrau", [(RUN * i, 0.0, RISE * (i + 1) - 4) for i in range(4)])]
    cons = []
    for i in range(4):
        cons += [(RUN * i, 332.0, RISE * (i + 1) - 4), (RUN * i, -340.0, RISE * (i + 1) - 4)]
    L["SubConsoles"] = [("Console", cons)]
    L["SubQuadroPlataforma"] = [("Longarina", [(880.0, 320.0, 976.0), (880.0, -320.0, 976.0)]),
                                ("Travessa", [(910.0, -290.0, 976.0), (1550.0, -290.0, 976.0)])]
    legs = [(x, y) for x in (910.0, 1550.0) for y in (320.0, -320.0)]
    L["SubPernas"] = [("Perna", [(x, y, 8.0) for x, y in legs])]
    L["SubChapasBase"] = [("ChapaBase", [(x, y, 0.0) for x, y in legs])]
    L["SubPostes"] = [("Poste", [(x, y, 1000.0) for x in (895.0, 1230.0, 1565.0) for y in (335.0, -335.0)])]
    L["SubTrilhos"] = [("TrilhoLateral", [(880.0, s * YR, z) for z in (1900.0, 1450.0) for s in (1, -1)]),
                       ("TrilhoTraseiro", [(1580.0 + RAIL_R, -YR, z) for z in (1900.0, 1450.0)])]
    L["SubCorrimaos"] = [("Corrimao", [(HANDRAIL_E0[0], s * YR, HANDRAIL_E0[1]) for s in (1, -1)])]
    return L


MAIN_DIRECT = [("ChapaPiso", [(880.0, 0.0, 996.0)])]


def _stat(res):
    comps = res.get("components") or []
    mv = max([c["moved"]["distance"] for c in comps if c.get("moved")] + [0.0])
    return mv


async def mate_component(server, assy_title, comp, x, y, z):
    """Pin one component with three plane mates against the host assembly's origin planes."""
    out = []
    for plane, coord in (("right", x), ("top", y), ("front", z)):
        e1 = {"component": comp, "plane": plane}
        e2 = {"plane": plane}
        if abs(coord) < 1e-6:
            r = await server.add_mate_by_name("coincident", e1, e2, unit="mm")
        else:
            r = await server.add_mate_by_name("distance", e1, e2, value=abs(coord), unit="mm")
        out.append((plane, r["mate_type"], r.get("flip"), round(_stat(r), 4)))
    return out


async def stage_sub(name, do_mates=True):
    import server
    L = layout()
    spec = L[name] if name in L else None
    if spec is None:
        raise SystemExit(f"unknown sub-assembly {name}")
    P = parts_table()
    path = os.path.join(OUT, name + ".SLDASM")
    if os.path.exists(path):
        print(f"[skip] {name} exists"); return
    await server.connect_solidworks()
    await server.create_new_assembly()
    placed = []
    for key, pts in spec:
        part = os.path.join(OUT, P[key]["file"])
        for (x, y, z) in pts:
            r = await server.insert_component(part, x, y, z, unit="mm")
            placed.append((r["name"], x, y, z, r.get("verified")))
            print(f"  inserted {r['name']} at {x:.2f},{y:.2f},{z:.2f} verified={r.get('verified')}", flush=True)
    await server.save_document(path)
    print(f"[saved] {path}", flush=True)
    if do_mates:
        for (cname, x, y, z, _v) in placed:
            res = await mate_component(server, name, cname, x, y, z)
            print(f"  mates {cname}: {res}", flush=True)
        await server.save_document(path)
        print(f"[saved+mated] {path}", flush=True)
    await server.close_document(False)

async def stage_main():
    import server
    P = parts_table()
    path = os.path.join(OUT, "EscadaIndustrial.SLDASM")
    if os.path.exists(path):
        print("[skip] main exists"); return
    await server.connect_solidworks()
    await server.create_new_assembly()
    await server.save_document(path)
    # first inserted component is auto-fixed by SolidWorks: make it the platform floor plate (a PART,
    # whose insert position is exact). Sub-assemblies are inserted floating at the origin and mated.
    r = await server.insert_component(os.path.join(OUT, P["ChapaPiso"]["file"]), 880, 0, 996, unit="mm")
    print(f"  inserted {r['name']} verified={r.get('verified')}", flush=True)
    await server.save_document()
    placed = []
    for name in layout():
        r = await server.insert_component(os.path.join(OUT, name + ".SLDASM"), 0, 0, 0, unit="mm")
        placed.append(r["name"])
        print(f"  inserted {r['name']}", flush=True)
    await server.save_document()
    print("[saved structure]", flush=True)
    for cname in placed:
        out = []
        for plane in ("right", "top", "front"):
            res = await server.add_mate_by_name("coincident", {"component": cname, "plane": plane}, {"plane": plane}, unit="mm", allow_move=True)
            out.append((plane, round(_stat(res), 3)))
        print(f"  mates {cname}: {out}", flush=True)
        await server.save_document()
    print("[saved+mated main]", flush=True)

if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "parts":
        asyncio.run(stage_parts(sys.argv[2:]))
    elif stage == "sub":
        asyncio.run(stage_sub(sys.argv[2], len(sys.argv) < 4 or sys.argv[3] != "nomates"))
    elif stage == "main":
        asyncio.run(stage_main())
