"""Live checks for the macro tools and the stale-COM-connection fix.

Deliberately never executes a vendor macro body: the .swp files that ship
with SolidWorks have unknown side effects (enable.swp toggles an OEM setting,
the benchmark macros open files). Coverage here is the inspection path, the
argument-resolution logic, and the failure paths, all of which exercise the
real COM calls without running third-party VBA.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server

import pythoncom

BENCHMARK_MACRO = r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldBenchmarking\Macro\testmacro.swp"
SIMULATION_MACRO = r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\Simulation\enable.swp"


class _DeadProxy:
    """A COM proxy whose SolidWorks process has gone away."""

    @property
    def RevisionNumber(self):
        raise pythoncom.com_error(-2147023174, "RPC server unavailable", None, None)


async def main():
    checks = []

    def check(name, condition, detail=""):
        checks.append(bool(condition))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    await server.connect_solidworks()

    # --- stale connection detection (the bug behind the RPC failures) ---
    live = await server._run(lambda: server._com_is_alive(server._connect()))
    check("_com_is_alive(live proxy) is True", live is True)
    check("_com_is_alive(dead proxy) is False", server._com_is_alive(_DeadProxy()) is False)

    # The old check was `_ = app.RevisionNumber`, which under the generated
    # typelib only builds a bound method and never reaches SolidWorks.
    check(
        "bare attribute access would NOT have caught the dead proxy",
        callable(_DeadProxy.__dict__["RevisionNumber"].fget) is True,
    )

    # --- list_macro_methods ---
    if os.path.isfile(BENCHMARK_MACRO):
        listed = await server.list_macro_methods(BENCHMARK_MACRO)
        check("list_macro_methods finds runnable entry points", listed["runnable_count"] > 0,
              f"({listed['runnable_count']} runnable)")
        check("runnable entries use Module.Procedure form",
              all("." in entry for entry in listed["runnable"]))
        check("needs_arguments is reported separately",
              len(listed["needs_arguments"]) > 0,
              f"({len(listed['needs_arguments'])} with args)")
        check("runnable and needs_arguments do not overlap",
              not (set(listed["runnable"]) & set(listed["needs_arguments"])))
    else:
        print("[SKIP] benchmark macro not present on this machine")

    if os.path.isfile(SIMULATION_MACRO):
        single = await server.list_macro_methods(SIMULATION_MACRO)
        check("single-entry macro reports exactly one runnable",
              single["runnable_count"] == 1, str(single["runnable"]))

    # --- failure paths ---
    try:
        await server.list_macro_methods(str(ROOT / "tests/output/__missing__.swp"))
        check("missing macro file raises", False)
    except FileNotFoundError:
        check("missing macro file raises FileNotFoundError", True)

    try:
        await server.run_macro(str(ROOT / "server.py"))
        check("non-macro extension rejected", False)
    except ValueError as exc:
        check("non-macro extension rejected", "not a SolidWorks macro" in str(exc))

    if os.path.isfile(BENCHMARK_MACRO):
        # Many runnable procedures -> must refuse to guess, and name the options.
        try:
            await server.run_macro(BENCHMARK_MACRO)
            check("ambiguous macro refuses to auto-pick", False)
        except ValueError as exc:
            check("ambiguous macro refuses to auto-pick and lists options",
                  "runnable entry points" in str(exc) and "CommonTests.Rebuild" in str(exc))

        # Real file, real COM round-trip, procedure that does not exist:
        # exercises RunMacro2 without executing any vendor VBA.
        try:
            await server.run_macro(BENCHMARK_MACRO, "NoSuchModule", "NoSuchProc")
            check("unknown procedure surfaces an error", False)
        except RuntimeError as exc:
            check("unknown procedure surfaces a clear error",
                  "refused to run" in str(exc))

    server._shutdown()
    passed = sum(checks)
    print("\n" + "=" * 70)
    print(f"{passed}/{len(checks)} checks passed")
    print("=" * 70)
    return 0 if passed == len(checks) else 1


sys.exit(asyncio.run(main()))
