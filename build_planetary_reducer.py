import asyncio
import json
import math
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parent / "tests" / "output" / "redutor_planetario_2_estagios"
PARTS = ROOT / "parts"
SUBS = ROOT / "subassemblies"
REPORT = ROOT / "relatorio_verificacao.json"


def p(name: str) -> str:
    return str(PARTS / name)


async def make_profile(name, outline, depth, holes=None, plane="front", both=True, material="AISI 1020"):
    return await server.create_profile_part(
        save_path=p(name), outline=outline, depth=depth, holes=holes or [],
        plane=plane, both_directions=both, material=material,
        close_after_save=True, unit="mm",
    )


async def make_gear(name, module, teeth, width, bore, phase=0.0):
    await server.create_new_part()
    result = await server.create_spur_gear(
        module=module, teeth=teeth, thickness=width, pressure_angle=20.0,
        bore_diameter=bore, plane="front", unit="mm", phase=phase,
        volume_tolerance=0.03,
    )
    await server.set_custom_property("Description", f"Engrenagem involuta real - {teeth} dentes")
    await server.set_custom_property("Module_mm", str(module))
    await server.set_custom_property("Teeth", str(teeth))
    await server.set_custom_property("PressureAngle_deg", "20")
    await server.set_custom_property("FaceWidth_mm", str(width))
    await server.set_custom_property("PitchDiameter_mm", str(module * teeth))
    await server.set_custom_property("GeometryVerification", str(result.get("verified")))
    saved = await server.save_document(p(name))
    await server.close_document(False)
    return result | {"path": saved["path"], "name": name}


async def make_shaft(name, radius, length, bore=0, material="AISI 1045"):
    holes = [{"shape": "circle", "cx": 0, "cy": 0, "radius": bore / 2}] if bore else []
    return await make_profile(
        name,
        {"shape": "circle", "cx": 0, "cy": 0, "radius": radius},
        length, holes, both=True, material=material,
    )


async def build_part_library():
    PARTS.mkdir(parents=True, exist_ok=True)
    SUBS.mkdir(parents=True, exist_ok=True)
    results = {"parts": [], "gears": [], "limitations": []}

    # Housing: thick annular body with bearing seats represented by through bores.
    results["parts"].append(await make_profile(
        "carcaca_principal.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 105}, 80,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 72}], material="AISI 1020",
    ))
    results["parts"].append(await make_profile(
        "tampa_aparafusada.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 105}, 10,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 20}] + [
            {"shape": "circle", "cx": 85 * math.cos(math.radians(a)),
             "cy": 85 * math.sin(math.radians(a)), "radius": 4}
            for a in range(0, 360, 60)
        ], material="AISI 1020",
    ))

    # Shafts, carriers, spacers, bearings, seals and fasteners.
    for spec in [
        ("eixo_entrada.SLDPRT", 15, 130, 0),
        ("eixo_saida.SLDPRT", 20, 150, 0),
        ("pino_planeta.SLDPRT", 8, 32, 0),
        ("espacador_planeta.SLDPRT", 18, 12, 8),
        ("rolamento_6208.SLDPRT", 36, 18, 20),
        ("retentor_entrada.SLDPRT", 32, 8, 15),
        ("retentor_saida.SLDPRT", 38, 8, 20),
        ("parafuso_M8.SLDPRT", 6, 35, 0),
    ]:
        results["parts"].append(await make_shaft(*spec))

    # Stage 1: sun 24 / planet 30 / theoretical internal ring 84 teeth.
    g1sun = await make_gear("solar_estagio1.SLDPRT", 3.0, 24, 14, 30)
    g1planet = await make_gear("planeta_estagio1.SLDPRT", 3.0, 30, 14, 16)
    g1carrier = await make_profile(
        "porta_satelites_estagio1.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 112}, 12,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 18}] + [
            {"shape": "circle", "cx": 81 * math.cos(math.radians(a)),
             "cy": 81 * math.sin(math.radians(a)), "radius": 8}
            for a in (0, 120, 240)
        ], material="AISI 1045",
    )
    crown1 = await make_profile(
        "coroa_interna_estagio1.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 145}, 14,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 126}], material="AISI 1020",
    )
    results["gears"] += [g1sun, g1planet]

    # Stage 2: sun 20 / planet 28 / theoretical internal ring 76 teeth.
    g2sun = await make_gear("solar_estagio2.SLDPRT", 2.5, 20, 12, 28)
    g2planet = await make_gear("planeta_estagio2.SLDPRT", 2.5, 28, 12, 14)
    g2carrier = await make_profile(
        "porta_satelites_estagio2.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 92}, 10,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 16}] + [
            {"shape": "circle", "cx": 60 * math.cos(math.radians(a)),
             "cy": 60 * math.sin(math.radians(a)), "radius": 7}
            for a in (0, 120, 240)
        ], material="AISI 1045",
    )
    crown2 = await make_profile(
        "coroa_interna_estagio2.SLDPRT",
        {"shape": "circle", "cx": 0, "cy": 0, "radius": 112}, 12,
        [{"shape": "circle", "cx": 0, "cy": 0, "radius": 94}], material="AISI 1020",
    )
    results["gears"] += [g2sun, g2planet]

    # The current MCP supports verified external involute gears, but not
    # internal involute gear generation. These crown files are retained as
    # annular structural placeholders and are explicitly not called gears.
    results["limitations"].append(
        "O MCP v5.21.0 não expõe gerador de engrenagem interna involuta; as duas coroas foram criadas como anéis estruturais e permanecem NÃO VERIFICADAS como engrenagens."
    )
    results["parts"].extend([g1carrier, crown1, g2carrier, crown2])
    return results


async def insert(path, x=0, y=0, z=0):
    return await server.insert_component(path, x=x, y=y, z=z, unit="mm", tolerance=0.2)


async def make_stage_subassembly(name, entries):
    await server.create_new_assembly()
    inserted = []
    for path, x, y, z in entries:
        inserted.append(await insert(path, x, y, z))
    saved = await server.save_document(str(SUBS / name))
    await server.close_document(False)
    return {"path": saved["path"], "components": inserted}


async def build_main(stage1, stage2):
    await server.create_new_assembly()
    comps = []
    comps.append(await insert(p("carcaca_principal.SLDPRT"), 0, 0, 0))
    housing_name = comps[0]["name"]
    await server.fix_component(housing_name)
    comps.append(await insert(stage1["path"], 0, 0, 35))
    comps.append(await insert(stage2["path"], 0, 0, 65))
    comps.append(await insert(p("eixo_entrada.SLDPRT"), 0, 0, -55))
    comps.append(await insert(p("eixo_saida.SLDPRT"), 0, 0, 70))
    comps.append(await insert(p("tampa_aparafusada.SLDPRT"), 0, 0, 85))
    for a in range(0, 360, 60):
        comps.append(await insert(p("parafuso_M8.SLDPRT"),
                                  85 * math.cos(math.radians(a)),
                                  85 * math.sin(math.radians(a)), 85))

    # Explicit mathematical transmission relations (reported, not inferred).
    rel1 = {"sun_teeth": 24, "planet_teeth": 30, "ring_teeth": 84,
            "carrier_fixed_relation": "ring fixed; omega_c = omega_s * Zs/(Zs+Zr)",
            "ratio": (24 + 84) / 24}
    rel2 = {"sun_teeth": 20, "planet_teeth": 28, "ring_teeth": 76,
            "carrier_fixed_relation": "ring fixed; omega_c = omega_s * Zs/(Zs+Zr)",
            "ratio": (20 + 76) / 20}
    math_check = {"stage1": rel1, "stage2": rel2,
                  "total_reduction": rel1["ratio"] * rel2["ratio"],
                  "planet_spacing_deg": [0, 120, 240],
                  "stage1_center_distance_mm": 3 * (24 + 30) / 2,
                  "stage2_center_distance_mm": 2.5 * (20 + 28) / 2,
                  "stage1_ring_compatibility": 24 + 2 * 30 == 84,
                  "stage2_ring_compatibility": 20 + 2 * 28 == 76}

    # Native gear mates are attempted only on the actual visible top faces.
    # If SolidWorks cannot resolve a face, the error is preserved in report.
    mate_results = []
    try:
        mate_results.append(await server.add_advanced_mate(
            "gear", 30, 0, 42, 81 + 30, 0, 42,
            gear_ratio_numerator=24, gear_ratio_denominator=30,
            reverse=True, unit="mm"))
    except Exception as exc:
        mate_results.append({"status": "not_created", "reason": str(exc)})

    pose_path = str(ROOT / "pose_before_motion.json")
    pose = await server.capture_assembly_pose(pose_path, unit="mm")
    motion = None
    try:
        motion = await server.create_motion_study("Planetary_2Stages_Test", activate=True, unit="mm")
    except Exception as exc:
        motion = {"status": "not_created", "reason": str(exc)}
    try:
        await server.restore_assembly_pose(pose_path, unit="mm")
    except Exception:
        pass

    interference = await server.interference_report()
    bom = await server.extract_assembly_bom()
    positions = await server.verify_assembly_positions(
        unit="mm", require_constrained=False, top_level_only=True,
        moving_components=[c["name"] for c in comps[3:5]],
    )
    exploded = None
    try:
        exploded = await server.create_exploded_view(140, "z", "mm")
    except Exception as exc:
        exploded = {"status": "not_created", "reason": str(exc)}

    section_png = str(ROOT / "vista_corte_isometrica.png")
    view_png = str(ROOT / "vista_isometrica.png")
    try:
        await server.set_view_direction([-1, -1, 0.8], [0, 0, 1], True, view_png)
    except Exception as exc:
        view_png = f"not_created: {exc}"
    saved = await server.save_document(str(ROOT / "redutor_planetario_2_estagios.SLDASM"))
    report = {
        "status": "created_with_explicit_limitations",
        "assembly": saved,
        "components": comps,
        "mathematical_verification": math_check,
        "gear_mates": mate_results,
        "motion_study": motion,
        "pose": pose,
        "positions": positions,
        "interference": interference,
        "bom": bom,
        "exploded_view": exploded,
        "views": {"isometric": view_png, "section": section_png,
                  "section_status": "not_created: MCP does not expose section-view creation for assemblies"},
        "notes": [
            "Engrenagens solares e planetárias foram geradas por create_spur_gear e retornaram medição de dentes/volume.",
            "A simulação criada é um Motion Study container; o MCP não cria motor/timeline automaticamente.",
            "Interferência foi consultada no estado efetivamente salvo.",
        ],
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return report


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    await server.connect_solidworks()
    library = await build_part_library()
    stage1 = await make_stage_subassembly(
        "submontagem_estagio1.SLDASM",
        [(p("coroa_interna_estagio1.SLDPRT"), 0, 0, 0),
         (p("solar_estagio1.SLDPRT"), 0, 0, 0),
         (p("porta_satelites_estagio1.SLDPRT"), 0, 0, -18)] +
        [(p("planeta_estagio1.SLDPRT"), 81 * math.cos(math.radians(a)),
          81 * math.sin(math.radians(a)), 0) for a in (0, 120, 240)])
    stage2 = await make_stage_subassembly(
        "submontagem_estagio2.SLDASM",
        [(p("coroa_interna_estagio2.SLDPRT"), 0, 0, 0),
         (p("solar_estagio2.SLDPRT"), 0, 0, 0),
         (p("porta_satelites_estagio2.SLDPRT"), 0, 0, -16)] +
        [(p("planeta_estagio2.SLDPRT"), 60 * math.cos(math.radians(a)),
          60 * math.sin(math.radians(a)), 0) for a in (0, 120, 240)])
    report = await build_main(stage1, stage2)
    report["library"] = library
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "assembly": report["assembly"],
                      "interference": report["interference"],
                      "bom_rows": len(report.get("bom", {}).get("bom", []))},
                     ensure_ascii=False, default=str))
    server._shutdown()


if __name__ == "__main__":
    asyncio.run(main())
