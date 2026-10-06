"""
SolidWorks MCP Server
----------------------
Exposes SolidWorks automation (parts, sketches, features) as MCP tools,
driving the running SolidWorks instance through its COM API.

All COM calls run on a single dedicated worker thread (COM/STA requirement):
the SolidWorks Application object and every document/feature object it hands
out must be used from the thread that first connected to it.
"""

import os
import io
import re
import json
import math
import time
import atexit
import base64
import asyncio
import shutil
import zipfile
import builtins
import logging
import tempfile
import functools
import contextlib
import contextvars
import concurrent.futures
from typing import Optional

import win32com.client
import pythoncom

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.types import ToolAnnotations
from pydantic import ConfigDict

# Drawing geometry, selection policy and verification rules. Kept in a
# COM-free module so the whole of it is testable under pytest without
# SolidWorks open -- see tests/test_alfa_drawing.py.
import alfa_drawing as ad

# Reject unknown tool arguments instead of silently ignoring them. Without
# this, FastMCP's default Pydantic config (extra="ignore") means a caller
# that misspells a parameter name (e.g. "x_center" instead of "cx") gets no
# error at all -- the field just falls back to its default and the tool
# quietly does the wrong thing (a mispositioned arc/polygon, not a failure).
ArgModelBase.model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solidworks-mcp")

# ---------------------------------------------------------------------------
# Structured call log (one JSON line per tool call)
# ---------------------------------------------------------------------------
# The log above is for a human watching the console. It is useless for
# reconstructing what an agent actually called after the fact -- every tool's
# COM work runs through a locally-defined `_impl` closure, so the existing
# "COM call: %s" debug line always prints the same literal name ("_impl"),
# never the tool's. This is a second, independent, append-only log: one JSON
# object per call (tool, a truncated view of its arguments, duration, and
# whether it raised), written by _log_tool_call via the single place every
# call already passes through regardless of which of the 150 tools it is --
# ToolManager.call_tool (patched onto `mcp` once below) -- rather than by
# touching each tool function.
CALL_LOG_PATH = os.environ.get(
    "SOLIDWORKS_MCP_CALL_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl"),
)
log_calls = logging.getLogger("solidworks-mcp.calls")
log_calls.setLevel(logging.INFO)
log_calls.propagate = False  # this is a data file, not console chatter
try:
    _calls_handler = logging.FileHandler(CALL_LOG_PATH, encoding="utf-8")
    _calls_handler.setFormatter(logging.Formatter("%(message)s"))
    log_calls.addHandler(_calls_handler)
except OSError as exc:
    # A read-only install directory or a locked-down profile folder shouldn't
    # take the whole server down over an audit trail it can live without.
    log.warning("Could not open call log at %s (%s); call logging disabled",
                CALL_LOG_PATH, exc)

# Argument values can be long (a filepath, a macro's full source) or bulky
# (an embedded image). The call log is for "what was called, with roughly
# what, how long did it take, did it fail" -- not a faithful replay record --
# so each value is summarized rather than stored in full.
_CALL_LOG_MAX_VALUE_LEN = 200


def _summarize_call_argument(value):
    if isinstance(value, str):
        return value if len(value) <= _CALL_LOG_MAX_VALUE_LEN else (
            f"{value[:_CALL_LOG_MAX_VALUE_LEN]}... ({len(value)} chars)")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _summarize_call_argument(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        shown = [_summarize_call_argument(v) for v in value[:20]]
        if len(value) > 20:
            shown.append(f"... ({len(value)} items total)")
        return shown
    return repr(value)[:_CALL_LOG_MAX_VALUE_LEN]


def _log_tool_call(name: str, arguments: dict, duration_ms: float,
                    ok: bool, error: Optional[str]) -> None:
    import json as _json  # local import: this module is logging-only, not a hot path
    record = {
        "ts": time.time(),
        "tool": name,
        "arguments": {k: _summarize_call_argument(v) for k, v in (arguments or {}).items()},
        "duration_ms": duration_ms,
        "ok": ok,
    }
    if error is not None:
        record["error"] = error[:_CALL_LOG_MAX_VALUE_LEN]
    try:
        log_calls.info(_json.dumps(record, default=str))
    except Exception:
        log.debug("Failed to write structured call log entry", exc_info=True)

SERVER_INSTRUCTIONS = """\
Este servidor controla uma sessao REAL e ja aberta do SolidWorks via COM: \
cada chamada tem efeito imediato e pode alterar, salvar ou fechar \
documentos de verdade. Antes de modelar algo nao trivial, siga este \
metodo (validado nas replicas de engenharia deste projeto -- ver \
RELATORIO_TESTES.md):

1. ISOLAR -- trabalhe num documento novo e descartavel (create_new_part / \
   create_new_assembly). Nao reaproveite nem edite um documento que o \
   usuario ja tinha aberto, a menos que ele peca isso explicitamente.
2. PLANEJAR -- antes de chamar qualquer tool, escreva em texto a arvore de \
   features pretendida e as dimensoes criticas que precisam bater com o \
   objetivo. Nao improvise feature a feature sem plano.
3. CONSTRUIR INCREMENTAL -- execute o plano em pequenos grupos de \
   features. Prefira ferramentas confirmadas OK no resource \
   solidworks://tool-status; para uma ferramenta marcada EXP, releia as \
   ressalvas la descritas antes de depender dela.
4. VALIDAR A CADA PASSO -- depois de cada feature estrutural (extrude, \
   corte, padrao, chanfro, casca...), chame measure_body e/ou \
   validate_model. O SolidWorks as vezes retorna sucesso mesmo com o \
   recurso em erro suprimido; nao acumule features sem validar.
5. INSPECIONAR VISUALMENTE -- chame capture_standard_views (ou \
   zoom_to_fit + set_view) periodicamente e observe a imagem retornada \
   antes de seguir para a proxima etapa, comparando proporcao e \
   silhueta com a referencia ou descricao pedida.
6. Use o prompt design_from_reference como roteiro completo quando a \
   tarefa for replicar uma peca a partir de uma referencia real (foto, \
   catalogo, desenho tecnico).
7. CONHECIMENTO DE ENGENHARIA -- antes de escolher material, tolerancia, \
   ajuste, elemento padronizado (parafuso/rolamento/chaveta) ou raio de \
   dobra/canto, consulte os resources solidworks://knowledge/* (materiais, \
   tolerancias-e-ajustes, gdt, elementos-de-maquina, chapa-metalica, \
   soldas-e-perfis-estruturais, processos-de-fabricacao, verificacao-e-qa; \
   solidworks://knowledge/roteiro-projetista amarra todos eles no fluxo \
   completo). Nao invente numero de catalogo nem propriedade de material --  \
   esses resources tem a tabela certa.

execute_python fica desligado por padrao e so deve ser usado se o \
usuario pedir execucao de script explicitamente com \
SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1 ja configurado.
"""

mcp = FastMCP("solidworks-mcp", instructions=SERVER_INSTRUCTIONS)


def _install_call_instrumentation(server: FastMCP) -> None:
    """Wrap the one place every tool call passes through regardless of which
    of the 150 tools it is: a structured log entry (see CALL_LOG_PATH above)
    and the per-tool timeout budget (see _timeout_budget_for below), neither
    of which _run can tell on its own -- every tool's COM work runs through
    a locally-defined `_impl` closure, so by the time a call reaches _run,
    the one piece of identifying information already available to
    ToolManager.call_tool (the tool's name) has been lost."""
    tool_manager = server._tool_manager
    original_call_tool = tool_manager.call_tool

    async def instrumented_call_tool(name, arguments, *args, **kwargs):
        token = _current_tool_name.set(name)
        start = time.monotonic()
        try:
            result = await original_call_tool(name, arguments, *args, **kwargs)
        except Exception as exc:
            _log_tool_call(name, arguments, round((time.monotonic() - start) * 1000, 1),
                            ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            _log_tool_call(name, arguments, round((time.monotonic() - start) * 1000, 1),
                            ok=True, error=None)
            return result
        finally:
            _current_tool_name.reset(token)

    tool_manager.call_tool = instrumented_call_tool


_install_call_instrumentation(mcp)

# ---------------------------------------------------------------------------
# Single-threaded COM executor
# ---------------------------------------------------------------------------
# pywin32 COM objects are apartment-threaded: an object obtained on one OS
# thread cannot be safely used from another. asyncio's default thread-pool
# offloading for sync callables does not guarantee the same worker thread
# across calls, so we pin all COM work to one persistent thread instead.

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sw-com")

# A single flat timeout punished every tool the same: a get_document_info
# that hangs is indistinguishable from a 400-component BOM that is
# legitimately still working, for up to the same 120s either way. Below,
# _timeout_budget_for gives each tool a budget sized to what it actually
# does; COM_TIMEOUT_SECONDS remains exactly what it always was -- the
# fallback for anything not otherwise categorized, AND (unchanged) a knob
# tests/run_live_test_project.py already pokes directly to widen it for a
# whole live run. The sentinel comparison below is what keeps that working:
# raising COM_TIMEOUT_SECONDS above its own default overrides every
# category's budget rather than being floored by them, which is what that
# script actually wants ("give the live run more room"), not a 15s cap on
# every read tool it then calls.
_DEFAULT_COM_TIMEOUT_SECONDS = 120
COM_TIMEOUT_SECONDS = _DEFAULT_COM_TIMEOUT_SECONDS

# Sizes from the PDF's own R3.4: simple read 15s; ordinary feature/setter
# 60s; assembly/BOM-scale read 300s; batch file export 900s;
# connect_solidworks with a cold SolidWorks launch 240s (its own internal
# wait loop in _connect() is itself bounded at 120s -- this has to be larger
# than that, not equal to it, or the outer timeout can fire first with a
# more confusing message while the launch is still legitimately in progress).
_READ_SIMPLE_TIMEOUT_SECONDS = 15
_FEATURE_TIMEOUT_SECONDS = 60
_ASSEMBLY_SCALE_TIMEOUT_SECONDS = 300
_BATCH_EXPORT_TIMEOUT_SECONDS = 900

# Per-tool overrides, checked first. Everything else falls back to either
# the assembly-scale or the plain read/feature budget below.
TIMEOUT_BUDGET_OVERRIDES = {
    "connect_solidworks": 240,
}

# Reading the whole assembly tree, cross-component interference or a
# component/feature/mate listing legitimately scales with model size --
# these are reads, but not the "stuck in under 15s or something is wrong"
# kind get_document_info or measure_body are.
ASSEMBLY_SCALE_TOOLS = frozenset({
    "extract_assembly_bom", "extract_assembly_data", "interference_check",
    "list_components", "list_mates", "list_features", "list_faces",
    "list_configurations", "list_display_states", "list_motion_studies",
})

BATCH_EXPORT_TOOLS = frozenset({"export_document", "export_flat_pattern_dxf", "pack_and_go"})

# Set by _install_call_instrumentation's wrapper around ToolManager.call_tool
# (the one place that knows which of the 150 tools is running) and read by
# _run, several `await`s deeper in the same task -- a plain module-level
# variable would race under concurrent calls, but a contextvar is scoped to
# the call that set it regardless of how many other calls are in flight.
_current_tool_name = contextvars.ContextVar("current_tool_name", default=None)


def _timeout_budget_for(tool_name: Optional[str]) -> float:
    if COM_TIMEOUT_SECONDS != _DEFAULT_COM_TIMEOUT_SECONDS:
        return COM_TIMEOUT_SECONDS  # explicit override in effect; see above
    if tool_name is None:
        return _DEFAULT_COM_TIMEOUT_SECONDS
    if tool_name in TIMEOUT_BUDGET_OVERRIDES:
        return TIMEOUT_BUDGET_OVERRIDES[tool_name]
    if tool_name in BATCH_EXPORT_TOOLS:
        return _BATCH_EXPORT_TIMEOUT_SECONDS
    if tool_name in ASSEMBLY_SCALE_TOOLS:
        return _ASSEMBLY_SCALE_TIMEOUT_SECONDS
    tool = mcp._tool_manager._tools.get(tool_name)
    read_only = bool(tool is not None and tool.annotations and tool.annotations.readOnlyHint)
    return _READ_SIMPLE_TIMEOUT_SECONDS if read_only else _FEATURE_TIMEOUT_SECONDS


def _init_com_thread():
    pythoncom.CoInitialize()
    log.debug("COM thread initialised (CoInitialize)")


def _cleanup_com_thread():
    try:
        pythoncom.CoUninitialize()
        log.debug("COM thread cleaned up (CoUninitialize)")
    except Exception:
        pass


_executor.submit(_init_com_thread).result()


def _shutdown():
    log.info("Shutting down COM executor")
    try:
        _executor.submit(_cleanup_com_thread).result(timeout=5)
    except Exception:
        pass
    _executor.shutdown(wait=False)


atexit.register(_shutdown)


async def _run(fn, *args, **kwargs):
    fn_name = fn.__name__ if hasattr(fn, "__name__") else str(fn)
    timeout = _timeout_budget_for(_current_tool_name.get())
    log.debug("COM call: %s (timeout budget %ss)", fn_name, timeout)
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        log.debug("COM call OK: %s", fn_name)
        return result
    except asyncio.TimeoutError:
        log.error("COM operation timed out after %ds: %s", timeout, fn_name)
        raise RuntimeError(
            f"SolidWorks operation timed out after {timeout}s. "
            "The application may be blocked by a dialog or heavy computation. "
            "Close any open dialogs and retry."
        )
    except Exception:
        log.exception("COM call failed: %s", fn_name)
        raise


def _redraw_document(doc) -> None:
    """Refresh graphics after a visual-property update without failing it."""
    try:
        redraw = doc.GraphicsRedraw2
        if callable(redraw):
            redraw()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

UNIT_TO_METERS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
_default_unit = "mm"


def to_meters(value: float, unit: Optional[str]) -> float:
    u = (unit or _default_unit).lower()
    if u not in UNIT_TO_METERS:
        raise ValueError(f"Unknown unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
    return value * UNIT_TO_METERS[u]


# ---------------------------------------------------------------------------
# Project-root path policy (writes only)
# ---------------------------------------------------------------------------
# Opt-in. A single engineer driving their own, already-open SolidWorks needs
# no restriction to save or export into their own project tree -- so with
# nothing configured this is exactly the os.path.abspath every write call
# already did. Set SOLIDWORKS_MCP_PROJECT_ROOTS (one or more absolute
# directories, separated by os.pathsep) when this server runs on someone
# else's behalf against a managed folder -- the Alfa Detail AI backend
# driving a shared project tree is exactly this case -- so a wrong or
# model-generated path can't write or overwrite anything outside it.
PROJECT_ROOTS_ENV = "SOLIDWORKS_MCP_PROJECT_ROOTS"


def _configured_project_roots() -> list:
    raw = os.environ.get(PROJECT_ROOTS_ENV, "")
    return [os.path.abspath(p) for p in raw.split(os.pathsep) if p.strip()]


def _resolve_write_path(path: str) -> str:
    """Resolve a tool-supplied output path for a WRITE, enforcing the
    project-root allowlist when PROJECT_ROOTS_ENV is set.

    Every tool that creates or overwrites a file on disk (save_document,
    export_document, export_flat_pattern_dxf, pack_and_go,
    capture_standard_views, capture_viewport) must resolve its destination
    through this, not a bare os.path.abspath, or a configured allowlist is
    silently bypassed for that one tool."""
    abs_path = os.path.abspath(path)
    roots = _configured_project_roots()
    if roots and not any(
        abs_path == root or abs_path.startswith(root + os.sep) for root in roots
    ):
        raise PermissionError(
            f"Refusing to write to '{abs_path}': it is outside the configured "
            f"project root(s) ({PROJECT_ROOTS_ENV}="
            f"{os.environ.get(PROJECT_ROOTS_ENV)!r}). Pass a path inside one "
            f"of those roots, or reconfigure {PROJECT_ROOTS_ENV}."
        )
    return abs_path


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_app = None
_last_launch_happened = False

SW_YEAR_RANGE = range(2030, 2015, -1)


def _find_solidworks_exe() -> Optional[str]:
    try:
        import winreg

        # Real registry layout (SW 2020+): the version keys live directly under
        # SOFTWARE\SolidWorks as "SOLIDWORKS <year>", each with a Setup subkey
        # holding "SolidWorks Folder" (the install directory).
        for hive, root in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\SolidWorks"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\SolidWorks"),
        ):
            try:
                with winreg.OpenKey(hive, root) as key:
                    versions = []
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(key, i)
                            i += 1
                            if sub.upper().startswith("SOLIDWORKS "):
                                versions.append(sub)
                        except OSError:
                            break
                    for version in sorted(versions, reverse=True):
                        try:
                            with winreg.OpenKey(key, version + r"\Setup") as skey:
                                folder, _ = winreg.QueryValueEx(skey, "SolidWorks Folder")
                                exe = os.path.join(folder, "SLDWORKS.exe")
                                if os.path.exists(exe):
                                    return exe
                        except OSError:
                            continue
            except OSError:
                continue

        # Legacy layout: SOFTWARE\SolidWorks\SOLIDWORKS\<version>\SolidWorks Exe
        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\SolidWorks\SOLIDWORKS"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\SolidWorks\SOLIDWORKS"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    versions = []
                    i = 0
                    while True:
                        try:
                            versions.append(winreg.EnumKey(key, i))
                            i += 1
                        except OSError:
                            break
                    for version in sorted(versions, reverse=True):
                        try:
                            with winreg.OpenKey(key, version) as vkey:
                                exe, _ = winreg.QueryValueEx(vkey, "SolidWorks Exe")
                                if os.path.exists(exe):
                                    return exe
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass

    # Filesystem fallback. Modern installers drop the version from the folder
    # name ("...\SOLIDWORKS\") and suffix side-by-side installs ("SOLIDWORKS (2)").
    candidates = [
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
        r"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    ]
    for n in range(2, 6):
        candidates.append(rf"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS ({n})\SLDWORKS.exe")
        candidates.append(rf"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS ({n})\SLDWORKS.exe")
    for year in SW_YEAR_RANGE:
        candidates.append(rf"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS {year}\SLDWORKS.exe")
        candidates.append(rf"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS {year}\SLDWORKS.exe")
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _com_is_alive(app) -> bool:
    """Round-trip a real COM call to prove the proxy still reaches SolidWorks.

    Bare attribute access is NOT a liveness check here. Under the generated
    SolidWorks type library, ``RevisionNumber`` is a method, so
    ``app.RevisionNumber`` only builds a bound-method wrapper on the Python
    side and never crosses the COM boundary -- a proxy left over from a
    SolidWorks instance that has since been closed looks perfectly healthy.
    Actually invoking it is what surfaces the "RPC server unavailable" error
    that tells us to reconnect.
    """
    try:
        revision = app.RevisionNumber
        if callable(revision):
            revision = revision()
        return revision is not None
    except Exception:
        return False


def _connect_running_instance():
    for method in (
        lambda: win32com.client.GetActiveObject("SldWorks.Application"),
        lambda: win32com.client.GetObject(Class="SldWorks.Application"),
    ):
        try:
            app = method()
            if not _com_is_alive(app):
                continue
            app.Visible = True
            return app
        except Exception:
            continue
    return None


def _connect():
    """Return a live SldWorks.Application, launching SolidWorks if needed.

    Must only be called from the COM worker thread.
    """
    global _app, _last_launch_happened
    _last_launch_happened = False

    if _app is not None:
        if _com_is_alive(_app):
            return _app
        log.warning("Cached COM connection is stale, reconnecting")
        _app = None

    app = _connect_running_instance()
    if app is not None:
        log.info("Connected to running SolidWorks instance")
        _app = app
        return _app

    exe = _find_solidworks_exe()
    if not exe:
        raise RuntimeError(
            "SolidWorks was not found on this machine. Install it, or launch it "
            "manually and try again."
        )

    log.info("Launching SolidWorks from %s", exe)
    os.startfile(exe)
    _last_launch_happened = True
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        app = _connect_running_instance()
        if app is not None:
            log.info("SolidWorks launched and connected")
            _app = app
            return _app

    raise RuntimeError("Timed out waiting for SolidWorks to start (120s). Close any startup dialogs and retry.")


def _active_doc():
    app = _connect()
    doc = app.ActiveDoc
    if doc is None:
        raise RuntimeError("No active document. Call create_new_part or open_document first.")
    return doc


def _split_com_result(result):
    """Normalize a pywin32 result that may include COM ``out`` parameters.

    With the SolidWorks type library generated for Python 3.14, methods such
    as OpenDoc6 and ActivateDoc3 return ``(value, out1, ...)`` when their
    output parameters are supplied as plain integers.  Older/dynamic pywin32
    dispatch returns only the primary value.  Keeping that difference here
    avoids passing unsupported VARIANT-by-reference wrappers to the generated
    proxy and makes the public document tools work in either configuration.
    """
    if isinstance(result, tuple):
        return result[0], result[1:]
    return result, ()


def _open_doc6(app, filepath: str, doc_type: int, options: int = 0):
    """Open a document through OpenDoc6 and return ``(document, errors, warnings)``.

    Same dual-mode handling as _save_doc3: try real by-reference VARIANTs for
    the Errors/Warnings out-params first (required when the Application
    object is dynamic IDispatch -- plain ints raise DISP_E_TYPEMISMATCH
    there), and fall back to plain ints for whichever binding rejects a
    VARIANT in that position instead.
    """
    errors_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    try:
        result = app.OpenDoc6(filepath, doc_type, options, "", errors_out, warnings_out)
        doc, outputs = _split_com_result(result)
        errors = int(outputs[0]) if len(outputs) > 0 else int(errors_out.value)
        warnings = int(outputs[1]) if len(outputs) > 1 else int(warnings_out.value)
    except TypeError:
        result = app.OpenDoc6(filepath, doc_type, options, "", 0, 0)
        doc, outputs = _split_com_result(result)
        errors = int(outputs[0]) if len(outputs) > 0 else 0
        warnings = int(outputs[1]) if len(outputs) > 1 else 0
    return doc, errors, warnings


def _activate_doc3(app, title: str, make_visible: bool = True):
    """Activate a document while tolerating typed and dynamic COM dispatch.

    Same VARIANT-first, plain-int-fallback handling as _open_doc6/_save_doc3
    for the Errors out-param.
    """
    errors_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    try:
        result = app.ActivateDoc3(title, make_visible, 0, errors_out)
        doc, outputs = _split_com_result(result)
        errors = int(outputs[0]) if outputs else int(errors_out.value)
    except TypeError:
        result = app.ActivateDoc3(title, make_visible, 0, 0)
        doc, outputs = _split_com_result(result)
        errors = int(outputs[0]) if outputs else 0
    return doc, errors


def _save_doc3(doc, options: int = 0):
    """Save a document and return ``(succeeded, errors, warnings)``."""
    # Active document objects are commonly exposed as dynamic IDispatch even
    # when the application object is type-library generated.  That dispatch
    # requires real by-reference VARIANTs for Save3; the typed fallback emits
    # an ``(value, error, warning)`` tuple instead.
    errors_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    try:
        result = doc.Save3(options, errors_out, warnings_out)
        succeeded, outputs = _split_com_result(result)
        errors = int(outputs[0]) if len(outputs) > 0 else int(errors_out.value)
        warnings = int(outputs[1]) if len(outputs) > 1 else int(warnings_out.value)
    except TypeError:
        result = doc.Save3(options, 0, 0)
        succeeded, outputs = _split_com_result(result)
        errors = int(outputs[0]) if len(outputs) > 0 else 0
        warnings = int(outputs[1]) if len(outputs) > 1 else 0
    return bool(succeeded), errors, warnings


def _com_member(obj, name, *args):
    """Read a COM member that typed and dynamic dispatch expose differently.

    The same SolidWorks member arrives as a bound method under dynamic
    IDispatch and as a plain value under the generated type library, so the
    inspection tools normalize both shapes through here.

    Dynamic dispatch can also hand back an already-evaluated object that is
    still ``callable``; invoking it then raises "member not found" even though
    the value itself is what was wanted. For a no-argument member that raise
    is recoverable, so fall back to the value. With arguments it is not --
    falling back would silently drop them -- so the error propagates.
    """
    value = getattr(obj, name)
    if not callable(value):
        return value
    try:
        return value(*args)
    except Exception:
        if args:
            raise
        return value


def _doc_title(doc) -> str:
    t = doc.GetTitle
    return t() if callable(t) else t


def _doc_path(doc) -> str:
    p = doc.GetPathName
    return p() if callable(p) else p


def _doc_type(doc) -> int:
    t = doc.GetType
    return t() if callable(t) else t


def _find_template(kind: str) -> str:
    # swUserPreferenceStringValue_e: swDefaultTemplatePart=8, Assembly=9, Drawing=10.
    # (Indices 4/5/6 are NOT the templates — index 6 returns the templates FOLDER,
    # which silently makes NewDocument create a blank Part instead of a Drawing.)
    pref_index = {"part": 8, "assembly": 9, "drawing": 10}[kind]
    filename = {"part": "Part.prtdot", "assembly": "Assembly.asmdot", "drawing": "Drawing.drwdot"}[kind]

    app = _connect()
    try:
        t = app.GetUserPreferenceStringValue(pref_index)
        if t and os.path.isfile(t):
            return t
    except Exception:
        pass

    for year in SW_YEAR_RANGE:
        for folder in (
            rf"C:\ProgramData\SOLIDWORKS\SOLIDWORKS {year}\templates",
            rf"C:\ProgramData\SolidWorks\SOLIDWORKS {year}\templates",
        ):
            candidate = os.path.join(folder, filename)
            if os.path.exists(candidate):
                return candidate

    raise RuntimeError(
        f"Could not find a {kind} template. Open SolidWorks > Options > Default Templates "
        f"and set one, or create a document manually once."
    )


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def _select_by_id(doc, name: str, sel_type: str, x: float = 0, y: float = 0, z: float = 0) -> bool:
    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    return bool(doc.Extension.SelectByID2(name, sel_type, x, y, z, False, 0, empty, 0))


def _planar_face_at_point(doc, x: float, y: float, z: float, tolerance: float = 1e-5):
    """Return the planar face at a model-space point, or raise a useful error.

    SelectByID2 is unreliable for unnamed faces in weldments, especially where
    several member faces meet. IFace2.GetClosestPointOn lets us resolve the
    caller's point against the actual model geometry instead.
    """
    requested = (x, y, z)
    closest = None
    for raw_body in doc.GetBodies2(0, True) or ():
        body = win32com.client.Dispatch(raw_body)
        for raw_face in body.GetFaces() or ():
            face = win32com.client.Dispatch(raw_face)
            try:
                surface = win32com.client.Dispatch(face.GetSurface)
                is_planar = surface.IsPlane
                if callable(is_planar):
                    is_planar = is_planar()
                if not is_planar:
                    continue
                nearest = tuple(face.GetClosestPointOn(*requested))
                distance = math.sqrt(sum(
                    (nearest[index] - requested[index]) ** 2 for index in range(3)
                ))
            except Exception:
                continue
            if closest is None or distance < closest[0]:
                closest = (distance, face)

    if closest is None or closest[0] > tolerance:
        nearest_distance = None if closest is None else closest[0]
        raise RuntimeError(
            f"No planar face was found at ({x}, {y}, {z}); "
            f"nearest planar face is {nearest_distance!r} m away."
        )
    return closest[1]


def _standard_plane_name(doc, which: str) -> str:
    """Standard plane names are localized (e.g. 'Front Plane' vs 'Plano frontal'),
    so we resolve them positionally from the feature tree instead of hardcoding text.
    The three standard planes always appear first, in Front/Top/Right order.
    """
    order = {"front": 0, "top": 1, "right": 2}
    idx = order.get(which.lower())
    if idx is None:
        raise ValueError("plane must be 'front', 'top', or 'right'")

    planes = []
    feat = doc.FirstFeature
    while feat is not None and len(planes) <= idx:
        try:
            if feat.GetTypeName2 == "RefPlane":
                planes.append(feat.Name)
        except Exception:
            pass
        try:
            feat = feat.GetNextFeature
        except Exception:
            break

    if idx >= len(planes):
        raise RuntimeError("Could not locate the document's standard planes.")
    return planes[idx]


#: Name of the most recent sketch opened by create_sketch/create_sketch_on_face
#: in this server process, captured the instant it becomes ActiveSketch (see
#: _remember_active_sketch). This is the authoritative source for "the sketch
#: you just drew" -- by the time cut_extrude/revolve_sketch run, close_sketch
#: has already cleared doc.SketchManager.ActiveSketch, so nothing short of a
#: value captured at creation time can name it directly.
_last_user_sketch_name: Optional[str] = None

#: Live COM reference to that same sketch, captured by close_sketch right
#: before it closes. Needed because a sketch closed via create_sketch_on_face
#: on a face (not a named plane) was observed, live, to never actually appear
#: in FeatureManager.GetFeatures(False) before the next cut_extrude runs --
#: not a timing issue a rebuild fixes (tried; still absent after a forced
#: EditRebuild3). Selecting this object directly (ISketch.Select2) sidesteps
#: name/tree lookup entirely, using the handle already in hand instead of
#: re-finding something that may not be independently enumerable yet.
_last_user_sketch_obj = None


def _remember_active_sketch(doc) -> None:
    """Capture the name AND live object of the sketch doc.InsertSketch2 just
    opened, while it is still ActiveSketch and therefore unambiguous. Call
    this right after opening a sketch on a plane/face -- never after closing
    one (close_sketch calls this itself, one line before closing, which is
    more reliable still -- see its comment)."""
    global _last_user_sketch_name, _last_user_sketch_obj
    try:
        active = doc.SketchManager.ActiveSketch
        if active is not None:
            _last_user_sketch_obj = win32com.client.Dispatch(active)
            _last_user_sketch_name = _last_user_sketch_obj.Name
    except Exception:
        pass


def _find_last_sketch(doc) -> Optional[str]:
    """Fallback for _select_last_sketch: return the last non-suppressed
    ProfileFeature in the tree, when _last_user_sketch_name isn't usable
    (e.g. a sketch that predates this server process, or was opened by some
    path other than create_sketch/create_sketch_on_face).

    Was implemented as a FirstFeature/GetNextFeature linked-list walk with a
    bare ``except: break`` -- if traversal threw a COM error past a corrupted
    or orphaned feature (see BUG 4 in bug_report_solidworks_mcp.txt, feature
    "Esboço28"), the walk silently stopped there, and every later sketch was
    never reached: cut_extrude/revolve_sketch kept re-selecting that same
    stale name forever, including across save/reopen. GetFeatures(False)
    (the same array-based enumeration already used by list_mates, shell_body,
    create_helix, etc.) visits every feature regardless of what happened to
    its neighbors, so one bad feature can no longer cut the scan short.
    Suppressed features are skipped: an orphaned sketch like "Esboço28" is
    suppressed, and was never a real candidate for "the sketch you just drew".

    CAVEAT (found live, 2026-10-04): this can still pick the wrong feature on
    a sheet-metal part -- SolidWorks' own bend/flatten machinery leaves
    non-suppressed internal features also typed "ProfileFeature" (observed:
    "Curva-Linhas2", right after a OneBend feature) that sort after the sketch
    you actually drew. That is exactly why this is now a fallback rather than
    the primary path.
    """
    name = None
    for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
        try:
            feature = win32com.client.Dispatch(raw_feature)
            feature_type = feature.GetTypeName2
            if callable(feature_type):
                feature_type = feature_type()
        except Exception:
            continue  # can't even tell what this is -- never a real candidate
        if feature_type != "ProfileFeature":
            continue
        # IsSuppressed/Name are read separately, each defaulting to "keep this
        # candidate" on error: a bare `except: continue` here previously
        # dropped the whole candidate silently on any COM hiccup, which left
        # `name` stuck on whatever the PREVIOUS successful match was (found
        # live, 2026-10-04 -- a feature after "Curva-Linhas2" errored on
        # access, so the scan silently kept reporting a stale sketch several
        # features back instead of surfacing the problem).
        try:
            suppressed = feature.IsSuppressed
            if callable(suppressed):
                suppressed = suppressed()
        except Exception:
            suppressed = False
        if suppressed:
            continue
        try:
            name = feature.Name
        except Exception:
            continue
    return name


#: Set by _select_last_sketch on every call to say which of its 3 paths
#: resolved the name (or why each one before it was skipped/failed), so a
#: tool's error message can report it instead of us re-guessing blind after
#: every live-test failure. Read this via get_sketch_status's debug field or
#: by having cut_extrude's error text include it -- see cut_extrude.
_last_select_debug: str = ""


def _select_last_sketch(doc) -> str:
    global _last_user_sketch_name, _last_user_sketch_obj, _last_select_debug
    try:
        if doc.SketchManager.ActiveSketch is not None:
            doc.SketchManager.InsertSketch(True)  # close it
    except Exception:
        pass

    doc.ClearSelection2(True)
    name = None
    debug_steps = [f"cached_name={_last_user_sketch_name!r} cached_obj={'set' if _last_user_sketch_obj is not None else 'None'}"]

    # 1) The live object, if close_sketch captured one for the current
    #    document. Select4(Append, Callout) is the generic entity-selection
    #    signature this file already uses for edges/faces/sketch segments
    #    (_select_all_edges, trim_extend_structural, ...) -- ISketch shares
    #    it too. (5.8.4 tried IFeature's Select2(Append, Mark) here instead;
    #    live testing showed that was the wrong method for an ISketch object,
    #    so it threw, got swallowed by the except below, and this whole path
    #    silently never engaged.) Marks the sketch as the active selection
    #    the same way SelectByID2(..., "SKETCH", ...) would, without needing
    #    the sketch to be independently findable by name/tree first.
    if _last_user_sketch_obj is not None:
        try:
            ok = _last_user_sketch_obj.Select4(False, pythoncom.Nothing)
            debug_steps.append(f"step1 Select4->{ok!r}")
            if ok:
                name = _last_user_sketch_name or "<sketch>"
        except Exception as exc:
            debug_steps.append(f"step1 Select4 raised {exc!r}")
            _last_user_sketch_obj = None  # stale COM reference -- stop trying it
    else:
        debug_steps.append("step1 skipped (no cached_obj)")

    # 2) Fall back to selecting by the cached name (handles the case where
    #    the object was never captured, e.g. a sketch opened by some other
    #    path, but the name still resolves in the tree).
    if not name:
        if _last_user_sketch_name:
            ok = _select_by_id(doc, _last_user_sketch_name, "SKETCH")
            debug_steps.append(f"step2 SelectByID2({_last_user_sketch_name!r})->{ok}")
            if ok:
                name = _last_user_sketch_name
        else:
            debug_steps.append("step2 skipped (no cached_name)")

    # 3) Last resort: scan the tree for the last non-suppressed ProfileFeature.
    if not name:
        doc.ClearSelection2(True)
        name = _find_last_sketch(doc)
        debug_steps.append(f"step3 _find_last_sketch->{name!r}")
        if not name:
            _last_select_debug = " | ".join(debug_steps)
            raise RuntimeError("No sketch found. Create a sketch and draw a closed profile first.")
        if not _select_by_id(doc, name, "SKETCH"):
            _last_select_debug = " | ".join(debug_steps)
            raise RuntimeError(f"Could not select sketch '{name}'.")
    _last_select_debug = " | ".join(debug_steps) + f" | RESULT={name!r}"
    return name


def _select_all_edges(doc) -> int:
    doc.ClearSelection2(True)
    bodies = doc.GetBodies2(0, True)
    if not bodies:
        raise RuntimeError("No solid body found in the active document.")
    count = 0
    for raw_body in bodies:
        body = win32com.client.Dispatch(raw_body)
        for raw_edge in body.GetEdges():
            edge = win32com.client.Dispatch(raw_edge)
            if edge.Select4(True, pythoncom.Nothing):
                count += 1
    if count == 0:
        raise RuntimeError("Could not select any edges.")
    return count


#: Feature type names (as GetTypeName2 reports them) that only exist on a
#: folded sheet-metal body -- i.e. a body with at least one bend.
_SHEET_METAL_BEND_FEATURE_TYPES = {"SMBaseFlange", "EdgeFlange", "OneBend"}


def _has_sheet_metal_bends(doc) -> bool:
    """True if the active document has a folded sheet-metal bend anywhere
    in its feature tree (as opposed to e.g. a flat, unbent sheet-metal
    blank, or a part with no sheet-metal features at all)."""
    for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
        try:
            feature_type = win32com.client.Dispatch(raw_feature).GetTypeName2
            if callable(feature_type):
                feature_type = feature_type()
        except Exception:
            continue
        if feature_type in _SHEET_METAL_BEND_FEATURE_TYPES:
            return True
    return False


def _active_assembly():
    doc = _active_doc()
    if _doc_type(doc) != 2:
        raise RuntimeError("The active document is not an assembly.")
    return doc


def _motion_study_manager(doc):
    """Return the native MotionStudy manager with a Python 3.14-safe dispatch.

    The dynamic COM proxy resolves the zero-argument
    ``IModelDocExtension.GetMotionStudyManager`` member as a property.  Its
    registered DISPID is 140 in the SOLIDWORKS 2025 type library, so invoke it
    with its declared return type instead.
    """
    raw_manager = doc.Extension._oleobj_.InvokeTypes(140, 0, 1, (9, 0), ())
    if raw_manager is None:
        raise RuntimeError("SolidWorks Motion Study Manager is unavailable for the active document.")
    return win32com.client.Dispatch(raw_manager)


def _invoke_motion_manager(manager, member: str, return_type, arguments=()):
    """Call a MotionStudy manager method without dynamic-proxy ambiguity."""
    dispid = manager._oleobj_.GetIDsOfNames(member)
    return manager._oleobj_.InvokeTypes(dispid, 0, 1, return_type, arguments)


# ---------------------------------------------------------------------------
# Component placement: measure, never assume
# ---------------------------------------------------------------------------
# A placement that silently lands somewhere else is the hardest failure to
# notice from a tool result: the call returns, the numbers echoed back are the
# ones that were asked for, and the part is only found in the wrong place much
# later -- in a view, or in a drawing. So every placement helper below reads
# the real state back out of SolidWorks and reports what it MEASURED next to
# what was requested, and anything it could not compute comes back as a
# warning in the payload instead of being swallowed.


def _unit_factor(unit: Optional[str]) -> tuple:
    """Resolve a unit name to (name, multiplier from metres to that unit)."""
    active = (unit or _default_unit).lower()
    if active not in UNIT_TO_METERS:
        raise ValueError(f"Unknown unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
    return active, 1.0 / UNIT_TO_METERS[active]


def _bodies_bounding_box(doc) -> tuple:
    """Union of the bounding boxes of EVERY solid body in a document.

    ``IBody2.GetBodyBox`` is per-body, so a multi-body part needs all of them
    unioned -- and multi-body is not an edge case here: every weldment and
    every structural-profile part is one. Reading only ``bodies[0]`` gives the
    box of whichever body happens to come first in the list, so an origin
    correction computed from it is wrong by however far the rest of the part
    extends beyond that single body. That is exactly the error that puts an
    inserted weldment somewhere other than the point it was asked for.

    Returns (box, body_count), box being [xmin, ymin, zmin, xmax, ymax, zmax]
    in metres -- or (None, body_count) when no body reported a usable box,
    which is the normal answer for a subassembly, a surface-only part, or a
    document that came in lightweight.
    """
    bodies = doc.GetBodies2(0, True) or ()
    low = [None, None, None]
    high = [None, None, None]
    measured = 0
    for raw_body in bodies:
        try:
            box = win32com.client.Dispatch(raw_body).GetBodyBox()
        except Exception:
            continue
        if not box or len(box) < 6:
            continue
        measured += 1
        for axis in range(3):
            lo, hi = box[axis], box[axis + 3]
            if lo > hi:
                lo, hi = hi, lo
            low[axis] = lo if low[axis] is None else min(low[axis], lo)
            high[axis] = hi if high[axis] is None else max(high[axis], hi)
    if not measured:
        return None, len(bodies)
    return [low[0], low[1], low[2], high[0], high[1], high[2]], len(bodies)


def _component_pose(component, factor: float) -> dict:
    """Read a component's real position and orientation out of SolidWorks.

    ``IMathTransform.ArrayData`` is the documented flat form of the pose:
    indices 0-8 the 3x3 rotation, 9-11 the translation in metres, 12 the scale.

    The rotation comes back ROW-major -- [[r00,r01,r02],[r10,r11,r12],
    [r20,r21,r22]] -- so a point (lx,ly,lz) in the component's own system maps
    to global.x = r00*lx + r01*ly + r02*lz (and so on), plus the translation.
    That convention was confirmed live against a known face; see
    .claude/knowledge/montagens_mecanicas_reais.md. Every payload carrying a
    matrix states it as ``rotation_convention`` precisely so no caller has to
    guess: assume the column convention instead and you get a plausible but
    wrong point, which still selects *a* face -- and a caller who then nudges
    the point until the selection "works" has validated the wrong convention
    rather than found the right one.
    """
    transform = component.Transform2
    data = transform.ArrayData
    if callable(data):
        data = data()
    values = list(data or ())
    if len(values) < 12:
        raise RuntimeError(
            "SolidWorks returned an invalid transform (fewer than 12 elements).")
    return {
        "translation": {
            "x": values[9] * factor,
            "y": values[10] * factor,
            "z": values[11] * factor,
        },
        "rotation_matrix": [list(values[0:3]), list(values[3:6]), list(values[6:9])],
        "rotation_convention": "row-major",
    }


def _read_pose(assy, name: str, factor: float):
    """Pose of a component by name, or None if it cannot be read right now."""
    try:
        component = _find_component(assy, name)
        if component is None:
            return None
        return _component_pose(component, factor)
    except Exception:
        return None


def _deviation(requested, measured: dict, tolerance: float) -> dict:
    """How far a measured point sits from the one that was requested."""
    dx = measured["x"] - requested[0]
    dy = measured["y"] - requested[1]
    dz = measured["z"] - requested[2]
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    return {
        "dx": dx, "dy": dy, "dz": dz,
        "distance": distance,
        "tolerance": tolerance,
        "within_tolerance": distance <= tolerance,
    }


def _preload_and_insert_component(assy, filepath: str, x: float, y: float, z: float):
    """AddComponent5 silently returns None unless the source document is already
    loaded into the SolidWorks session (via a silent OpenDoc6) and the assembly
    is re-activated as ActiveDoc right before the call.

    AddComponent5's own X/Y/Z place the component's BOUNDING-BOX CENTER at that
    point, not its origin -- confirmed live, 2026-10-04: inserting two parts at
    (0,0,0) and (10,15,10) landed their origins at (-20,-5,20) and (0,10,20)
    respectively, each exactly equal to (requested - that part's own bbox
    center). A caller reasoning in a part's own coordinate system (where
    insert_component's docstring says "at a position") has no way to predict
    that offset without a separate measure_body call on the source file first.
    Measure the just-opened source doc's bounding box here and add its center
    to x/y/z before the AddComponent5 call, so the component's ORIGIN -- not
    its bbox center -- ends up at the caller's requested point instead.

    The box is the union of EVERY solid body (_bodies_bounding_box), not the
    first one: a weldment or a structural profile part has several, and the
    first body's box alone describes only a piece of it.

    Returns (assembly, component, correction). ``correction`` records the
    offset actually applied, how many bodies it was measured from, and any
    reason it could not be computed -- so insert_component can report that
    rather than leaving the caller to infer it from a position that came out
    wrong.
    """
    assy_title = _doc_title(assy)
    app = _connect()
    ext = os.path.splitext(filepath)[1].lower()
    doc_type = {".sldprt": 1, ".sldasm": 2}.get(ext, 1)
    loaded_doc, errors, _warnings = _open_doc6(app, filepath, doc_type, 1)
    if loaded_doc is None:
        raise RuntimeError(f"Failed to load component '{filepath}' (error code {errors}).")

    correction = {"applied": False, "offset_m": [0.0, 0.0, 0.0],
                  "body_count": 0, "bodies_measured": False, "warnings": []}
    uncorrected = (
        "AddComponent5 places the bounding-box centre at the requested point, so the "
        "component's ORIGIN lands at (requested - bbox centre) instead: check "
        "actual_position against requested_position in the result before relying on "
        "this placement"
    )
    try:
        box, body_count = _bodies_bounding_box(loaded_doc)
        correction["body_count"] = body_count
        correction["bodies_measured"] = box is not None
        if box is None:
            correction["warnings"].append(
                f"no solid body in '{os.path.basename(filepath)}' reported a readable "
                f"bounding box (subassembly, surface-only or lightweight), so the origin "
                f"correction could not be computed -- {uncorrected}")
        else:
            offset = [(box[0] + box[3]) / 2.0,
                      (box[1] + box[4]) / 2.0,
                      (box[2] + box[5]) / 2.0]
            x += offset[0]
            y += offset[1]
            z += offset[2]
            correction["applied"] = True
            correction["offset_m"] = offset
    except Exception as exc:
        # Reported, never swallowed: the component still gets inserted, just at
        # AddComponent5's own bbox-centre placement, and the caller has to know
        # that to read the resulting position correctly.
        correction["warnings"].append(
            f"could not measure the source document's bodies ({exc}) -- {uncorrected}")

    active_assy, reactivate_errors = _activate_doc3(app, assy_title, False)
    if active_assy is None:
        raise RuntimeError(f"Failed to re-activate assembly '{assy_title}' (error code {reactivate_errors}).")
    assy = app.ActiveDoc

    comp = assy.AddComponent5(filepath, 0, "", False, "", x, y, z)
    if comp is None:
        raise RuntimeError(f"Failed to insert component '{filepath}' into the assembly.")
    return assy, win32com.client.Dispatch(comp), correction


def _find_component_feature(assy, name: str):
    feat = assy.FirstFeature
    while feat is not None:
        try:
            if feat.GetTypeName2 == "Reference" and feat.Name == name:
                return feat
        except Exception:
            pass
        try:
            feat = feat.GetNextFeature
        except Exception:
            break
    return None


def _find_component(assy, name: str):
    """Return the IComponent2 instance matching an assembly component name."""
    for raw_component in assy.GetComponents(True) or ():
        component = win32com.client.Dispatch(raw_component)
        if component.Name2 == name:
            return component
    return None


def _select_component(assy, name: str):
    assy.ClearSelection2(True)
    feat = _find_component_feature(assy, name)
    if feat is None:
        raise RuntimeError(f"Component '{name}' not found in the assembly.")
    if not feat.Select2(False, 0):
        raise RuntimeError(f"Could not select component '{name}'.")


# ===========================================================================
# Connection tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def connect_solidworks() -> dict:
    """Connect to a running SolidWorks instance, launching it if it isn't open yet."""

    def _impl():
        app = _connect()
        version = app.RevisionNumber
        if callable(version):
            version = version()
        return {"connected": True, "version": str(version), "launched": _last_launch_happened}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_solidworks_info() -> dict:
    """Get the connected SolidWorks application's version and visibility state."""

    def _impl():
        app = _connect()
        version = app.RevisionNumber
        if callable(version):
            version = version()
        return {"version": str(version), "visible": bool(app.Visible)}

    return await _run(_impl)


# ===========================================================================
# Document tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_new_part() -> dict:
    """Create a new part document from the default part template."""

    def _impl():
        app = _connect()
        template = _find_template("part")
        doc = app.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise RuntimeError("Failed to create a new part document.")
        try:
            doc.ShowNamedView2("*Isometric", 7)
            doc.ViewZoomtofit2()
        except Exception:
            pass
        return {"title": _doc_title(doc), "type": "Part"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_new_assembly() -> dict:
    """Create a new assembly document from the default assembly template."""

    def _impl():
        app = _connect()
        template = _find_template("assembly")
        doc = app.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise RuntimeError("Failed to create a new assembly document.")
        return {"title": _doc_title(doc), "type": "Assembly"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_new_drawing() -> dict:
    """Create a new drawing document from the default drawing template."""

    def _impl():
        app = _connect()
        template = _find_template("drawing")
        doc = app.NewDocument(template, 0, 0, 0)
        if doc is None:
            raise RuntimeError("Failed to create a new drawing document.")
        # Verify the created document really is a drawing (type 3). If the
        # template resolved to something wrong, NewDocument silently makes a part.
        active = app.ActiveDoc
        dtype = _doc_type(active) if active is not None else _doc_type(doc)
        if dtype != 3:
            raise RuntimeError(
                "A document was created but it is not a drawing (the drawing template "
                "could not be resolved). Check SolidWorks > Options > Default Templates."
            )
        return {"title": _doc_title(active), "type": "Drawing"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_drawing_view(source_filepath: str, view_type: str = "front",
                               x: float = 150, y: float = 150,
                               scale: float = 1.0, unit: Optional[str] = None) -> dict:
    """Insert a standard model view into the active drawing.

    view_type: front, back, left, right, top, bottom, isometric, trimetric, dimetric.

    x/y is the GEOMETRIC CENTRE of the view on the sheet, not its corner --
    a view placed at (150, 150) extends half its width either side.

    scale is the decimal ratio: 1 for 1:1, 2 for 2:1, 0.05 for 1:20. It is NOT
    the denominator, so scale=20 means 20:1.

    Returns the view's name (needed by every other drawing tool), its real
    outline, size and centre in mm, the sheet format and usable area, and the
    scale SolidWorks ACTUALLY applied -- which can differ from the one asked
    for. `issues` reports a refused scale, a scale that is not on the
    normalised series, and a view that does not fit the usable area, with the
    normalised scale that would fit.

    Call get_drawing_layout first on a sheet that already has views: two
    inserts without explicit positions stack both views on the same centre."""

    def _impl():
        doc = _active_doc()
        if _doc_type(doc) != 3:
            raise RuntimeError("The active document is not a drawing.")
        if not os.path.exists(source_filepath):
            raise FileNotFoundError(f"Source file not found: {source_filepath}")
        if scale <= 0:
            raise ValueError(f"Scale must be positive, got {scale}.")

        view_names = {
            "front": ("*Front", "*Frontal"), "back": ("*Back", "*Posterior"),
            "left": ("*Left", "*Esquerda"), "right": ("*Right", "*Direita"),
            "top": ("*Top", "*Superior"), "bottom": ("*Bottom", "*Inferior"),
            "isometric": ("*Isometric", "*Isométrica"),
            "trimetric": ("*Trimetric", "*Trimétrica"),
            "dimetric": ("*Dimetric", "*Dimétrica"),
        }
        view_candidates = view_names.get(view_type.lower())
        if view_candidates is None:
            raise ValueError(f"Unknown view_type '{view_type}'. Use: {', '.join(view_names)}")

        # CreateDrawViewFromModelView3 requires the referenced model to be LOADED
        # in the SolidWorks session. Open it silently first (it stays hidden), then
        # re-activate the drawing before creating the view.
        app = _connect()
        drawing_title = _doc_title(doc)
        ext = os.path.splitext(source_filepath)[1].lower()
        src_type = {".sldprt": 1, ".sldasm": 2}.get(ext, 1)
        try:
            source_doc, _open_errs, _open_warns = _open_doc6(app, source_filepath, src_type)
        except Exception as e:
            log.warning("Could not pre-load source model: %s", e)
            source_doc = app.GetOpenDocumentByName(source_filepath)
        model_view_names = getattr(source_doc, "GetModelViewNames", ())
        # Dynamic IDispatch exposes this no-argument member as an already
        # evaluated property, while the generated SolidWorks proxy exposes it
        # as a callable method.
        if callable(model_view_names):
            model_view_names = model_view_names()
        available_views = tuple(model_view_names or ())
        sw_view = next((name for name in view_candidates if name in available_views), view_candidates[0])
        active_drawing, react_err = _activate_doc3(app, drawing_title, False)
        if active_drawing is None:
            raise RuntimeError(f"Could not reactivate drawing '{drawing_title}' (error code {react_err}).")
        doc = app.ActiveDoc

        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        model_name = os.path.basename(source_filepath)
        view = doc.CreateDrawViewFromModelView3(model_name, sw_view, x_m, y_m, 0)
        if view is None:
            raise RuntimeError(
                f"Failed to insert {view_type} view of '{model_name}'. "
                "Ensure the source document exists and contains geometry."
            )
        view = win32com.client.Dispatch(view)
        view_name = _view_name(view)

        # The scale assignment used to sit in `try: ... except: pass`, and the
        # return value echoed the REQUESTED scale either way. A refused
        # assignment therefore produced a view at the sheet scale while the
        # tool reported the scale asked for -- and every sheet coordinate
        # computed from that number was wrong. Now the assignment is reported,
        # and what comes back is the scale SolidWorks actually holds.
        scale_applied, scale_error = True, None
        try:
            view.ScaleRatio = (scale, 1.0)
            # The API help for IView.ScaleRatio says to rebuild after changing
            # it; without this the outline read below can still be the old one.
            doc.EditRebuild3()
        except Exception as exc:
            scale_applied, scale_error = False, str(exc)
            log.warning("Could not set the scale of view %r to %s: %s",
                        view_name, scale, exc)

        effective_ratio = _view_scale_ratio(view)
        result = {
            "view_name": view_name,
            "view_type": view_type,
            "source": source_filepath,
            "position": [x, y],
            "requested_scale": scale,
            "effective_scale": ad.format_scale(*effective_ratio),
            "effective_scale_ratio": list(effective_ratio),
            "scale_applied": scale_applied,
            "unit": unit or _default_unit,
            "issues": [],
        }
        if scale_error:
            result["scale_error"] = scale_error
            result["issues"].append(ad.issue(
                "E08", "critical",
                f"the requested scale {scale:g}:1 was refused; the view is at "
                f"{result['effective_scale']}", view_name))
        if not ad.is_normalized_scale(*effective_ratio):
            nearest = ad.nearest_normalized_scale(effective_ratio[0] / effective_ratio[1])
            result["issues"].append(ad.issue(
                "E08", "warning",
                f"{result['effective_scale']} is not on the normalised series; "
                f"nearest is {nearest[0]}:{nearest[1]}", view_name))

        # The outline is what makes a later dimension or view placement
        # meaningful; the old return had no way to tell whether the view even
        # landed on the paper.
        try:
            outline_m = _view_outline_m(view)
            result["outline_mm"] = [ad.mm(c) for c in outline_m]
            result["size_mm"] = [round(s, 3) for s in ad.box_size(result["outline_mm"])]
            result["center_mm"] = [ad.mm(c) for c in _view_position_m(view)]
            sheet = win32com.client.Dispatch(_com_member(doc, "GetCurrentSheet"))
            props = _sheet_properties(sheet)
            width_mm, height_mm = ad.mm(props["width_m"]), ad.mm(props["height_m"])
            config = ad.SheetConfig()
            result["sheet_format"] = ad.identify_format(width_mm, height_mm)
            result["sheet_size_mm"] = [width_mm, height_mm]
            usable = config.usable_area(width_mm, height_mm)
            result["usable_area_mm"] = usable
            if not ad.box_contains(usable, result["outline_mm"]):
                fitting = ad.pick_scale_that_fits(
                    [s / (effective_ratio[0] / effective_ratio[1]) for s in result["size_mm"]],
                    (usable[2] - usable[0], usable[3] - usable[1]),
                    margin_mm=config.dim_band_mm)
                hint = (f"; {fitting[0]}:{fitting[1]} would fit"
                        if fitting else "; no normalised scale fits this sheet")
                result["issues"].append(ad.issue(
                    "E07", "critical",
                    f"view {view_name} is {result['size_mm'][0]:g} x "
                    f"{result['size_mm'][1]:g} mm and does not fit the usable area "
                    f"of the {result['sheet_format'] or 'sheet'}{hint}", view_name))
        except Exception:
            log.warning("Could not measure view %r after insertion", view_name,
                        exc_info=True)
        result["ok"] = not any(i["severity"] == "critical" for i in result["issues"])
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def open_document(filepath: str) -> dict:
    """Open an existing SolidWorks file (.sldprt, .sldasm, or .slddrw)."""

    def _impl():
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        app = _connect()
        ext = os.path.splitext(filepath)[1].lower()
        doc_type = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}.get(ext, 1)
        doc, errors, _warnings = _open_doc6(app, filepath, doc_type)
        if doc is None:
            raise RuntimeError(f"Failed to open '{filepath}' (error code {errors}).")
        title = _doc_title(doc)
        active_doc, activate_errors = _activate_doc3(app, title, True)
        if active_doc is None:
            raise RuntimeError(
                f"Opened '{filepath}' but could not activate it (error code {activate_errors})."
            )
        return {"title": title, "path": filepath}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def close_document(save: bool = False) -> dict:
    """Close the active document, optionally saving it first."""

    def _impl():
        app = _connect()
        doc = app.ActiveDoc
        if doc is None:
            return {"closed": False, "message": "No document was open."}
        title = _doc_title(doc)
        if save:
            saved, errs, _wrns = _save_doc3(doc)
            if not saved or errs != 0:
                raise RuntimeError(f"Failed to save '{title}' before closing (error code {errs}).")
        app.CloseDoc(title)
        return {"closed": True, "title": title}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def save_document(filepath: Optional[str] = None) -> dict:
    """Save the active document. Omit filepath to save in place."""

    def _impl():
        doc = _active_doc()
        if filepath:
            abs_path = _resolve_write_path(filepath)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            ok = False
            try:
                ok = bool(doc.SaveAs(abs_path))
            except Exception:
                ok = False
            if not ok:
                errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                empty_export = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
                ok = bool(doc.Extension.SaveAs(abs_path, 0, 0, empty_export, errors, warnings))
            if not ok:
                raise RuntimeError(f"Failed to save to '{abs_path}'.")
            return {"path": abs_path}
        else:
            saved, errors, _warnings = _save_doc3(doc)
            if not saved or errors != 0:
                raise RuntimeError(f"Save failed (error code {errors}).")
            return {"path": _doc_path(doc)}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_document_info() -> dict:
    """Get the active document's title, path, and document type."""

    def _impl():
        doc = _active_doc()
        type_names = {0: "None", 1: "Part", 2: "Assembly", 3: "Drawing"}
        return {
            "title": _doc_title(doc),
            "path": _doc_path(doc) or None,
            "type": type_names.get(_doc_type(doc), "Unknown"),
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_open_documents() -> dict:
    """List every SolidWorks document currently open."""

    def _impl():
        app = _connect()
        type_names = {1: "Part", 2: "Assembly", 3: "Drawing"}
        docs = []
        open_docs = app.GetDocuments
        if callable(open_docs):
            open_docs = open_docs()
        for doc in open_docs or ():
            try:
                docs.append({"title": _doc_title(doc), "type": type_names.get(_doc_type(doc), "Unknown")})
            except Exception:
                pass
        return {"count": len(docs), "documents": docs}

    return await _run(_impl)


# ===========================================================================
# Assembly tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_component(filepath: str, x: float = 0, y: float = 0, z: float = 0,
                            unit: Optional[str] = None,
                            tolerance: float = 0.01) -> dict:
    """Insert an existing part or sub-assembly file into the active assembly at a position.

    x/y/z place the component's own origin (its sketch/model origin, as seen
    when you open that file alone) at this point in the assembly -- not its
    bounding-box center, which is what the underlying AddComponent5 call does
    natively if not corrected for (see _preload_and_insert_component).

    THE POSITION IS VERIFIED, NOT ASSUMED. After the insert, the component's
    pose is read back out of SolidWorks, so the result carries
    ``actual_position`` (what was measured) beside ``requested_position`` (what
    was asked for) and the ``deviation`` between them. ``verified`` is true
    only when the measured origin landed within ``tolerance`` of the requested
    point. Read it: a false there is a part sitting somewhere other than where
    it was put, caught at insert time instead of in a drawing later.

    ``tolerance`` is in the same unit as x/y/z (default 0.01 mm).

    ``origin_correction`` reports the offset applied to compensate
    AddComponent5's bounding-box-centre placement, and how many solid bodies it
    was measured from -- ``body_count`` above 1 means a weldment or multi-body
    part, whose box is the union of all of them. Anything that could not be
    computed or measured lands in ``warnings`` rather than passing silently."""

    def _impl():
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        assy = _active_assembly()
        active_unit, factor = _unit_factor(unit)
        x_m, y_m, z_m = to_meters(x, unit), to_meters(y, unit), to_meters(z, unit)
        assy, comp, correction = _preload_and_insert_component(assy, filepath, x_m, y_m, z_m)
        name = comp.Name2
        warnings = list(correction.pop("warnings", []))
        result = {
            # name/path/position/unit keep the shape earlier callers read.
            "name": name,
            "path": filepath,
            "position": [x, y, z],
            "unit": active_unit,
            "requested_position": {"x": x, "y": y, "z": z, "unit": active_unit},
            "origin_correction": correction,
        }

        try:
            pose = _component_pose(comp, factor)
        except Exception as exc:
            result["actual_position"] = None
            result["deviation"] = None
            result["verified"] = False
            warnings.append(
                f"the component was inserted but its position could not be read back "
                f"from SolidWorks ({exc}), so this placement is UNVERIFIED -- confirm it "
                f"with get_component_transform or verify_assembly_positions")
            result["warnings"] = warnings
            return result

        measured = dict(pose["translation"], unit=active_unit)
        deviation = dict(_deviation((x, y, z), pose["translation"], tolerance),
                         unit=active_unit)
        result["actual_position"] = measured
        result["rotation_matrix"] = pose["rotation_matrix"]
        result["rotation_convention"] = pose["rotation_convention"]
        result["deviation"] = deviation
        result["verified"] = bool(deviation["within_tolerance"])
        if not deviation["within_tolerance"]:
            warnings.append(
                f"'{name}' landed {deviation['distance']:.4g} {active_unit} away from the "
                f"requested point (dx={deviation['dx']:.4g}, dy={deviation['dy']:.4g}, "
                f"dz={deviation['dz']:.4g} {active_unit}): the component is NOT where it "
                f"was asked to be, do not build on this placement before fixing it")
        result["warnings"] = warnings
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_components(top_level_only: bool = False) -> dict:
    """List the components of the active assembly, at every level by default.

    top_level_only=True returns only the first level, which is what this tool
    used to do unconditionally -- in an assembly built from subassemblies that
    hid most of the parts.

    ``suppressed`` and ``visible`` are independent: a component can be shown
    (not suppressed) but hidden from view, or vice versa. Use ``visible`` to
    know what is actually on screen right now.

    Each entry carries its `level` (0 = top) and `parent`, so the tree can be
    reconstructed. For BOM quantities and grouping use extract_assembly_bom."""

    def _impl():
        assy = _active_assembly()
        components = []
        # IAssemblyDoc.GetComponents(ToplevelOnly): True means first level
        # only. Passing False returns every component at every level, already
        # flattened.
        for raw in assy.GetComponents(bool(top_level_only)) or ():
            comp = win32com.client.Dispatch(raw)
            entry = {
                "name": comp.Name2,
                "path": comp.GetPathName,
                "suppressed": bool(comp.IsSuppressed),
                "fixed": bool(comp.IsFixed),
            }
            try:
                # 1 = swThisConfiguration: visibility as shown right now.
                visibility = comp.GetVisibility(1, None)
                state = visibility[0] if isinstance(visibility, (tuple, list)) else visibility
                entry["visible"] = bool(state)
            except Exception:
                entry["visible"] = None
            for attribute, key in (("ReferencedConfiguration", "configuration"),
                                   ("ExcludeFromBOM", "excluded_from_bom"),
                                   ("IsVirtual", "virtual")):
                try:
                    value = _com_member(comp, attribute)
                    entry[key] = bool(value) if key != "configuration" else (
                        str(value) if value is not None else None)
                except Exception:
                    entry[key] = None
            # Name2 of a nested component is "sub-1/part-1": the path through
            # the tree. Its depth is the level, which GetComponents(False)
            # does not otherwise report.
            entry["level"] = entry["name"].count("/")
            entry["parent"] = (entry["name"].rsplit("/", 1)[0]
                               if "/" in entry["name"] else None)
            components.append(entry)
        return {
            "assembly": _doc_title(_active_doc()),
            "top_level_only": bool(top_level_only),
            "count": len(components),
            "components": components,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def extract_assembly_data(config: str = "") -> dict:
    """Walk every TOP-LEVEL component of the active assembly and return name,
    material/custom properties, and mass/dimensions for each one in a single call.

    SUPERSEDED for BOM work by extract_assembly_bom, which walks every level.
    This tool reads only the first level, so parts inside subassemblies do not
    appear and a subassembly counts as one item. It is kept unchanged because
    callers depend on its exact shape; use it when the first level is really
    what is wanted.

    Built for BOM-style extraction (e.g. an AI pipeline that needs quantity,
    material, weight, and dimensions per part): without this, a caller needs
    list_components plus one get_custom_properties and one measure_body call
    PER component. This collapses that into one round trip and also returns
    ``bom``: the same data grouped by source file with a computed ``quantity``,
    ready to hand to a backend that builds the actual BOM/cut-list document.

    config: configuration name to read custom properties from for every
    component, same meaning as in get_custom_properties; empty string (the
    default) reads document-level properties, not the component's assembly
    configuration. Mass/dimensions are unaffected by this -- they always
    measure the component's current geometry.

    A component that fails to measure or read properties (suppressed, no
    material, lightweight) does not fail the whole call -- its failure is
    recorded in that component's own ``errors`` list instead, so a BOM
    checklist can report exactly which parts are incomplete.

    ``properties["Material"]`` is filled in from the native SolidWorks
    material assignment (set_material) when no custom property named
    "Material" already exists -- the two live in separate places in
    SolidWorks, and a custom property always takes precedence if present.
    """

    def _impl():
        assy = _active_assembly()
        factor = 1.0 / UNIT_TO_METERS.get(_default_unit, 0.001)
        components = []
        for raw in assy.GetComponents(True) or ():
            comp = win32com.client.Dispatch(raw)
            entry = {
                "name": comp.Name2,
                "path": comp.GetPathName,
                "suppressed": bool(comp.IsSuppressed),
                "properties": None,
                "measurement": None,
                "errors": [],
            }

            model_doc = None
            try:
                # Dynamic IDispatch auto-invokes zero-arg methods on attribute
                # access, so comp.GetModelDoc2 (no parens) is ALREADY the
                # resolved IModelDoc2 here -- and since pywin32 wraps every
                # dispatched COM object with a generic __call__, _com_member's
                # callable() check can't tell "unresolved bound method" apart
                # from "already-resolved object that merely supports being
                # called". Try the call (correct under the generated type
                # library, where GetModelDoc2 is a real method); if it raises
                # DISP_E_MEMBERNOTFOUND, the pre-call value was the real
                # object all along, so fall back to it instead of failing.
                raw_doc = comp.GetModelDoc2
                if callable(raw_doc):
                    try:
                        raw_doc = raw_doc()
                    except Exception:
                        pass
                if raw_doc is not None:
                    model_doc = win32com.client.Dispatch(raw_doc)
            except Exception as exc:
                entry["errors"].append(f"could not open component document: {exc}")

            if model_doc is None:
                if not entry["suppressed"]:
                    entry["errors"].append(
                        "component document is not loaded (suppressed or lightweight)"
                    )
                components.append(entry)
                continue

            try:
                # Match get_custom_properties' own default exactly: empty
                # config reads document-level properties, not the component's
                # referenced configuration. A part set up with set_custom_property
                # (document-level, same as this tool's own default) would
                # otherwise come back empty here even though the data exists --
                # confirmed by testing against a live part built that way.
                cpm = model_doc.Extension.CustomPropertyManager(config)
                entry["properties"] = _read_custom_properties(cpm)
            except Exception as exc:
                entry["errors"].append(f"could not read custom properties: {exc}")

            # Material commonly lives in the native SolidWorks material slot
            # (set_material), not as a custom property -- only use it as a
            # fallback so an explicit "Material" custom property always wins.
            if entry["properties"] is not None and not any(
                k.lower() == "material" for k in entry["properties"]
            ):
                native_material = _native_material_name(model_doc, config)
                if native_material:
                    entry["properties"]["Material"] = {
                        "value": native_material,
                        "resolved": native_material,
                    }

            try:
                entry["measurement"] = _measure_model_doc(model_doc, factor)
            except Exception as exc:
                entry["errors"].append(f"could not measure body: {exc}")

            components.append(entry)

        bom: dict = {}
        order = []
        for entry in components:
            key = entry["path"] or entry["name"]
            if key not in bom:
                bom[key] = {
                    "path": entry["path"],
                    "representative_name": entry["name"],
                    "quantity": 0,
                    "properties": entry["properties"],
                    "measurement": entry["measurement"],
                    "instance_names": [],
                }
                order.append(key)
            row = bom[key]
            row["quantity"] += 1
            row["instance_names"].append(entry["name"])
            if row["properties"] is None and entry["properties"]:
                row["properties"] = entry["properties"]
            if row["measurement"] is None and entry["measurement"]:
                row["measurement"] = entry["measurement"]

        return {
            "assembly": _doc_title(_active_doc()),
            "component_count": len(components),
            "unique_part_count": len(order),
            "components": components,
            "bom": [bom[k] for k in order],
        }

    return await _run(_impl)


def _component_flags(comp) -> dict:
    """Read the flags that decide whether a component belongs in a BOM."""
    flags = {}
    for attribute, key in (("IsSuppressed", "suppressed"),
                           ("ExcludeFromBOM", "excluded_from_bom"),
                           ("IsVirtual", "virtual"),
                           ("ReferencedConfiguration", "configuration"),
                           ("GetPathName", "path"),
                           ("Name2", "name")):
        try:
            value = _com_member(comp, attribute)
        except Exception:
            value = None
        if key in ("configuration", "path", "name"):
            flags[key] = str(value) if value is not None else None
        else:
            flags[key] = bool(value)
    return flags


def _component_model_doc(comp):
    """The IModelDoc2 behind a component, or None if it is not loaded.

    Dynamic IDispatch auto-invokes zero-argument methods on attribute access,
    so comp.GetModelDoc2 is often ALREADY the resolved document; pywin32 also
    makes every dispatched object callable, so callable() cannot tell the two
    cases apart. Try the call, fall back to the value.
    """
    try:
        raw = comp.GetModelDoc2
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                pass
        return win32com.client.Dispatch(raw) if raw is not None else None
    except Exception:
        return None


def _is_assembly_component(comp) -> bool:
    model = _component_model_doc(comp)
    if model is None:
        return False
    try:
        return _doc_type(model) == 2
    except Exception:
        return False


def _walk_assembly(comp, level: int, parent: Optional[str], out: list,
                   max_depth: int, visited: set) -> None:
    """Depth-first walk of an assembly tree, appending one entry per instance.

    Every physical instance appears once, so counting instances gives the
    right BOM quantity with no need to multiply by a parent quantity: a
    subassembly used twice is walked twice.

    ``visited`` guards against a circular reference, which SolidWorks allows
    to exist in a broken assembly and which would otherwise recurse forever.
    """
    flags = _component_flags(comp)
    identity = (flags["path"], flags["configuration"], flags["name"], level)
    if identity in visited:
        return
    visited.add(identity)

    is_assembly = _is_assembly_component(comp)
    entry = dict(flags, level=level, parent=parent, is_assembly=is_assembly,
                 component=comp)
    out.append(entry)

    if not is_assembly or flags["suppressed"] or level >= max_depth:
        return
    try:
        children = _com_member(comp, "GetChildren") or ()
    except Exception:
        log.debug("Could not read children of %r", flags["name"], exc_info=True)
        return
    for raw_child in children:
        try:
            child = win32com.client.Dispatch(raw_child)
        except Exception:
            continue
        _walk_assembly(child, level + 1, flags["name"], out, max_depth, visited)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def extract_assembly_bom(config: str = "",
                               include_subassemblies: bool = True,
                               include_suppressed: bool = False,
                               max_depth: int = 10) -> dict:
    """Build a full multi-level BOM of the active assembly in one call.

    Walks EVERY level of the assembly tree, not just the first, and groups
    rows by (file path, referenced configuration) -- so the same plate in a
    2 mm and a 3 mm configuration becomes two rows instead of being silently
    summed into one.

    Each BOM row carries quantity, material, custom properties, mass and
    bounding box. Each tree entry carries its level and parent, so a
    per-subassembly BOM can be built from the same result.

    include_subassemblies: list the subassembly itself as its own row, as well
        as its contents. Structural work usually wants this (the subassembly
        is a weldment that gets a code of its own); turn it off for a pure
        parts list.
    include_suppressed: suppressed components are left out by default, because
        a suppressed part is not in the physical build.
    Components marked "Exclude from bill of materials" are always left out.
    config: configuration to read custom properties from; empty (the default)
        reads document-level properties.

    Use this instead of extract_assembly_data for anything that feeds a BOM,
    a cut list or a weight total."""

    def _impl():
        assy = _active_assembly()
        factor = 1.0 / UNIT_TO_METERS.get(_default_unit, 0.001)

        tree: list = []
        visited: set = set()
        for raw in assy.GetComponents(True) or ():
            try:
                comp = win32com.client.Dispatch(raw)
            except Exception:
                continue
            _walk_assembly(comp, 0, None, tree, max_depth, visited)

        rows: dict = {}
        order: list = []
        entries: list = []
        skipped = {"suppressed": 0, "excluded_from_bom": 0, "not_loaded": 0}

        for node in tree:
            comp = node.pop("component")
            entry = dict(node, properties=None, measurement=None, errors=[])
            entries.append(entry)

            if entry["excluded_from_bom"]:
                skipped["excluded_from_bom"] += 1
                entry["counted"] = False
                continue
            if entry["suppressed"] and not include_suppressed:
                skipped["suppressed"] += 1
                entry["counted"] = False
                continue
            if entry["is_assembly"] and not include_subassemblies:
                entry["counted"] = False
                continue

            model_doc = _component_model_doc(comp)
            if model_doc is None:
                entry["errors"].append(
                    "component document is not loaded (suppressed or lightweight)")
                skipped["not_loaded"] += 1
                entry["counted"] = False
                continue

            try:
                cpm = model_doc.Extension.CustomPropertyManager(config)
                entry["properties"] = _read_custom_properties(cpm)
            except Exception as exc:
                entry["errors"].append(f"could not read custom properties: {exc}")

            if entry["properties"] is not None and not any(
                k.lower() == "material" for k in entry["properties"]
            ):
                native_material = _native_material_name(model_doc, config)
                if native_material:
                    entry["properties"]["Material"] = {
                        "value": native_material,
                        "resolved": native_material,
                    }

            if not entry["is_assembly"]:
                try:
                    entry["measurement"] = _measure_model_doc(model_doc, factor)
                except Exception as exc:
                    entry["errors"].append(f"could not measure body: {exc}")

            entry["counted"] = True
            # The grouping key the old code got wrong: it keyed on the file
            # path alone, so two configurations of one file were summed as a
            # single item.
            key = (entry["path"] or entry["name"], entry["configuration"] or "")
            if key not in rows:
                rows[key] = {
                    "path": entry["path"],
                    "configuration": entry["configuration"],
                    "representative_name": entry["name"],
                    "is_assembly": entry["is_assembly"],
                    "min_level": entry["level"],
                    "quantity": 0,
                    "properties": entry["properties"],
                    "measurement": entry["measurement"],
                    "instance_names": [],
                }
                order.append(key)
            row = rows[key]
            row["quantity"] += 1
            row["instance_names"].append(entry["name"])
            row["min_level"] = min(row["min_level"], entry["level"])
            if row["properties"] is None and entry["properties"]:
                row["properties"] = entry["properties"]
            if row["measurement"] is None and entry["measurement"]:
                row["measurement"] = entry["measurement"]

        bom = [rows[k] for k in order]
        total_mass = 0.0
        mass_incomplete = []
        for row in bom:
            if row["is_assembly"]:
                continue
            mass = (row.get("measurement") or {}).get("mass_kg")
            if mass is None:
                mass_incomplete.append(row["representative_name"])
            else:
                total_mass += mass * row["quantity"]

        return {
            "assembly": _doc_title(_active_doc()),
            "levels_walked": max((e["level"] for e in entries), default=0) + 1,
            "instance_count": len(entries),
            "counted_instance_count": sum(1 for e in entries if e.get("counted")),
            "unique_row_count": len(bom),
            "skipped": skipped,
            "total_mass_kg": round(total_mass, 4),
            "mass_incomplete_for": mass_incomplete,
            "bom": bom,
            "tree": entries,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_component_transform(name: str, unit: Optional[str] = None) -> dict:
    """Read an assembly component's exact position and orientation.

    Returns translation in ``unit`` (mm by default), the 3x3 rotation matrix,
    and whether the component is fixed. Use this before changing an assembly
    pose so a placement can be checked or restored deterministically -- and
    after every mate, to see where the component actually ended up rather than
    where it was meant to go.

    ``rotation_matrix`` is ROW-major and says so in ``rotation_convention``:
    a point (lx,ly,lz) in the component's own system becomes
    global.x = r00*lx + r01*ly + r02*lz, global.y = r10*lx + r11*ly + r12*lz,
    global.z = r20*lx + r21*ly + r22*lz, plus the translation. Confirmed live
    against a known face (.claude/knowledge/montagens_mecanicas_reais.md).
    Do not guess the other convention: it produces a plausible but wrong
    global point that still manages to select *a* face, so a caller who then
    adjusts the point until the selection works has confirmed the wrong
    convention instead of finding the right one.
    """

    def _impl():
        assy = _active_assembly()
        component = _find_component(assy, name)
        if component is None:
            raise RuntimeError(f"Component '{name}' not found in the assembly.")
        active_unit, factor = _unit_factor(unit)
        try:
            pose = _component_pose(component, factor)
        except Exception as exc:
            raise RuntimeError(f"Component '{name}' returned an invalid transform: {exc}") from exc
        return {
            "name": name,
            "translation": dict(pose["translation"], unit=active_unit),
            "rotation_matrix": pose["rotation_matrix"],
            "rotation_convention": pose["rotation_convention"],
            "fixed": bool(component.IsFixed),
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_component_transform(
    name: str,
    x: float = 0,
    y: float = 0,
    z: float = 0,
    rotation_x: float = 0,
    rotation_y: float = 0,
    rotation_z: float = 0,
    unit: Optional[str] = None,
    fix_after: bool = False,
    tolerance: float = 0.01,
) -> dict:
    """Set a component's absolute assembly position and Euler rotation.

    Translations use ``unit`` (mm by default); rotations are degrees. The
    rotation is composed X then Y then Z, and is applied as an absolute pose,
    not an incremental drag. Fixed components are floated automatically before
    the transform is applied. Set ``fix_after`` to lock the verified pose.

    THE RESULT IS MEASURED, NOT ECHOED. After the transform is applied and the
    assembly rebuilt, the pose is read back out of SolidWorks:
    ``actual_position`` and ``actual_rotation_matrix`` are what SolidWorks
    reports, ``deviation`` is the distance from the requested point, and
    ``verified`` is true only when both the position landed within
    ``tolerance`` and the rotation matrix came back as the one that was sent.
    A component that an existing mate drags elsewhere the moment the rebuild
    solves shows up here as a non-zero deviation instead of a clean-looking
    success.

    ``tolerance`` is in the same unit as x/y/z (default 0.01 mm). Both matrices
    in the result follow the convention named in ``rotation_convention``
    (row-major, the same one get_component_transform reports). Orientation is
    only confirmed here as a round-trip of that matrix; whether it produces
    the intended orientation of real geometry is a separate question, answered
    by looking at a view aligned to the axis in question, not by the
    round-trip (see .claude/knowledge/montagens_mecanicas_reais.md).
    """

    def _impl():
        assy = _active_assembly()
        component = _find_component(assy, name)
        if component is None:
            raise RuntimeError(f"Component '{name}' not found in the assembly.")

        was_fixed = bool(component.IsFixed)
        if was_fixed:
            _select_component(assy, name)
            assy.UnfixComponent()

        rx, ry, rz = (math.radians(value) for value in (rotation_x, rotation_y, rotation_z))
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        # Rz * Ry * Rx: the convention documented by this tool.
        rotation = (
            cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx,
            sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx,
            -sy, cy * sx, cy * cx,
        )
        transform_data = rotation + (
            to_meters(x, unit), to_meters(y, unit), to_meters(z, unit),
            1.0, 0.0, 0.0, 0.0,
        )
        transform_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, transform_data,
        )
        # The installed dynamic SolidWorks 2025 proxy does not expose
        # IMathUtility.CreateTransform.  Every component already owns a valid
        # IMathTransform, however, and ArrayData is the documented mutable
        # representation of the 4x4 pose matrix.
        transform = component.Transform2
        transform.ArrayData = transform_variant
        component.Transform2 = transform
        rebuild = assy.EditRebuild3
        if callable(rebuild):
            rebuild()

        if fix_after:
            _select_component(assy, name)
            assy.FixComponent()

        active_unit, factor = _unit_factor(unit)
        result = {
            "name": name,
            # translation/rotation_degrees keep the shape earlier callers read:
            # they are the REQUESTED pose. actual_* below is the measured one.
            "translation": {"x": x, "y": y, "z": z, "unit": active_unit},
            "rotation_degrees": {"x": rotation_x, "y": rotation_y, "z": rotation_z},
            "requested_position": {"x": x, "y": y, "z": z, "unit": active_unit},
            "fixed": bool(fix_after),
            "previously_fixed": was_fixed,
        }
        warnings = []

        # Read the pose back instead of trusting that the write took: an
        # existing mate can drag the component somewhere else as soon as the
        # rebuild solves, and SolidWorks reports no error when it does.
        try:
            applied = _component_pose(component, factor)
        except Exception as exc:
            result["actual_position"] = None
            result["actual_rotation_matrix"] = None
            result["deviation"] = None
            result["verified"] = False
            warnings.append(
                f"the transform was applied but the resulting pose could not be read "
                f"back from SolidWorks ({exc}), so it is UNVERIFIED")
            result["warnings"] = warnings
            return result

        deviation = dict(_deviation((x, y, z), applied["translation"], tolerance),
                         unit=active_unit)
        intended = [list(rotation[0:3]), list(rotation[3:6]), list(rotation[6:9])]
        rotation_error = max(
            abs(sent - read)
            for sent_row, read_row in zip(intended, applied["rotation_matrix"])
            for sent, read in zip(sent_row, read_row)
        )
        result["actual_position"] = dict(applied["translation"], unit=active_unit)
        result["actual_rotation_matrix"] = applied["rotation_matrix"]
        result["rotation_convention"] = applied["rotation_convention"]
        result["requested_rotation_matrix"] = intended
        result["rotation_max_elementwise_error"] = rotation_error
        result["deviation"] = deviation
        result["verified"] = bool(deviation["within_tolerance"] and rotation_error <= 1e-6)

        if not deviation["within_tolerance"]:
            warnings.append(
                f"'{name}' ended up {deviation['distance']:.4g} {active_unit} from the "
                f"requested point (dx={deviation['dx']:.4g}, dy={deviation['dy']:.4g}, "
                f"dz={deviation['dz']:.4g} {active_unit}) -- an existing mate is most "
                f"likely overriding this placement; check list_mates for '{name}'")
        if rotation_error > 1e-6:
            warnings.append(
                f"the rotation SolidWorks reports back differs from the one sent by "
                f"{rotation_error:.3g} at worst per element: the orientation of '{name}' "
                f"is not the one requested")
        result["warnings"] = warnings
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def capture_standard_views(output_dir: str = "tests/output", prefix: str = "model") -> dict:
    """Export front, back, top, bottom, and isometric PNG views of the active model.

    Use this immediately after positioning components. The tool restores the
    isometric camera after exporting, returns every image path, and fails if a
    requested image is not written by SolidWorks.
    """

    def _impl():
        if not prefix or any(char in prefix for char in '\\/:*?"<>|'):
            raise ValueError("prefix must be a non-empty filename stem without path characters.")
        doc = _active_doc()
        target_dir = _resolve_write_path(output_dir)
        os.makedirs(target_dir, exist_ok=True)
        views = (
            ("front", "*Front", 1), ("back", "*Back", 2),
            ("top", "*Top", 5), ("bottom", "*Bottom", 6),
            ("isometric", "*Isometric", 7),
        )
        paths = {}
        for label, view_name, view_id in views:
            path = os.path.join(target_dir, f"{prefix}_{label}.png")
            doc.ShowNamedView2(view_name, view_id)
            doc.ViewZoomtofit2()
            doc.SaveAs3(path, 0, 0)
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                raise RuntimeError(f"SolidWorks did not export the {label} view to '{path}'.")
            paths[label] = path
        doc.ShowNamedView2("*Isometric", 7)
        doc.ViewZoomtofit2()
        return {"views": paths, "restored_view": "isometric"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def fix_component(name: str) -> dict:
    """Fix a component in place (removes its remaining degrees of freedom)."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        assy.FixComponent()
        return {"name": name, "fixed": True}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def float_component(name: str) -> dict:
    """Float a previously fixed component, giving it back its degrees of freedom."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        assy.UnfixComponent()
        return {"name": name, "fixed": False}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_component(name: str) -> dict:
    """Remove a component from the active assembly."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        if not assy.Extension.DeleteSelection2(0):
            raise RuntimeError(f"Failed to delete component '{name}'.")
        return {"deleted": name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def suppress_component(name: str) -> dict:
    """Suppress a component in the active assembly (hides it and excludes from calculations)."""

    def _impl():
        assy = _active_assembly()
        component = _find_component(assy, name)
        if component is None:
            raise RuntimeError(f"Component '{name}' not found in the assembly.")
        status = component.SetSuppression2(0)  # swComponentSuppressed
        # SetSuppression2 returns swSuppressionChangeOk (2) on success.
        if status not in (2, None):
            raise RuntimeError(f"Failed to suppress component '{name}' (status {status}).")
        return {"name": name, "suppressed": True}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def unsuppress_component(name: str) -> dict:
    """Unsuppress a previously suppressed component in the active assembly."""

    def _impl():
        assy = _active_assembly()
        component = _find_component(assy, name)
        if component is None:
            raise RuntimeError(f"Component '{name}' not found in the assembly.")
        status = component.SetSuppression2(2)  # swComponentResolved
        # SetSuppression2 returns swSuppressionChangeOk (2) on success.
        if status not in (2, None):
            raise RuntimeError(f"Failed to unsuppress component '{name}' (status {status}).")
        return {"name": name, "suppressed": False}

    return await _run(_impl)


# What each supported mate type does NOT constrain. Every one of them leaves
# something open, so a mate that reports success has not necessarily LOCATED
# anything -- the classic case being a concentric mate on a shaft, which holds
# the axis and lets the part slide anywhere along it. Reported as
# degrees_of_freedom_left so the caller reads it instead of assuming.
MATE_DOF_LEFT = {
    "coincident": ("the two translations within the plane and the spin about the plane "
                   "normal are still free"),
    "concentric": ("sliding along the shared axis and spinning about it are both still "
                   "free -- a concentric mate does NOT fix axial position, so the part "
                   "can sit anywhere along its hole and still look correctly mated in "
                   "an isometric view"),
    "perpendicular": "all three translations are still free -- this fixes direction only",
    "parallel": "all three translations are still free -- this fixes direction only",
    "tangent": "sliding along the contact and the spin about it are still free",
}

# swMateAlign_e (confirmed against SolidWorks API documentation and VBA
# examples): ALIGNED=0, ANTI_ALIGNED=1, CLOSEST=2. AddMate5's second
# parameter takes one of these -- it was previously hardcoded to 0
# (ALIGNED) on every call, regardless of the actual geometry.
#
# That hardcoding is a second, distinct cause of a part landing "torta"
# (tilted, or seated through a crooked gap) even when the FACE SELECTION
# itself is correct: forcing ALIGNED makes SolidWorks solve for the two
# faces' reference directions pointing the SAME way, which is the physically
# wrong solve for a mate whose faces are meant to face each other (the usual
# case for two flat faces pressed together, or a shaft seated against a
# shoulder) -- the solver still reports success, because ALIGNED is a valid
# solution, just not the one that produces a flush fit. CLOSEST lets
# SolidWorks pick whichever of the two solutions keeps the components near
# their CURRENT relative pose instead of always forcing one side, which
# removes that systematic bias; an explicit 'aligned'/'anti_aligned' is still
# available for a caller that already knows which one the geometry needs.
MATE_ALIGN = {"aligned": 0, "anti_aligned": 1, "closest": 2}


def _entity_direction_local(entity):
    """A single direction vector for a FACE, in its OWN part's coordinates:
    the face normal for a planar face, the axis for a cylindrical one.

    Returns (kind, (x,y,z)) or (None, None) for a face this reduces to no
    single direction (anything but plane/cylinder) -- there is no
    "torta" check to run on a direction that doesn't exist.
    """
    try:
        surface = win32com.client.Dispatch(entity.GetSurface)
    except Exception:
        return None, None
    try:
        is_planar = surface.IsPlane
        if callable(is_planar):
            is_planar = is_planar()
        if is_planar:
            normal = tuple(_com_member(entity, "Normal"))
            if len(normal) >= 3 and any(normal[:3]):
                return "planar_normal", tuple(normal[:3])
    except Exception:
        pass
    try:
        is_cylinder = surface.IsCylinder
        if callable(is_cylinder):
            is_cylinder = is_cylinder()
        if is_cylinder:
            params = tuple(_com_member(surface, "CylinderParams"))
            if len(params) >= 7 and any(params[3:6]):
                return "cylindrical_axis", tuple(params[3:6])
    except Exception:
        pass
    return None, None


def _rotate_direction(direction, rotation_matrix):
    """Apply a row-major 3x3 rotation to a direction (no translation)."""
    r = rotation_matrix
    return (
        r[0][0] * direction[0] + r[0][1] * direction[1] + r[0][2] * direction[2],
        r[1][0] * direction[0] + r[1][1] * direction[1] + r[1][2] * direction[2],
        r[2][0] * direction[0] + r[2][1] * direction[1] + r[2][2] * direction[2],
    )


def _angle_between_degrees(a, b) -> Optional[float]:
    """Angle between two direction vectors, or None if either is ~zero-length."""
    norm_a = math.sqrt(sum(v * v for v in a))
    norm_b = math.sqrt(sum(v * v for v in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return None
    cosine = sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
    cosine = max(-1.0, min(1.0, cosine))
    return math.degrees(math.acos(cosine))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_mate(mate_type: str, point1: dict, point2: dict,
                   align: str = "closest") -> dict:
    """Mate two faces/planes, each chosen by a point that
    lies on it, e.g. point1={"x":0,"y":0,"z":0,"unit":"mm"}. Supported mate_type
    values: 'coincident', 'concentric', 'parallel', 'perpendicular', 'tangent'.

    THE EFFECT IS MEASURED, NOT ASSUMED. The two components owning the picked
    faces are identified, and their positions are read out of SolidWorks before
    and after the mate: ``components`` carries, per component,
    ``position_before``, ``position_after`` and ``moved`` (how far the mate
    actually dragged it). A mate that solves to somewhere other than intended
    is visible right here, instead of being discovered later in a view.

    ``align`` -- 'closest' (default), 'aligned' or 'anti_aligned' -- is
    swMateAlign_e. EVERY call used to force 'aligned' regardless of the
    geometry, which is a second, separate way a part ends up tilted or seated
    through a crooked gap even when the face selection itself was right:
    forcing the two faces' reference directions to point the SAME way is the
    wrong solve for the common case of two faces meant to face each other (two
    flat faces pressed flush, a shaft against a shoulder) -- SolidWorks still
    reports success, because 'aligned' is a mathematically valid solution,
    just not the flush one. 'closest' removes that bias by picking whichever
    solution keeps the components near where they already were; pass
    'aligned' or 'anti_aligned' only when the geometry's correct orientation
    is already known.

    ``geometry_check`` reports the MEASURED angle, in the assembly's global
    coordinates, between the two picked faces' reference directions (the
    normal for a planar face, the axis for a cylindrical one) after the mate
    solved -- a number to read instead of judging "crooked" from a picture.
    It does not say what that angle SHOULD be (this tool has no way to know
    the intended fit), so a caller that knows the expected angle for this
    joint (0, 90 or 180 degrees are the common cases) should compare it
    itself; ``kind`` is None when neither face reduces to a single direction
    (anything but a plane or a cylinder), which is reported rather than
    silently skipped.

    ``degrees_of_freedom_left`` says what this mate type does NOT constrain.
    Read it before treating the component as located: every supported type
    leaves something free, and the position reported above is simply where the
    part sits now -- not a position the mate holds. For a concentric mate in
    particular, the axial position is NOT fixed; constrain the remaining
    direction with a second mate, or fix_component, before building on it.

    TWO LIMITS THAT NEITHER OF THOSE FIELDS CAN REPORT.

    It resolves each point through SelectByID2, which picks from the current
    CAMERA. A face hidden behind the model cannot be reached at all, whatever
    coordinate is passed -- so an internal face, such as a piston's pin-boss
    bore inside the skirt, is unreachable from any orientation, and the call
    either fails or silently grabs the outer face in front of it. Use
    list_faces on the part first to get a pick point that really lies on the
    face wanted, and set_view('isometric') before picking; that only helps for
    a convex part. When the two faces are already coaxial, the near part can
    occlude the far one's bore from every angle, and no pick point exists.

    A 'concentric' mate removes only two degrees of freedom -- it aligns the
    axes and leaves sliding along the shared axis free -- so the part stops
    wherever the solver left it. The ``components`` field above now reports
    where that was, but it is still only one of infinitely many valid
    positions: check it with get_component_transform against the geometry that
    is supposed to limit it BEFORE fix_component, and see
    .claude/knowledge/montagens_mecanicas_reais.md."""

    def _impl():
        assy = _active_assembly()
        types = {"coincident": 0, "concentric": 1, "perpendicular": 2, "parallel": 3, "tangent": 4}
        mate_code = types.get(mate_type.lower())
        if mate_code is None:
            raise ValueError(f"Unknown mate_type '{mate_type}'. Use one of: {', '.join(types)}")
        align_code = MATE_ALIGN.get(align.lower())
        if align_code is None:
            raise ValueError(f"Unknown align '{align}'. Use one of: {', '.join(MATE_ALIGN)}")

        def pt(p):
            u = p.get("unit")
            return to_meters(p["x"], u), to_meters(p["y"], u), to_meters(p["z"], u)

        x1, y1, z1 = pt(point1)
        x2, y2, z2 = pt(point2)
        active_unit, factor = _unit_factor(point1.get("unit"))

        assy.ClearSelection2(True)
        empty_callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not assy.Extension.SelectByID2("", "FACE", x1, y1, z1, False, 1, empty_callout, 0):
            raise RuntimeError(f"No face/plane found at point1 {point1}.")
        if not assy.Extension.SelectByID2("", "FACE", x2, y2, z2, True, 1, empty_callout, 0):
            raise RuntimeError(f"No face/plane found at point2 {point2}.")

        # Hold on to the components owning the two picked faces, AND the two
        # picked faces themselves (for the direction check below), all taken
        # from the selection before AddMate5 consumes it. The IComponent2
        # objects stay valid across AddMate5, so the same objects give the
        # before and the after pose.
        picked = []
        local_directions = {}
        for index in (1, 2):
            try:
                raw_component = assy.SelectionManager.GetSelectedObjectsComponent4(index, -1)
                if raw_component is None:
                    continue
                component = win32com.client.Dispatch(raw_component)
                name = str(component.Name2)
                picked.append((name, component))
            except Exception:
                continue
            try:
                raw_entity = assy.SelectionManager.GetSelectedObject6(index, -1)
                kind, direction = _entity_direction_local(win32com.client.Dispatch(raw_entity))
                if kind is not None:
                    local_directions[index] = (kind, direction)
            except Exception:
                continue
        before = {}
        for name, component in picked:
            try:
                before[name] = _component_pose(component, factor)
            except Exception:
                before[name] = None

        mate_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        mate = assy.AddMate5(
            mate_code, align_code, False,
            0.0, 0.0, 0.0,
            0.0, 0.0,
            0.0, 0.0, 0.0,
            False, False, 0, mate_err,
        )
        # swAddMateError_NoError is 1; COM can return an object even on failure.
        if mate is None or mate_err.value != 1:
            raise RuntimeError(f"Mate creation failed (error code {mate_err.value}).")

        # Geometry check: the two faces' reference directions, rotated into
        # the ASSEMBLY's coordinates using each owning component's pose AFTER
        # the mate solved, then the angle between them. Needs both directions
        # and both components resolved; anything missing is named in `kind`
        # rather than silently producing no result.
        geometry_check = {"kind": None, "angle_degrees": None, "note": None}
        if len(picked) == 2 and set(local_directions) == {1, 2}:
            try:
                global_directions = []
                for index, (name, component) in zip((1, 2), picked):
                    kind, local_dir = local_directions[index]
                    pose_now = _component_pose(component, 1.0)  # direction is unit-less
                    global_directions.append(
                        _rotate_direction(local_dir, pose_now["rotation_matrix"]))
                angle = _angle_between_degrees(*global_directions)
                kinds = {local_directions[1][0], local_directions[2][0]}
                geometry_check["kind"] = (
                    "planar_normals" if kinds == {"planar_normal"} else
                    "cylindrical_axes" if kinds == {"cylindrical_axis"} else
                    "mixed")
                geometry_check["angle_degrees"] = angle
                geometry_check["note"] = (
                    "angle between the two faces' reference directions in assembly "
                    "coordinates -- 0 or 180 degrees means parallel/anti-parallel "
                    "(flush or facing-away), 90 means perpendicular; this tool does "
                    "not know which one this joint should be")
            except Exception as exc:
                geometry_check["note"] = f"could not be computed ({exc})"
        else:
            geometry_check["note"] = (
                "at least one picked face is neither planar nor cylindrical (or its "
                "owning component could not be resolved), so it reduces to no single "
                "direction to check")

        warnings = []
        components = []
        for name, component in picked:
            entry = {"name": name}
            start = before.get(name)
            entry["position_before"] = (dict(start["translation"], unit=active_unit)
                                        if start else None)
            try:
                end = _component_pose(component, factor)
            except Exception as exc:
                entry["position_after"] = None
                entry["moved"] = None
                components.append(entry)
                warnings.append(
                    f"the mate was created but '{name}' position could not be read back "
                    f"({exc}), so its resulting position is UNVERIFIED")
                continue
            entry["position_after"] = dict(end["translation"], unit=active_unit)
            entry["rotation_matrix_after"] = end["rotation_matrix"]
            entry["rotation_convention"] = end["rotation_convention"]
            if start:
                delta = _deviation(
                    (start["translation"]["x"], start["translation"]["y"],
                     start["translation"]["z"]),
                    end["translation"], 0.0)
                entry["moved"] = {
                    "dx": delta["dx"], "dy": delta["dy"], "dz": delta["dz"],
                    "distance": delta["distance"], "unit": active_unit,
                }
            else:
                entry["moved"] = None
            components.append(entry)

        if not picked:
            warnings.append(
                "the mate was created but neither picked face could be traced back to a "
                "component, so no position was verified -- check the result with "
                "verify_assembly_positions")
        dof_left = MATE_DOF_LEFT.get(mate_type.lower())
        if dof_left:
            warnings.append(
                f"a '{mate_type}' mate does not fully locate anything: {dof_left}. The "
                f"positions above are where the components sit now, not positions this "
                f"mate holds -- constrain the remaining direction with another mate, or "
                f"fix_component, before building on them")
        angle = geometry_check.get("angle_degrees")
        if angle is not None and 1e-3 < angle < 179.999:
            warnings.append(
                f"the two faces' reference directions are {angle:.3g} degrees apart in "
                f"assembly coordinates, neither parallel (0) nor anti-parallel (180): if "
                f"this joint is meant to be flush or axis-aligned, it is NOT -- it is "
                f"sitting at an angle. Try align='aligned' or align='anti_aligned' "
                f"instead of 'closest', or re-check which faces were picked")
        return {
            "mate_type": mate_type,
            "align": align.lower(),
            "error_code": mate_err.value,
            "unit": active_unit,
            "components": components,
            "geometry_check": geometry_check,
            "degrees_of_freedom_left": dof_left,
            "warnings": warnings,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_mates() -> dict:
    """List every mate (constraint) in the active assembly."""

    def _impl():
        assy = _active_assembly()
        mates = []
        for raw_feature in assy.FeatureManager.GetFeatures(False) or ():
            feat = win32com.client.Dispatch(raw_feature)
            try:
                feature_type = feat.GetTypeName2
                if not str(feature_type).startswith("Mate") or feature_type == "MateGroup":
                    continue
                suppressed = feat.IsSuppressed
                if callable(suppressed):
                    suppressed = suppressed()
                mates.append({
                    "name": feat.Name,
                    "type": feature_type,
                    "suppressed": bool(suppressed),
                })
            except Exception:
                pass
        return {"count": len(mates), "mates": mates}

    return await _run(_impl)


def _mated_component_names(assy) -> tuple:
    """Names of every component taking part in at least one mate.

    Returns (names, complete). ``complete`` is False when the mate entities
    could not be read, and in that case the caller must NOT conclude that a
    component is unmated -- it only means this particular check could not run,
    which is reported rather than turned into a false finding.
    """
    names = set()
    complete = True
    try:
        features = assy.FeatureManager.GetFeatures(False) or ()
    except Exception:
        return names, False
    for raw_feature in features:
        feat = win32com.client.Dispatch(raw_feature)
        try:
            type_name = str(feat.GetTypeName2)
        except Exception:
            complete = False
            continue
        if not type_name.startswith("Mate") or type_name == "MateGroup":
            continue
        try:
            mate = _com_member(feat, "GetSpecificFeature2")
            if mate is None:
                complete = False
                continue
            mate = win32com.client.Dispatch(mate)
            count = _com_member(mate, "GetMateEntityCount")
            for index in range(int(count or 0)):
                entity = win32com.client.Dispatch(mate.MateEntity(index))
                component = _com_member(entity, "ReferenceComponent")
                if component is not None:
                    names.add(str(win32com.client.Dispatch(component).Name2))
        except Exception:
            complete = False
    return names, complete


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def verify_assembly_positions(tolerance: float = 0.01,
                                    unit: Optional[str] = None,
                                    require_constrained: bool = True,
                                    top_level_only: bool = True,
                                    moving_components: Optional[list] = None) -> dict:
    """Check where every component of the active assembly ACTUALLY is. Read-only.

    The assembly-side counterpart of verify_drawing, and the gate to run before
    telling anyone an assembly is built: a part in the wrong place produces a
    perfectly valid drawing of the wrong assembly, so position has to be
    checkable on its own terms.

    Every component's pose is read out of SolidWorks and returned, then checked
    for the failures that put a part somewhere other than where it was placed:

      M01  neither fixed nor mated -- nothing holds it, so its position is not
           reproducible and the next rebuild may move it (skip with
           require_constrained=False)
      M02  sitting exactly on the assembly origin, which is what a placement
           that did not take looks like
      M03  two or more components sharing one position, i.e. parts stacked on
           top of each other instead of spread where they belong
      M04  fixed and mated at once -- over-constrained, and the mate silently
           loses to the fix
      M05  position could not be read at all (suppressed, lightweight, not
           loaded)
      M06  a component named in ``moving_components`` is fixed, so the
           mechanism it belongs to cannot actually move

    MECHANISMS. ``moving_components`` is the list of component names that are
    MEANT to move -- a slider, a crank, a piston, any link of a linkage.
    Without it, every one of them trips M01, because being under-constrained
    is exactly what a moving part is, and the check cannot tell an intentional
    degree of freedom from a forgotten mate. Naming them here says "these are
    free on purpose": they are exempted from M01, and checked for the opposite
    failure instead (M06, fixed when they should be free). Everything else
    still applies to them -- a moving part stacked on another part (M03) is
    just as wrong in a mechanism as in a static assembly.

    A moving component's position is still not reproducible, though, and no
    mate will make it so: that is the nature of a free degree of freedom. To
    record and get back a mechanism's position, use capture_assembly_pose and
    restore_assembly_pose; this tool reports where things are, it does not
    remember it.

    Each issue carries a code, a severity, a message and the component names it
    refers to. ``status`` is "approved" only when nothing was found.

    ``tolerance`` (in ``unit``, default 0.01 mm) is how close two positions
    have to be to count as the same, and how close to the origin counts as on
    it. ``top_level_only`` defaults to True because a nested component's
    transform is expressed within its own parent assembly, which makes
    cross-level position comparison meaningless; set it False to list deeper
    components as well, and read their positions per parent.

    This checks placement, not solid overlap -- run interference_check for
    that, and verify_drawing for the drawing itself.
    """

    def _impl():
        assy = _active_assembly()
        active_unit, factor = _unit_factor(unit)
        mated, mate_scan_complete = _mated_component_names(assy)
        mobile = {str(n) for n in (moving_components or ())}

        issues = []
        components = []
        for raw_component in assy.GetComponents(bool(top_level_only)) or ():
            component = win32com.client.Dispatch(raw_component)
            name = str(component.Name2)
            entry = {"name": name, "position": None, "rotation_matrix": None}
            try:
                entry["path"] = component.GetPathName
                entry["suppressed"] = bool(component.IsSuppressed)
                entry["fixed"] = bool(component.IsFixed)
            except Exception:
                entry.setdefault("path", None)
                entry.setdefault("suppressed", None)
                entry.setdefault("fixed", None)
            entry["mated"] = (name in mated) if mate_scan_complete else None
            entry["declared_moving"] = name in mobile

            try:
                pose = _component_pose(component, factor)
            except Exception as exc:
                entry["position"] = None
                issues.append(ad.issue(
                    "M05", "warning",
                    f"could not read the position of '{name}' ({exc}): it is suppressed, "
                    f"lightweight or not loaded, so there is no way to confirm where it is",
                    name))
                components.append(entry)
                continue

            entry["position"] = dict(pose["translation"], unit=active_unit)
            entry["rotation_matrix"] = pose["rotation_matrix"]
            entry["rotation_convention"] = pose["rotation_convention"]
            components.append(entry)

        # A suppressed component is not actually instanced in the assembly --
        # whatever Transform2 happens to return for it (often a stale value
        # from before it was suppressed) describes nothing real, so M01/M02/
        # M03 would be noise computed over a part that is not there. M05 above
        # already covers one that cannot be read at all; this excludes the
        # ones that COULD be read but should not be judged.
        positioned = [c for c in components
                     if c["position"] is not None and not c.get("suppressed")]

        # M06: a part that is supposed to move but is pinned. Checked before
        # M01 and independently of the mate scan -- being fixed is read
        # straight off the component, so this one answer is always available.
        for entry in positioned:
            if entry["declared_moving"] and entry["fixed"]:
                issues.append(ad.issue(
                    "M06", "warning",
                    f"'{entry['name']}' was declared as a moving component but is FIXED: "
                    f"whatever drives the mechanism cannot move it, and the assembly will "
                    f"look stuck or over-constrained instead of articulating. "
                    f"float_component it.",
                    entry["name"]))

        # M01 / M04: what holds each component in place.
        if mate_scan_complete:
            for entry in positioned:
                name = entry["name"]
                if (require_constrained and not entry["mated"] and not entry["fixed"]
                        and not entry["declared_moving"]):
                    issues.append(ad.issue(
                        "M01", "warning",
                        f"'{name}' is neither fixed nor part of any mate: nothing holds it "
                        f"where it is, so its position is not reproducible and a rebuild "
                        f"or a drag can move it. Mate it, fix_component it, or -- if it is "
                        f"meant to move -- name it in moving_components.",
                        name))
                if entry["mated"] and entry["fixed"]:
                    issues.append(ad.issue(
                        "M04", "warning",
                        f"'{name}' is fixed AND mated: the fix wins and the mate is carried "
                        f"without doing anything, which hides the fact that the mate is not "
                        f"what is positioning the part. float_component it, or drop the mate.",
                        name))

        # M02: a component on the origin, which is where a placement that did
        # not take ends up. Normal for the one base part of an assembly, so it
        # is only worth reporting when there is more than one component.
        if len(positioned) > 1:
            for entry in positioned:
                point = entry["position"]
                if max(abs(point["x"]), abs(point["y"]), abs(point["z"])) <= tolerance:
                    issues.append(ad.issue(
                        "M02", "info",
                        f"'{entry['name']}' sits on the assembly origin "
                        f"(0, 0, 0 {active_unit}). Intended for a base part; for any other "
                        f"part this is what an insert whose position did not take looks like.",
                        entry["name"]))

        # M03: components occupying the same point -- the "parts all landed in
        # the same place" symptom, which an isometric view hides completely.
        for first in range(len(positioned)):
            for second in range(first + 1, len(positioned)):
                a, b = positioned[first], positioned[second]
                gap = math.sqrt(
                    (a["position"]["x"] - b["position"]["x"]) ** 2
                    + (a["position"]["y"] - b["position"]["y"]) ** 2
                    + (a["position"]["z"] - b["position"]["z"]) ** 2
                )
                if gap <= tolerance:
                    issues.append(ad.issue(
                        "M03", "warning",
                        f"'{a['name']}' and '{b['name']}' are at the same position "
                        f"({gap:.4g} {active_unit} apart): they are stacked on top of each "
                        f"other rather than placed where each belongs.",
                        a["name"], b["name"]))

        checks_skipped = []
        if not mate_scan_complete:
            checks_skipped.append(
                "M01/M04: the mate entities could not be read, so which components are "
                "mated is unknown -- no conclusion was drawn about constraints")
        if not require_constrained:
            checks_skipped.append("M01: disabled by require_constrained=False")
        if mobile:
            checks_skipped.append(
                f"M01 on {len(mobile)} component(s) declared as moving "
                f"({', '.join(sorted(mobile))}): a free degree of freedom is what makes "
                f"them move, so it is not reported as a missing constraint. Their "
                f"positions are not reproducible by definition -- record them with "
                f"capture_assembly_pose.")
        unknown_mobile = sorted(mobile - {c["name"] for c in components})
        if unknown_mobile:
            checks_skipped.append(
                f"moving_components named {', '.join(unknown_mobile)}, which are not in "
                f"this assembly at this level -- check the names against list_components "
                f"(a nested component's name looks like 'sub-1/part-1')")

        return {
            "status": ad.worst_status(issues),
            "assembly": _doc_title(_active_doc()),
            "unit": active_unit,
            "tolerance": tolerance,
            "top_level_only": bool(top_level_only),
            "moving_components": sorted(mobile),
            "component_count": len(components),
            "positions_read": len(positioned),
            "components": components,
            "count": len(issues),
            "critical": sum(1 for i in issues if i["severity"] == "critical"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "issues": issues,
            "checks_skipped": checks_skipped,
        }

    return await _run(_impl)


# ---------------------------------------------------------------------------
# Mechanism poses: a moving assembly's position has to be RECORDED
# ---------------------------------------------------------------------------
# A static assembly keeps its position for free: everything in it is fixed or
# fully mated, so a rebuild puts it all back exactly where it was. A mechanism
# cannot work that way. Its moving components are under-constrained ON
# PURPOSE -- that free degree of freedom is the movement -- so the pose it
# happens to be in is one of infinitely many valid ones, and the next drag,
# rebuild, motion study or mate edit silently replaces it. Nothing in
# SolidWorks records what it was.
#
# That is why positions "get lost" as soon as movement is involved while the
# same assembly modelled static stays put, and it is not a bug to fix in a
# mate: no mate can hold a degree of freedom that is meant to be free. The
# answer is to record the pose outside the model and be able to put it back.


POSE_FORMAT = "solidworks-mcp/assembly-pose/1"


def _apply_pose(component, rotation_rows, translation_m) -> None:
    """Write a pose straight into a component's IMathTransform.

    Takes the 3x3 rotation ROWS and the translation in METRES -- the same flat
    layout _component_pose reads (indices 0-8 rotation row-major, 9-11
    translation, 12 scale). Deliberately not Euler angles: a mechanism pose
    has to come back exactly, and decomposing a matrix to angles and
    recomposing it loses that (gimbal-aliased triples map to the same matrix,
    and the round trip is only as exact as the decomposition).
    """
    flat = tuple(float(v) for row in rotation_rows for v in row) + (
        float(translation_m[0]), float(translation_m[1]), float(translation_m[2]),
        1.0, 0.0, 0.0, 0.0,
    )
    variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)
    transform = component.Transform2
    transform.ArrayData = variant
    component.Transform2 = transform


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def capture_assembly_pose(label: str = "",
                                filepath: Optional[str] = None,
                                unit: Optional[str] = None,
                                top_level_only: bool = True) -> dict:
    """Record where every component of the active assembly is RIGHT NOW, as a
    snapshot restore_assembly_pose can put back exactly. Read-only.

    THIS IS FOR ASSEMBLIES THAT MOVE. A static assembly does not need it --
    fixed and fully mated components return to the same place on every
    rebuild. A mechanism does: its moving parts are under-constrained on
    purpose, so the position it is in right now is one of infinitely many
    valid ones and the next drag, rebuild or motion study overwrites it with
    no record of what it was. No mate can prevent that, because the free
    degree of freedom IS the movement. Capturing the pose is what makes a
    mechanism position reproducible.

    Each component is stored with its translation in metres AND the exact 3x3
    rotation matrix SolidWorks reports (row-major, see
    get_component_transform), plus whether it was fixed. No Euler conversion
    anywhere, so the restore is exact rather than close.

    ``filepath`` also writes the snapshot as JSON, which is what lets a pose
    outlive the session -- pass the same path to restore_assembly_pose later,
    or keep several files for several positions of the same mechanism. Without
    it the snapshot exists only in this conversation's history.

    ``label`` is free text kept in the snapshot ("crank at 45 deg", "piston at
    TDC", "gate half open") so a folder of poses stays readable.

    A suppressed or unreadable component is recorded with ``position: null``
    and named in ``warnings``: it is skipped on restore rather than being
    silently given a wrong pose.
    """

    def _impl():
        assy = _active_assembly()
        active_unit, factor = _unit_factor(unit)
        warnings = []
        components = []
        for raw_component in assy.GetComponents(bool(top_level_only)) or ():
            component = win32com.client.Dispatch(raw_component)
            name = str(component.Name2)
            entry = {"name": name, "position": None, "position_m": None,
                     "rotation_matrix": None}
            for attribute, key in (("GetPathName", "path"),
                                   ("IsSuppressed", "suppressed"),
                                   ("IsFixed", "fixed")):
                try:
                    value = _com_member(component, attribute)
                    entry[key] = str(value) if key == "path" else bool(value)
                except Exception:
                    entry[key] = None
            try:
                # Read twice: once in metres, which is what restores exactly,
                # and once in the caller's unit, which is what a human reads.
                exact = _component_pose(component, 1.0)
                shown = _component_pose(component, factor)
            except Exception as exc:
                warnings.append(
                    f"'{name}' pose could not be read ({exc}), so it is recorded without "
                    f"a position and will be left untouched on restore")
                components.append(entry)
                continue
            entry["position_m"] = [exact["translation"]["x"],
                                   exact["translation"]["y"],
                                   exact["translation"]["z"]]
            entry["position"] = dict(shown["translation"], unit=active_unit)
            entry["rotation_matrix"] = exact["rotation_matrix"]
            entry["rotation_convention"] = exact["rotation_convention"]
            components.append(entry)

        pose = {
            "format": POSE_FORMAT,
            "assembly": _doc_title(_active_doc()),
            "assembly_path": _doc_path(_active_doc()),
            "label": label,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "unit": active_unit,
            "top_level_only": bool(top_level_only),
            "component_count": len(components),
            "positions_recorded": sum(1 for c in components if c["position_m"]),
            "components": components,
        }
        if filepath:
            target = _resolve_write_path(filepath)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(pose, handle, indent=2, ensure_ascii=False)
            if not os.path.exists(target):
                raise RuntimeError(f"SolidWorks MCP did not write the pose to '{target}'.")
            pose["filepath"] = target
        pose["warnings"] = warnings
        return pose

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def restore_assembly_pose(filepath: Optional[str] = None,
                                pose: Optional[dict] = None,
                                tolerance: float = 0.01,
                                restore_fixed_state: bool = True) -> dict:
    """Put a mechanism back into a pose recorded by capture_assembly_pose.

    Pass ``filepath`` (a JSON snapshot written earlier) or ``pose`` (the dict
    capture_assembly_pose returned). The exact 3x3 rotation matrix and the
    metre translation are written straight back into each component's
    transform, so the mechanism returns to that pose exactly, not
    approximately.

    THE RESULT IS MEASURED. After the assembly rebuilds, every component's
    pose is read back and compared with the snapshot: ``components`` carries
    the per-component ``deviation``, and ``verified`` is true only when every
    one of them landed within ``tolerance``. This matters more here than
    anywhere else -- an existing mate can override a restored pose the moment
    the rebuild solves, and the component that gets dragged elsewhere is
    reported instead of being assumed back in place.

    A component in the snapshot that is no longer in the assembly, or was
    recorded without a position, is listed in ``skipped`` rather than guessed
    at. A component in the assembly that is not in the snapshot is listed in
    ``untouched`` -- the restore does not invent a pose for it.

    ``restore_fixed_state`` puts each component's fixed/floating state back as
    recorded. A fixed component is floated before its transform is written
    (SolidWorks will not move a fixed component) and re-fixed afterwards.
    """

    def _impl():
        if (filepath is None) == (pose is None):
            raise ValueError("Pass exactly one of filepath or pose.")
        if filepath is not None:
            if not os.path.exists(filepath):
                raise FileNotFoundError(f"Pose file not found: {filepath}")
            with open(filepath, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        else:
            snapshot = pose
        if not isinstance(snapshot, dict) or not snapshot.get("components"):
            raise ValueError(
                "That is not an assembly pose: expected the object "
                f"capture_assembly_pose returns (format '{POSE_FORMAT}') with a "
                f"non-empty 'components' list.")
        stored_format = str(snapshot.get("format", ""))
        if stored_format and stored_format != POSE_FORMAT:
            raise ValueError(
                f"Pose format '{stored_format}' is not '{POSE_FORMAT}'; it was written by "
                f"a different version and its layout cannot be assumed.")

        assy = _active_assembly()
        active_unit, factor = _unit_factor(snapshot.get("unit"))
        title = _doc_title(_active_doc())
        warnings = []
        if snapshot.get("assembly") and snapshot["assembly"] != title:
            warnings.append(
                f"this pose was captured from assembly '{snapshot['assembly']}' but the "
                f"active assembly is '{title}': the components were matched by name, so "
                f"confirm the result before relying on it")

        wanted = {}
        skipped = []
        for entry in snapshot["components"]:
            name = str(entry.get("name", ""))
            if not entry.get("position_m") or not entry.get("rotation_matrix"):
                skipped.append({"name": name, "reason": "recorded without a position"})
                continue
            wanted[name] = entry

        present = {}
        for raw_component in assy.GetComponents(bool(snapshot.get("top_level_only", True))) or ():
            component = win32com.client.Dispatch(raw_component)
            present[str(component.Name2)] = component
        for name in sorted(set(wanted) - set(present)):
            skipped.append({"name": name, "reason": "no longer in the assembly"})
            wanted.pop(name, None)
        untouched = sorted(set(present) - set(wanted) - {s["name"] for s in skipped})

        # Float first, write every transform, then rebuild ONCE: rebuilding
        # between writes lets a half-restored pose solve against mates and
        # drag components that have not been written yet.
        refix = []
        for name, entry in wanted.items():
            component = present[name]
            try:
                if bool(component.IsFixed):
                    _select_component(assy, name)
                    assy.UnfixComponent()
                    if restore_fixed_state and entry.get("fixed"):
                        refix.append(name)
                elif restore_fixed_state and entry.get("fixed"):
                    refix.append(name)
            except Exception as exc:
                warnings.append(f"could not float '{name}' before restoring it ({exc})")
            try:
                _apply_pose(component, entry["rotation_matrix"], entry["position_m"])
            except Exception as exc:
                skipped.append({"name": name, "reason": f"transform could not be written: {exc}"})

        rebuild = assy.EditRebuild3
        if callable(rebuild):
            rebuild()

        for name in refix:
            try:
                _select_component(assy, name)
                assy.FixComponent()
            except Exception as exc:
                warnings.append(f"could not re-fix '{name}' after restoring it ({exc})")

        failed = {s["name"] for s in skipped}
        components = []
        for name, entry in wanted.items():
            if name in failed:
                continue
            result = {"name": name,
                      "requested_position": dict(
                          zip(("x", "y", "z"),
                              [v * factor for v in entry["position_m"]]),
                          unit=active_unit)}
            try:
                landed = _component_pose(present[name], factor)
            except Exception as exc:
                result["actual_position"] = None
                result["deviation"] = None
                result["verified"] = False
                warnings.append(
                    f"'{name}' was restored but its pose could not be read back ({exc}), "
                    f"so it is UNVERIFIED")
                components.append(result)
                continue
            requested = tuple(v * factor for v in entry["position_m"])
            deviation = dict(_deviation(requested, landed["translation"], tolerance),
                             unit=active_unit)
            result["actual_position"] = dict(landed["translation"], unit=active_unit)
            result["deviation"] = deviation
            result["verified"] = bool(deviation["within_tolerance"])
            result["fixed"] = name in refix
            if not deviation["within_tolerance"]:
                warnings.append(
                    f"'{name}' did not return to its recorded pose: it is "
                    f"{deviation['distance']:.4g} {active_unit} away (dx="
                    f"{deviation['dx']:.4g}, dy={deviation['dy']:.4g}, dz="
                    f"{deviation['dz']:.4g}). A mate most likely overrode the restore -- "
                    f"check list_mates for '{name}'")
            components.append(result)

        return {
            "assembly": title,
            "label": snapshot.get("label", ""),
            "captured_at": snapshot.get("captured_at"),
            "source": filepath or "inline pose",
            "unit": active_unit,
            "restored": len(components),
            "verified": bool(components) and all(c["verified"] for c in components),
            "components": components,
            "skipped": skipped,
            "untouched": untouched,
            "refixed": sorted(refix) if restore_fixed_state else [],
            "warnings": warnings,
        }

    return await _run(_impl)


# ===========================================================================
# Motion Study tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_motion_studies() -> dict:
    """List native SolidWorks Motion Studies in the active assembly or part.

    This is intentionally study management only. Mechanical movement is
    provided by assembly mates; motors, forces, timeline keyframes, and Motion
    Analysis solve controls are not created by this tool.
    """

    def _impl():
        doc = _active_doc()
        manager = _motion_study_manager(doc)
        count = int(_invoke_motion_manager(manager, "GetMotionStudyCount", (3, 0)))
        raw_names = _invoke_motion_manager(manager, "GetMotionStudyNames", (12, 0))
        names = [str(name) for name in (raw_names or ())]
        return {"count": count, "names": names, "supports_timeline_motors": False}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_motion_study(name: Optional[str] = None, activate: bool = True) -> dict:
    """Create and optionally activate a native SolidWorks Animation study.

    The created study is a container for MotionManager animation data. It does
    not add a motor or solve a physical analysis; use mechanical mates to
    constrain the assembly's movement.
    """

    def _impl():
        doc = _active_doc()
        manager = _motion_study_manager(doc)
        raw_study = _invoke_motion_manager(manager, "CreateMotionStudy", (9, 0))
        if raw_study is None:
            raise RuntimeError("SolidWorks did not create a Motion Study.")
        study = win32com.client.Dispatch(raw_study)
        study_name = str(getattr(study, "Name", "") or "").strip()
        requested_name = (name or "").strip()
        if requested_name:
            try:
                study.Name = requested_name
                study_name = requested_name
            except Exception as exc:
                raise RuntimeError(f"Motion Study was created but could not be renamed to '{requested_name}': {exc}") from exc
        if activate:
            activated = bool(manager.ActivateMotionStudy(study_name))
            if not activated:
                raise RuntimeError(f"Motion Study '{study_name}' was created but could not be activated.")
        count = int(_invoke_motion_manager(manager, "GetMotionStudyCount", (3, 0)))
        return {"name": study_name, "created": True, "activated": bool(activate), "count": count,
                "contains_motor": False, "study_type": "Animation"}

    return await _run(_impl)


# ===========================================================================
# Export tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def export_document(filepath: str) -> dict:
    """Export the active document to STEP, STL, IGES, PDF, DWG, DXF or Parasolid.

    The format comes from the file extension:
    .step/.stp, .stl, .igs/.iges, .pdf (drawings), .dwg and .dxf (drawings),
    .x_t/.x_b (Parasolid), .sat, .3mf

    .dwg and .dxf of a DRAWING export every sheet, each to its own file: for
    sheets beyond the first, SolidWorks appends the sheet name to the
    filename. The returned `sheets` field says how many there were, since the
    extra files are not named by the caller."""

    def _impl():
        abs_path = _resolve_write_path(filepath)
        ext = os.path.splitext(abs_path)[1].lower()
        valid = {".step", ".stp", ".stl", ".igs", ".iges", ".pdf", ".dwg", ".dxf",
                 ".x_t", ".x_b", ".sat", ".3mf"}
        if ext not in valid:
            raise ValueError(f"Unsupported export format '{ext}'. Use: {', '.join(sorted(valid))}")
        doc = _active_doc()

        # .dwg was missing entirely, even though the drawing-only
        # export_flat_pattern_dxf already accepted it. Structural-steel
        # offices exchange DWG with AutoCAD constantly.
        drawing_only = {".pdf", ".dwg"}
        if ext in drawing_only and _doc_type(doc) != 3:
            raise RuntimeError(
                f"{ext} export is only supported for drawing documents "
                f"(.slddrw). Open or create a drawing first."
            )

        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        # Both ExportData (param 4) and AdvancedSaveAsOptions (param 5) are
        # VT_DISPATCH slots — plain None triggers "type mismatch". A null-dispatch
        # VARIANT satisfies COM.
        null_disp = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        ok = False
        try:
            ok = bool(doc.Extension.SaveAs(abs_path, 0, 0, null_disp, errors, warnings))
        except Exception as e:
            log.warning("Extension.SaveAs failed: %s", e)
        if not ok:
            try:
                ok = bool(doc.Extension.SaveAs3(abs_path, 0, 0, null_disp, null_disp, errors, warnings))
            except Exception as e:
                log.warning("Extension.SaveAs3 failed: %s", e)
        if not ok:
            raise RuntimeError(f"Export failed (error {errors.value}, warning {warnings.value}).")
        log.info("Exported to %s", abs_path)
        result = {"path": abs_path, "format": ext.lstrip(".")}
        if _doc_type(doc) == 3:
            try:
                result["sheets"] = len(_sheet_names(doc))
            except Exception:
                log.debug("Could not count sheets after export", exc_info=True)
        if not os.path.exists(abs_path):
            raise RuntimeError(
                f"SolidWorks reported success but no file was written to "
                f"{abs_path}."
            )
        result["size_bytes"] = os.path.getsize(abs_path)
        return result

    return await _run(_impl)


# ===========================================================================
# Sketch tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_sketch(plane: str = "front") -> dict:
    """Create a sketch on a standard plane or a named reference plane.

    Standard values are 'front', 'top', and 'right'. Any other value is treated
    as the exact name of an existing reference plane, enabling offset-plane
    workflows such as independent piston ring grooves.
    """

    def _impl():
        doc = _active_doc()
        plane_name = (
            _standard_plane_name(doc, plane)
            if plane.lower() in {"front", "top", "right"}
            else plane
        )
        if not _select_by_id(doc, plane_name, "PLANE"):
            raise RuntimeError(f"Could not select plane '{plane_name}'.")
        doc.InsertSketch2(True)
        _remember_active_sketch(doc)
        return {"plane": plane_name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_sketch_on_face(x: float = 0, y: float = 0, z: float = 0, unit: Optional[str] = None) -> dict:
    """Create a sketch on an existing body face, chosen by a point known to lie on it.

    x/y/z only pick WHICH face to sketch on. Once the sketch is open, geometry
    you draw in it (draw_circle, draw_rectangle, ...) uses that face's own
    implicit sketch coordinates, whose origin and axis orientation SolidWorks
    derives from the face's surface parameterization -- not from x/y/z, and
    not predictable ahead of time (confirmed to differ between a face
    inherited directly from its defining sketch and a folded EdgeFlange face).
    Draw a small test shape and inspect it with list_faces/measure_body first
    if you need geometry at a specific position on the face.

    RAISES if the chosen face's surface is directly defined by (coincident
    with) an existing sketch already in the tree -- e.g. the flat remainder
    of a sheet-metal EdgeFlange face is literally the plane of that flange's
    own profile sketch. InsertSketch2 on such a face reopens that EXISTING
    sketch for editing rather than creating an independent new one (found
    live, 2026-10-04, via bug_report_solidworks_mcp.txt's EdgeFlange-face
    scenario: draw_circle appeared to succeed, but silently added the circle
    into the flange's own defining sketch; every later cut_extrude pointed at
    that same sketch and failed, because it now mixed the flange's profile
    geometry with an unrelated circle instead of forming one closed loop).

    CONFIRMED DEAD END for cutting an EdgeFlange face specifically (full
    writeup: .claude/knowledge/chapa_metalica.md, section "Furo em face de
    EdgeFlange"). This RAISE catches it in some but not all cases -- whether
    SolidWorks reopens the defining sketch depends on prior state (e.g.
    whether flatten_sheet_metal was toggled earlier in the session), so a
    call that doesn't raise here can still fail at cut_extrude instead, or
    -- worse -- "succeed" while leaving the cut suppressed outside the Flat
    Pattern configuration (no hole in the actual folded part). Don't retry
    with different coordinates or a different selection method; none of
    that is the actual problem. Cut before folding, or use a named
    reference plane (create_reference_plane + create_sketch) and accept
    that the result won't survive flatten_sheet_metal()."""

    def _impl():
        doc = _active_doc()
        x_m, y_m, z_m = to_meters(x, unit), to_meters(y, unit), to_meters(z, unit)
        doc.ClearSelection2(True)
        if not _select_by_id(doc, "", "FACE", x_m, y_m, z_m):
            raise RuntimeError(f"No face found at ({x}, {y}, {z}) {unit or _default_unit}.")
        pre_existing = {str(feat.Name) for feat in (doc.FeatureManager.GetFeatures(False) or ())}
        doc.InsertSketch2(True)
        _remember_active_sketch(doc)
        active_name = _last_user_sketch_name
        if active_name and active_name in pre_existing:
            doc.InsertSketch2(True)  # close it immediately -- don't leave it open for editing
            raise RuntimeError(
                f"The face at ({x}, {y}, {z}) {unit or _default_unit} is directly defined by the "
                f"existing sketch '{active_name}' -- InsertSketch2 reopened that sketch for editing "
                f"instead of creating a new one. Drawing here would corrupt '{active_name}' (e.g. a "
                f"flange's own profile sketch), not add an independent sketch. Use a named reference "
                f"plane (create_reference_plane + create_sketch) on this face instead."
            )
        return {"point": {"x": x, "y": y, "z": z}, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def close_sketch() -> dict:
    """Exit the currently active sketch."""

    def _impl():
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            return {"closed": False, "message": "No sketch was active."}
        # Capture the name HERE, one line before exiting it, not in
        # create_sketch/create_sketch_on_face at open time: live testing
        # (2026-10-04) showed a sketch opened via create_sketch_on_face can
        # still report ActiveSketch as None immediately after InsertSketch2,
        # silently leaving _remember_active_sketch's open-time capture stale.
        # Right here ActiveSketch is guaranteed non-None (just checked above).
        _remember_active_sketch(doc)
        doc.InsertSketch2(True)
        # A sketch just closed on a face (as opposed to a named plane) was
        # observed not to appear in FeatureManager.GetFeatures(False) yet at
        # this point -- cut_extrude's SelectByID2("SKETCH") lookup needs the
        # feature actually registered in the tree, not just a name. Force
        # that registration now rather than leaving the caller to guess.
        rebuild = doc.EditRebuild3
        if callable(rebuild):
            rebuild()
        return {"closed": True}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_sketch_status() -> dict:
    """Report whether a sketch is currently active, and its name if so."""

    def _impl():
        doc = _active_doc()
        active = doc.SketchManager.ActiveSketch
        if active is None:
            return {"active": False}
        try:
            name = active.Name
        except Exception:
            name = None
        return {"active": True, "name": name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_line(x1: float = 0, y1: float = 0, x2: float = 100, y2: float = 0, unit: Optional[str] = None) -> dict:
    """Draw a line in the active sketch from (x1, y1) to (x2, y2)."""

    def _impl():
        if x1 == x2 and y1 == y2:
            raise ValueError("Start and end points are identical — line has zero length.")
        doc = _active_doc()
        line = doc.SketchManager.CreateLine(
            to_meters(x1, unit), to_meters(y1, unit), 0,
            to_meters(x2, unit), to_meters(y2, unit), 0,
        )
        if line is None:
            raise RuntimeError("Failed to draw line. Is a sketch active?")
        return {"from": [x1, y1], "to": [x2, y2], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_centerline(x1: float = 0, y1: float = 0, x2: float = 0, y2: float = 100,
                           unit: Optional[str] = None) -> dict:
    """Draw a centerline (construction geometry) in the active PART/ASSEMBLY sketch.

    A centerline is required as the rotation axis for revolve_sketch (Insert >
    Centerline in the SolidWorks UI). It is NOT the same as add_centerline, which
    only works on drawing (.SLDDRW) views — this tool draws inside a 3D part or
    assembly sketch, e.g. to model a revolved part like a piston, shaft, or bolt.

    x1/y1, x2/y2: the two endpoints of the centerline in sketch coordinates."""

    def _impl():
        if x1 == x2 and y1 == y2:
            raise ValueError("Start and end points are identical — centerline has zero length.")
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            raise RuntimeError("No sketch is active. Call create_sketch first.")
        line = doc.SketchManager.CreateCenterLine(
            to_meters(x1, unit), to_meters(y1, unit), 0,
            to_meters(x2, unit), to_meters(y2, unit), 0,
        )
        if line is None:
            raise RuntimeError("Failed to draw centerline. Is a sketch active?")
        return {"from": [x1, y1], "to": [x2, y2], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_circle(x: float = 0, y: float = 0, radius: float = 25, unit: Optional[str] = None) -> dict:
    """Draw a circle in the active sketch given its center and radius."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        doc = _active_doc()
        x_m, y_m, r_m = to_meters(x, unit), to_meters(y, unit), to_meters(radius, unit)
        circle = doc.SketchManager.CreateCircle(x_m, y_m, 0, x_m + r_m, y_m, 0)
        if circle is None:
            raise RuntimeError("Failed to draw circle. Is a sketch active?")
        return {"center": [x, y], "radius": radius, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_rectangle(x1: float = -50, y1: float = -25, x2: float = 50, y2: float = 25, unit: Optional[str] = None) -> dict:
    """Draw a rectangle in the active sketch given two opposite corners."""

    def _impl():
        if x1 == x2 or y1 == y2:
            raise ValueError("Rectangle corners must differ in both X and Y — zero-area rectangle.")
        doc = _active_doc()
        rect = doc.SketchManager.CreateCornerRectangle(
            to_meters(x1, unit), to_meters(y1, unit), 0,
            to_meters(x2, unit), to_meters(y2, unit), 0,
        )
        if rect is None:
            raise RuntimeError("Failed to draw rectangle. Is a sketch active?")
        return {"width": abs(x2 - x1), "height": abs(y2 - y1), "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_arc(cx: float = 0, cy: float = 0, radius: float = 25,
                    start_angle: float = 0, end_angle: float = 90, unit: Optional[str] = None) -> dict:
    """Draw an arc in the active sketch given center, radius, and start/end angles (degrees)."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        if start_angle == end_angle:
            raise ValueError("Start and end angles are identical — arc has zero sweep.")
        doc = _active_doc()
        cx_m, cy_m, r_m = to_meters(cx, unit), to_meters(cy, unit), to_meters(radius, unit)
        a1, a2 = math.radians(start_angle), math.radians(end_angle)
        x1, y1 = cx_m + r_m * math.cos(a1), cy_m + r_m * math.sin(a1)
        x2, y2 = cx_m + r_m * math.cos(a2), cy_m + r_m * math.sin(a2)
        arc = doc.SketchManager.CreateArc(cx_m, cy_m, 0, x1, y1, 0, x2, y2, 0, 1)
        if arc is None:
            raise RuntimeError("Failed to draw arc. Is a sketch active?")
        return {"center": [cx, cy], "radius": radius, "start_angle": start_angle,
                "end_angle": end_angle, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_spline(points: list[list[float]], natural_ends: bool = True,
                      unit: Optional[str] = None) -> dict:
    """Draw a 2D B-spline through two or more [x, y] control points.

    Use splines for smooth aerodynamic outlines, ergonomic transitions, and
    organic industrial-design profiles. The active sketch remains in control;
    close it normally before creating a solid feature.
    """

    def _impl():
        if len(points) < 2:
            raise ValueError("points must contain at least two [x, y] coordinates.")
        flattened: list[float] = []
        for index, point in enumerate(points):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"points[{index}] must be an [x, y] pair.")
            x, y = point
            # CreateSpline3 expects XY pairs for a 2D sketch. Supplying Z
            # values shifts every following point and makes the profile fail.
            flattened.extend((to_meters(float(x), unit), to_meters(float(y), unit)))

        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            raise RuntimeError("No sketch is active. Create a sketch before drawing a spline.")
        # CreateSpline3 is the current API path for 2D and on-surface splines.
        # For a 2D spline, surfaces and directions are omitted and the COM
        # method receives one contiguous XY coordinate array.
        # Status is an output object array for on-surface splines and is empty
        # for 2D splines. Keep it as an empty by-reference VARIANT.
        status = win32com.client.VARIANT(pythoncom.VT_VARIANT | pythoncom.VT_BYREF, None)
        # Dynamic dispatch otherwise converts a Python tuple into SAFEARRAY of
        # VARIANT. SolidWorks 2025 requires SAFEARRAY(double) for PointData.
        point_data = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, flattened
        )
        # CreateSpline3's 4th argument is IsPeriodic (True = closed-loop
        # spline), not "natural ends". Passing natural_ends straight through
        # made the documented default (True) silently build a periodic
        # spline that whips back on itself through non-cyclic points --
        # confirmed live: natural_ends=True produced a huge closed spike
        # instead of a smooth open curve, natural_ends=False produced the
        # correct open curve. Invert it so natural_ends=True (the default,
        # "open spline with free end tangents") maps to IsPeriodic=False.
        is_periodic = not natural_ends
        spline = doc.SketchManager.CreateSpline3(point_data, None, None, is_periodic, status)
        if spline is None:
            raise RuntimeError("SolidWorks could not create the spline in the active sketch.")
        return {
            "point_count": len(points),
            "natural_ends": natural_ends,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_polygon(cx: float = 0, cy: float = 0, radius: float = 25, sides: int = 6, unit: Optional[str] = None) -> dict:
    """Draw a regular, circumscribed polygon in the active sketch."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        if not (3 <= sides <= 100):
            raise ValueError("sides must be between 3 and 100.")
        doc = _active_doc()
        cx_m, cy_m, r_m = to_meters(cx, unit), to_meters(cy, unit), to_meters(radius, unit)
        polygon = doc.SketchManager.CreatePolygon(cx_m, cy_m, 0, cx_m + r_m, cy_m, 0, sides, False)
        if polygon is None:
            raise RuntimeError("Failed to draw polygon. Is a sketch active?")
        return {"center": [cx, cy], "radius": radius, "sides": sides, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_sketch_dimension(x1: float, y1: float,
                                x2: Optional[float] = None, y2: Optional[float] = None,
                                dim_x: float = 0, dim_y: float = 50,
                                value: Optional[float] = None, unit: Optional[str] = None) -> dict:
    """Add a driving dimension to sketch entities selected by coordinate.
    Select the first entity near (x1,y1), optionally a second near (x2,y2).
    The dimension text is placed at (dim_x, dim_y).
    If value is given, the dimension is set to that value (driving dimension)."""

    def _impl():
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            raise RuntimeError("No sketch is active. Open a sketch first.")

        doc.ClearSelection2(True)
        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        if not _select_by_id(doc, "", "SKETCHSEGMENT", x1_m, y1_m, 0):
            if not _select_by_id(doc, "", "SKETCHPOINT", x1_m, y1_m, 0):
                raise RuntimeError(f"No sketch entity found near ({x1}, {y1}).")

        if x2 is not None and y2 is not None:
            x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "SKETCHSEGMENT", x2_m, y2_m, 0, True, 0, empty, 0):
                doc.Extension.SelectByID2("", "SKETCHPOINT", x2_m, y2_m, 0, True, 0, empty, 0)

        dim_x_m, dim_y_m = to_meters(dim_x, unit), to_meters(dim_y, unit)
        # AddDimension2 can open the interactive value dialog and block COM.
        # Temporarily disable swInputDimValOnCreate (10), then restore the user's
        # original setting even if SolidWorks rejects the selected geometry.
        app = _connect()
        input_value_preference = 10  # swInputDimValOnCreate
        original_input_value = bool(app.GetUserPreferenceToggle(input_value_preference))
        try:
            app.SetUserPreferenceToggle(input_value_preference, False)
            disp_dim = doc.AddDimension2(dim_x_m, dim_y_m, 0)
        finally:
            app.SetUserPreferenceToggle(input_value_preference, original_input_value)
        if disp_dim is None:
            raise RuntimeError("Failed to add dimension. Ensure the sketch entities are valid for dimensioning.")

        disp_dim = win32com.client.Dispatch(disp_dim)
        result = {"unit": unit or _default_unit}

        if value is not None:
            val_m = to_meters(value, unit)
            dim = win32com.client.Dispatch(disp_dim.GetDimension2(0))
            dim.SystemValue = val_m
            if not math.isclose(float(dim.SystemValue), val_m, rel_tol=0.0, abs_tol=1e-9):
                raise RuntimeError("SolidWorks did not apply the requested sketch dimension value.")
            result["value"] = value

        doc.ClearSelection2(True)
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_sketch_relation(relation: str, x1: float, y1: float,
                                x2: Optional[float] = None, y2: Optional[float] = None,
                                unit: Optional[str] = None) -> dict:
    """Add a geometric relation (constraint) between sketch entities selected by coordinate.
    Relations: horizontal, vertical, coincident, collinear, perpendicular, parallel,
    equal, fixed, tangent, concentric, midpoint, symmetric."""

    def _impl():
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            raise RuntimeError("No sketch is active.")

        relations = {
            "horizontal": "sgHORIZONTAL2D", "vertical": "sgVERTICAL2D",
            "coincident": "sgCOINCIDENT", "collinear": "sgCOLINEAR",
            "perpendicular": "sgPERPENDICULAR", "parallel": "sgPARALLEL",
            "equal": "sgEQUAL", "fixed": "sgFIXED",
            "tangent": "sgTANGENT", "concentric": "sgCONCENTRIC",
            "midpoint": "sgMIDPOINT", "symmetric": "sgSYMMETRIC",
        }
        sg_type = relations.get(relation.lower())
        if sg_type is None:
            raise ValueError(f"Unknown relation '{relation}'. Use: {', '.join(relations)}")

        doc.ClearSelection2(True)
        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        if not _select_by_id(doc, "", "SKETCHSEGMENT", x1_m, y1_m, 0):
            if not _select_by_id(doc, "", "SKETCHPOINT", x1_m, y1_m, 0):
                raise RuntimeError(f"No sketch entity found near ({x1}, {y1}).")

        if x2 is not None and y2 is not None:
            x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "SKETCHSEGMENT", x2_m, y2_m, 0, True, 0, empty, 0):
                doc.Extension.SelectByID2("", "SKETCHPOINT", x2_m, y2_m, 0, True, 0, empty, 0)

        doc.SketchAddConstraints(sg_type)
        return {"relation": relation}

    return await _run(_impl)


# ===========================================================================
# Feature tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def extrude_sketch(depth: float = 10, both_directions: bool = False,
                         merge: bool = True, unit: Optional[str] = None) -> dict:
    """Boss-extrude the last sketch drawn; set merge=False to keep a separate body."""

    def _impl():
        if depth <= 0:
            raise ValueError(f"Depth must be positive, got {depth}.")
        doc = _active_doc()
        depth_m = to_meters(depth, unit)
        sketch_name = _select_last_sketch(doc)
        end_cond = 6 if both_directions else 0  # 6=MidPlane, 0=Blind
        feat = doc.FeatureManager.FeatureExtrusion2(
            True, False, False, end_cond, 0, depth_m, depth_m,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            merge, True, True, 0, 0.0, False,
        )
        if feat is None:
            raise RuntimeError(f"Extrusion failed on sketch '{sketch_name}'. Check that its profile is closed.")
        return {"sketch": sketch_name, "depth": depth, "both_directions": both_directions, "merge": merge,
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def cut_extrude(depth: float = 10, through_all: bool = False,
                       both_directions: bool = False, unit: Optional[str] = None) -> dict:
    """Cut-extrude the last sketch drawn, removing material from the body."""

    def _impl():
        if not through_all and depth <= 0:
            raise ValueError(f"Depth must be positive, got {depth}.")
        doc = _active_doc()
        sketch_name = _select_last_sketch(doc)
        if through_all:
            end_cond = 2 if both_directions else 1  # ThroughAllBoth / ThroughAll
            cut_depth = 0.0
        else:
            end_cond = 6 if both_directions else 0  # MidPlane / Blind
            cut_depth = to_meters(depth, unit)

        feat = doc.FeatureManager.FeatureCut4(
            True, False, False, end_cond, 0, cut_depth, 0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            False, True, True,
            False, False, False,
            0, 0.0, False, False,
        )
        if feat is None:
            raise RuntimeError(
                f"Cut failed on sketch '{sketch_name}'. Check that its profile is closed. "
                f"[_select_last_sketch debug: {_last_select_debug}]"
            )
        return {"sketch": sketch_name, "through_all": through_all,
                "depth": None if through_all else depth, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def revolve_sketch(angle: float = 360, both_directions: bool = False,
                          cut: bool = False, reverse: bool = False,
                          unit: Optional[str] = None) -> dict:
    """Revolve the last sketch around its centerline axis.
    The sketch MUST contain a centerline (see draw_centerline) that serves as
    the revolve axis. Angle in degrees (default 360 = full revolution).
    cut=True performs a revolved CUT (removes material) instead of a revolved
    boss (adds material) — e.g. for a piston ring groove or an O-ring seat.
    reverse=True flips which side of the profile is kept for the cut/boss."""

    def _impl():
        if angle <= 0 or angle > 360:
            raise ValueError(f"Angle must be between 0 (exclusive) and 360 (inclusive), got {angle}.")
        doc = _active_doc()
        sketch_name = _select_last_sketch(doc)
        angle_rad = math.radians(angle)

        # FeatureRevolve2 takes 20 positional args in this exact order (verified
        # against the SW 2025 type library — the previous 17-arg call was both
        # short by 3 required args AND had every position after arg 4 mis-mapped,
        # e.g. it passed the angle into the ReverseDir bool slot).
        feat = doc.FeatureManager.FeatureRevolve2(
            not both_directions,   # SingleDir
            True,                  # IsSolid (False would create a surface, not a solid)
            False,                 # IsThin
            cut,                   # IsCut (False=boss/add material, True=cut/remove)
            reverse,                # ReverseDir
            False,                 # BothDirectionUpToSameEntity
            0,                     # Dir1Type (0 = swEndCondBlind, i.e. revolve by angle)
            0,                     # Dir2Type
            angle_rad,             # Dir1Angle
            angle_rad if both_directions else 0.0,  # Dir2Angle
            False, False,          # OffsetReverse1, OffsetReverse2
            0.0, 0.0,              # OffsetDistance1, OffsetDistance2
            0,                     # ThinType (0 = none)
            0.0, 0.0,              # ThinThickness1, ThinThickness2
            True,                  # Merge
            True,                  # UseFeatScope
            True,                  # UseAutoSelect
        )
        if feat is None:
            raise RuntimeError(
                f"Revolve failed on sketch '{sketch_name}'. "
                "Ensure the sketch contains a centerline (draw_centerline) as the "
                "revolve axis, and that the profile does not cross the axis."
            )
        return {"sketch": sketch_name, "angle": angle,
                "both_directions": both_directions, "cut": cut}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def sweep_sketch(profile_sketch: str, path_sketch: str) -> dict:
    """Sweep a closed profile sketch along a path sketch to create a solid.
    Both sketches must already exist. The profile must be a closed contour.
    The path can be open or closed (on a different plane than the profile)."""

    def _impl():
        doc = _active_doc()
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(profile_sketch, "SKETCH", 0, 0, 0, False, 1, empty, 0):
            raise RuntimeError(f"Could not select profile sketch '{profile_sketch}'.")
        if not doc.Extension.SelectByID2(path_sketch, "SKETCH", 0, 0, 0, True, 4, empty, 0):
            raise RuntimeError(f"Could not select path sketch '{path_sketch}'.")

        feat = doc.FeatureManager.InsertProtrusionSwept4(
            False, False, 0, False, False,
            0, 0, False, 0.0, 0.0, 0,
            0, True, True, True, 0.0, True, False, 0.0, 0,
        )
        if feat is None:
            raise RuntimeError(
                "Sweep failed. Ensure the profile sketch is closed, "
                "the path sketch is on a different plane, and both are named correctly."
            )
        return {"profile": profile_sketch, "path": path_sketch}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def loft_sketches(sketch_names: list) -> dict:
    """Loft between two or more closed profile sketches to create a solid.
    Each sketch must be on a different plane. Provide at least 2 sketch names."""

    def _impl():
        if len(sketch_names) < 2:
            raise ValueError("At least 2 sketches are required for a loft.")
        doc = _active_doc()
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        for i, name in enumerate(sketch_names):
            append = i > 0
            if not doc.Extension.SelectByID2(name, "SKETCH", 0, 0, 0, append, 1, empty, 0):
                raise RuntimeError(f"Could not select sketch '{name}'.")

        feat = doc.FeatureManager.InsertProtrusionBlend2(
            False, True, True, 1.0, 0, 0,
            0.0, 0.0, False, False,
            False, 0.0, 0.0, 0,
            True, True, True, 0,
        )
        if feat is None:
            raise RuntimeError(
                "Loft failed. Ensure all sketches are closed profiles on different planes "
                "and are listed in order from start to end."
            )
        return {"sketches": sketch_names}

    return await _run(_impl)


_BEND_FILLET_CHAMFER_WARNING = (
    "This part has sheet-metal bend(s) ({bend_types}). {tool}(...) fillets/"
    "chamfers EVERY edge of the body, with no way to exclude bend edges -- "
    "confirmed live, 2026-10-04 (see .claude/knowledge/chapa_metalica.md, "
    "section 'fillet_edges/chamfer_edges numa peca de chapa metalica "
    "dobrada'): the folded 3D state stays valid, but flatten_sheet_metal() "
    "afterward fails (FlatPattern error_code 1). Pass force=True to proceed "
    "anyway, then call flatten_sheet_metal() yourself to confirm the flat "
    "pattern still rebuilds before relying on export_flat_pattern_dxf."
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def fillet_edges(radius: float = 2, unit: Optional[str] = None, force: bool = False) -> dict:
    """Apply a constant-radius fillet to every edge of the solid body/bodies.

    force: required (set True) on a part with sheet-metal bends -- this
    tool cannot exclude bend edges from "every edge", and filleting a bend
    edge is confirmed to break flatten_sheet_metal() afterward even though
    the folded 3D body stays valid. See the raised error for the full
    explanation if this fires."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        doc = _active_doc()
        if not force and _has_sheet_metal_bends(doc):
            raise RuntimeError(_BEND_FILLET_CHAMFER_WARNING.format(bend_types="SMBaseFlange/EdgeFlange/OneBend", tool="fillet_edges"))
        radius_m = to_meters(radius, unit)
        edge_count = _select_all_edges(doc)
        empty = win32com.client.VARIANT(pythoncom.VT_EMPTY, None)
        feat = doc.FeatureManager.FeatureFillet3(195, radius_m, 0, False, False, empty, empty)
        if feat is None:
            raise RuntimeError("Failed to create fillet.")
        return {"radius": radius, "unit": unit or _default_unit, "edges": edge_count}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def chamfer_edges(distance: float = 2, angle: float = 45, unit: Optional[str] = None, force: bool = False) -> dict:
    """Apply a distance/angle chamfer to every edge of the solid body/bodies.

    force: required (set True) on a part with sheet-metal bends -- this
    tool cannot exclude bend edges from "every edge", and chamfering a bend
    edge is confirmed to break flatten_sheet_metal() afterward even though
    the folded 3D body stays valid. See the raised error for the full
    explanation if this fires."""

    def _impl():
        if distance <= 0:
            raise ValueError(f"Distance must be positive, got {distance}.")
        if not (0 < angle < 90):
            raise ValueError(f"Angle must be between 0 and 90 degrees (exclusive), got {angle}.")
        doc = _active_doc()
        if not force and _has_sheet_metal_bends(doc):
            raise RuntimeError(_BEND_FILLET_CHAMFER_WARNING.format(bend_types="SMBaseFlange/EdgeFlange/OneBend", tool="chamfer_edges"))
        dist_m = to_meters(distance, unit)
        edge_count = _select_all_edges(doc)
        feat = doc.FeatureManager.InsertFeatureChamfer(1, 0, dist_m, math.radians(angle), dist_m, 0, 0, 0)
        if feat is None:
            raise RuntimeError("Failed to create chamfer.")
        return {"distance": distance, "angle": angle, "unit": unit or _default_unit, "edges": edge_count}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def shell_body(thickness: float = 2, remove_face_at_x: Optional[float] = None,
                      remove_face_at_y: Optional[float] = None,
                      remove_face_at_z: Optional[float] = None,
                      unit: Optional[str] = None) -> dict:
    """Hollow out the solid body leaving thin walls.
    Optionally select a face to remove (open shell) by specifying a point on it.
    If no face is selected, creates a closed shell (uniform thickness all around)."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        doc = _active_doc()
        t_m = to_meters(thickness, unit)

        doc.ClearSelection2(True)
        if remove_face_at_x is not None and remove_face_at_y is not None and remove_face_at_z is not None:
            fx = to_meters(remove_face_at_x, unit)
            fy = to_meters(remove_face_at_y, unit)
            fz = to_meters(remove_face_at_z, unit)
            empty_callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 1, empty_callout, 0):
                raise RuntimeError(
                    f"No face found at ({remove_face_at_x}, {remove_face_at_y}, {remove_face_at_z})."
                )

        shell_count_before = sum(
            1
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "Shell"
        )
        # InsertFeatureShell belongs to IModelDoc2 and returns void over COM.
        doc.InsertFeatureShell(t_m, False)
        rebuild = doc.EditRebuild3
        if callable(rebuild):
            rebuild()
        shell_count_after = sum(
            1
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "Shell"
        )
        if shell_count_after <= shell_count_before:
            raise RuntimeError(
                "Shell failed. Ensure the body has sufficient thickness "
                "and select a face to remove for an open shell."
            )
        return {"thickness": thickness, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def linear_pattern(feature_name: str, direction: str = "x",
                          count: int = 2, spacing: float = 20,
                          count2: int = 1, spacing2: float = 20,
                          unit: Optional[str] = None) -> dict:
    """Repeat a feature in a linear pattern.
    direction: 'x', 'y', or 'z' (uses the corresponding standard axis).
    count/spacing: instances and distance in the primary direction.
    count2/spacing2: instances and distance in the secondary direction (perpendicular)."""

    def _impl():
        if count < 2:
            raise ValueError(f"Count must be at least 2, got {count}.")
        if spacing <= 0:
            raise ValueError(f"Spacing must be positive, got {spacing}.")
        doc = _active_doc()
        spacing_m = to_meters(spacing, unit)
        spacing2_m = to_meters(spacing2, unit)

        # SolidWorks expects a true linear reference with selection mark 1 and
        # the seed feature with mark 4.  Resolve a reusable reference axis
        # constructed from the localized standard planes.
        if direction.lower() not in {"x", "y", "z"}:
            raise ValueError(f"Direction must be 'x', 'y', or 'z', got '{direction}'.")

        primary_axis = _ensure_pattern_axis(doc, direction)
        use_dir2 = count2 > 1
        secondary_axis = None
        if use_dir2:
            # The public tool accepts one direction, so choose a deterministic
            # perpendicular standard direction for a rectangular pattern.
            secondary_directions = {"x": "y", "y": "x", "z": "x"}
            secondary_axis = _ensure_pattern_axis(
                doc, secondary_directions[direction.lower()]
            )

        doc.ClearSelection2(True)
        if not doc.Extension.SelectByID2(
            primary_axis, "AXIS", 0, 0, 0, False, 1,
            win32com.client.VARIANT(pythoncom.VT_DISPATCH, None), 0,
        ):
            raise RuntimeError(f"Could not select the {direction} direction reference.")
        if use_dir2:
            # The API requires the second direction reference with mark 2.
            if not doc.Extension.SelectByID2(
                secondary_axis, "AXIS", 0, 0, 0, True, 2,
                win32com.client.VARIANT(pythoncom.VT_DISPATCH, None), 0,
            ):
                raise RuntimeError("Could not select the secondary pattern direction reference.")
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(
            feature_name, "BODYFEATURE", 0, 0, 0, True, 4, empty, 0
        ):
            raise RuntimeError(f"Could not select feature '{feature_name}'.")

        feat = doc.FeatureManager.FeatureLinearPattern4(
            count, spacing_m, count2 if use_dir2 else 1, spacing2_m if use_dir2 else 0,
            False, False, "", "", False, False,
            False, False, False, False, True, True, False, False, 0.0, 0.0,
        )
        if feat is None:
            raise RuntimeError(
                f"Linear pattern failed on feature '{feature_name}'. "
                "Ensure the feature exists and the direction axis is valid."
            )
        return {"feature": feature_name, "direction": direction,
                "count": count, "spacing": spacing,
                "count2": count2 if use_dir2 else 1,
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def circular_pattern(feature_name: str, axis: str = "z",
                             count: int = 4, angle: float = 360) -> dict:
    """Repeat a feature in a circular pattern around an axis.
    axis: 'x', 'y', or 'z' (uses the corresponding standard axis).
    count: total instances (including original). angle: total span in degrees."""

    def _impl():
        if count < 2:
            raise ValueError(f"Count must be at least 2, got {count}.")
        if angle <= 0 or angle > 360:
            raise ValueError(f"Angle must be between 0 and 360, got {angle}.")
        doc = _active_doc()

        if axis.lower() not in {"x", "y", "z"}:
            raise ValueError(f"Axis must be 'x', 'y', or 'z', got '{axis}'.")

        axis_name = _ensure_pattern_axis(doc, axis)
        doc.ClearSelection2(True)
        if not doc.Extension.SelectByID2(
            axis_name, "AXIS", 0, 0, 0, False, 1,
            win32com.client.VARIANT(pythoncom.VT_DISPATCH, None), 0,
        ):
            raise RuntimeError(f"Could not select the {axis} rotation reference.")
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(
            feature_name, "BODYFEATURE", 0, 0, 0, True, 4, empty, 0
        ):
            raise RuntimeError(f"Could not select feature '{feature_name}'.")

        angle_rad = math.radians(angle)

        feat = doc.FeatureManager.FeatureCircularPattern4(
            count, angle_rad, False,
            "", False, True, False,
        )
        if feat is None:
            raise RuntimeError(
                f"Circular pattern failed on feature '{feature_name}'. "
                "Ensure the feature and axis are valid."
            )
        return {"feature": feature_name, "axis": axis, "count": count, "angle": angle}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def hole_wizard(face_x: float, face_y: float, face_z: float,
                       hole_x: float, hole_y: float,
                       hole_type: str = "simple",
                       size: float = 6, depth: float = 20,
                       thread_standard: str = "ISO",
                       unit: Optional[str] = None) -> dict:
    """Create a hole using the Hole Wizard on a face.
    face_x/y/z: a point on the face where the hole will be placed. This ONLY
    picks which face to use -- it has no effect on where hole_x/hole_y land.
    hole_x/y: the 2D position of the hole center, in the sketch SolidWorks
    opens on that face. WARNING: for a face that isn't a named plane,
    SolidWorks -- not this tool -- decides that sketch's origin and axis
    orientation from the face's own surface parameterization, which depends
    on the feature history that produced it and is NOT derivable from
    face_x/y/z. hole_x=0, hole_y=0 happens to land on face_x/y/z for some
    faces and not others (confirmed: it does on a flat face inherited
    directly from its defining sketch, and does not on an EdgeFlange's
    folded face) -- there is no formula to predict it ahead of time. If
    placement fails with "Could not select the Hole Wizard placement point",
    the coordinates fell outside that face's own sketch bounds; adjust by
    trial and error or place at (0, 0) first and inspect the result.
    hole_type: 'simple', 'counterbore', 'countersink', 'tapped' (threaded).
    size: nominal hole diameter (or thread size for tapped).
    depth: hole depth (ignored for through-all; use depth=0 to drill through).
    thread_standard: 'ISO' or 'ANSI' metric. Hole Wizard uses metric M-size
    families; tapped holes use the matching ISO coarse pitch automatically.

    CONFIRMED DEAD END on an EdgeFlange's folded face specifically (full
    writeup: .claude/knowledge/chapa_metalica.md, section "Furo em face de
    EdgeFlange"): fails deterministically with "Ensure the face is flat",
    live-reproduced on 2026-10-04 with correct, confirmed coordinates. The
    face selection itself succeeds; HoleWizard4 refuses the face. Root cause
    not found on the server side -- don't retry with different size/depth/
    coordinates, that's not what's failing. create_sketch_on_face +
    cut_extrude doesn't work around it either (see that tool's docstring).
    Cut before folding, or tell the user this face needs a manual hole."""

    def _impl():
        if size <= 0:
            raise ValueError(f"Size must be positive, got {size}.")
        doc = _active_doc()

        fx = to_meters(face_x, unit)
        fy = to_meters(face_y, unit)
        fz = to_meters(face_z, unit)
        types = {
            # swWzdGeneralHoleTypes_e
            "simple": 2,       # swWzdHole
            "counterbore": 0,  # swWzdCounterBore
            "countersink": 1,  # swWzdCounterSink
            "tapped": 4,       # swWzdTap
        }
        kind = hole_type.lower()
        hw_type = types.get(kind)
        if hw_type is None:
            raise ValueError(f"Unknown hole_type '{hole_type}'. Use: {', '.join(types)}")

        size_m = to_meters(size, unit)
        depth_m = to_meters(depth, unit) if depth > 0 else 0.0
        end_cond = 0 if depth > 0 else 1  # 0=Blind, 1=ThroughAll
        standard_name = thread_standard.strip().upper()
        standards = {
            # swWzdHoleStandards_e and swWzdHoleStandardFastenerTypes_e.
            "ISO": {
                "standard": 8,
                "simple": 143,       # swStandardISODrillSizes
                "counterbore": 139,  # swStandardISOSocketHeadCap
                "countersink": 141,  # swStandardISOCTSKFlatHead
                "tapped": 147,       # swStandardISOTappedHole
            },
            "ANSI": {
                "standard": 1,
                "simple": 39,        # swStandardAnsiMetricDrillSizes
                "counterbore": 33,   # swStandardAnsiMetricSocketHeadCapScrew
                "countersink": 36,   # swStandardAnsiMetricFlatHead82
                "tapped": 43,        # swStandardAnsiMetricTappedHole
            },
        }
        standard = standards.get(standard_name)
        if standard is None:
            raise ValueError("thread_standard must be 'ISO' or 'ANSI'.")

        size_mm = size_m * 1000.0
        # "simple" maps to swStandardISODrillSizes/swStandardAnsiMetricDrillSizes
        # (143/39) -- a table of bare drill diameters ("8.5", not "M8"). The
        # fastener-size tables used by counterbore/countersink/tapped (socket
        # head cap, flat head, tapped hole) DO expect the "M" prefix. Passing
        # "M8" to the drill-size table finds no match, so Hole Wizard silently
        # falls back to its last-used favorite (observed: an ANSI inch size)
        # instead of raising -- this was BUG 3 in bug_report_solidworks_mcp.txt.
        if kind == "simple":
            size_label = f"{size_mm:g}"
        else:
            size_label = f"M{int(round(size_mm))}" if math.isclose(size_mm, round(size_mm), abs_tol=1e-6) else f"M{size_mm:g}"
        drill_angle = math.radians(118)

        if kind == "simple":
            values = [1.0, drill_angle, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0, -1.0, -1.0, -1.0, -1.0]
        elif kind == "counterbore":
            counterbore_depth = min(depth_m, size_m) if depth_m > 0 else size_m
            values = [size_m * 2, counterbore_depth, 0.0, 1.0, drill_angle, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        elif kind == "countersink":
            values = [size_m * 2, math.radians(90), 0.0, 1.0, drill_angle, 0.0, 0.0, 0.0, 0.0, -1.0, -1.0, -1.0]
        else:
            # Hole Wizard expects a complete metric thread designation, rather
            # than just its nominal diameter, for a tapped hole.
            coarse_pitches = {
                1.0: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 1.8: 0.35,
                2.0: 0.4, 2.5: 0.45, 3.0: 0.5, 3.5: 0.6, 4.0: 0.7,
                5.0: 0.8, 6.0: 1.0, 8.0: 1.25, 10.0: 1.5, 12.0: 1.75,
                14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 22.0: 2.5,
                24.0: 3.0, 27.0: 3.0, 30.0: 3.5, 33.0: 3.5, 36.0: 4.0,
                39.0: 4.0, 42.0: 4.5, 45.0: 4.5, 48.0: 5.0, 52.0: 5.0,
                56.0: 5.5, 60.0: 5.5, 64.0: 6.0, 68.0: 6.0, 72.0: 6.0,
            }
            pitch = next(
                (p for nominal, p in coarse_pitches.items() if math.isclose(size_mm, nominal, abs_tol=1e-6)),
                None,
            )
            if pitch is None:
                supported = ", ".join(f"M{nominal:g}" for nominal in coarse_pitches)
                raise ValueError(f"Tapped holes support these metric nominal sizes: {supported}.")
            pitch_label = (
                f"{pitch:.1f}"
                if math.isclose(pitch, round(pitch, 1), abs_tol=1e-9)
                else f"{pitch:.2f}".rstrip("0")
            )
            size_label = f"{size_label}x{pitch_label}"
            thread_depth = depth_m if depth_m > 0 else size_m * 3
            values = [
                size_m * 0.8, thread_depth, thread_depth,
                0.0, 0.0, 0.0, 0.0, drill_angle,
                0.0, float(end_cond), 0.0, 0.0,
            ]

        # A Hole Wizard feature is positioned by preselected sketch points.
        # Create the point on the requested face before calling HoleWizard4;
        # creating it afterwards cannot change the hole position.
        doc.ClearSelection2(True)
        if not _select_by_id(doc, "", "FACE", fx, fy, fz):
            raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")
        doc.InsertSketch2(True)
        point = None
        try:
            point = doc.SketchManager.CreatePoint(to_meters(hole_x, unit), to_meters(hole_y, unit), 0)
        finally:
            if doc.SketchManager.ActiveSketch is not None:
                doc.InsertSketch2(True)

        doc.ClearSelection2(True)
        if point is None or not point.Select2(False, 0):
            raise RuntimeError("Could not select the Hole Wizard placement point.")

        feat = doc.FeatureManager.HoleWizard4(
            hw_type,
            standard["standard"],
            standard[kind],
            size_label,
            end_cond,
            size_m,
            depth_m,
            *values,
            "",     # ThreadClass (used by ANSI inch threads only)
            False,  # RevDir
            False,  # UseFeatScope
            True,   # UseAutoSelect
            False,  # AssemblyFeatureScope
            False,  # AutoSelectComponents
            False,  # PropagateFeatureToParts
        )
        if feat is None:
            raise RuntimeError(
                f"Hole Wizard failed on face at ({face_x},{face_y},{face_z}). "
                f"Ensure the face is flat and '{size_label}' is available in the {standard_name} table."
            )

        feature = win32com.client.Dispatch(feat)

        return {
            "feature": feature.Name,
            "hole_type": kind,
            "size": size,
            "depth": depth if depth > 0 else "through",
            "thread_standard": standard_name,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


# ===========================================================================
# Weldment / Structural Member tools
# ===========================================================================

def _weldment_profile_roots() -> list:
    """Directories that may hold 'weldment profiles/<standard>/<type>.sldlfp'."""
    roots = []
    app = _connect()

    # SolidWorks File Locations preference for weldment profiles (index 233 =
    # swFileLocationsWeldmentProfiles). May contain several ';'-separated paths.
    try:
        pref = app.GetUserPreferenceStringValue(233)
        if pref:
            roots.extend(p for p in pref.split(";") if p)
    except Exception:
        pass

    # Standard install location: ProgramData (this is where profiles actually live).
    for year in SW_YEAR_RANGE:
        roots.append(rf"C:\ProgramData\SOLIDWORKS\SOLIDWORKS {year}\weldment profiles")
        roots.append(rf"C:\ProgramData\SolidWorks\SOLIDWORKS {year}\weldment profiles")
    return roots


def _get_weldment_profile_path(standard: str, profile_type: str) -> str:
    """Locate the weldment profile file '<standard>/<type>.sldlfp'.

    In SolidWorks a profile FILE holds one shape family (e.g. 'square tube') and
    each SIZE (e.g. '40 x 40 x 4') is a configuration inside that file. So we
    resolve to the .sldlfp here; the size is applied later as a configuration.
    """
    for root in _weldment_profile_roots():
        if not os.path.isdir(root):
            continue
        # Match the standard folder case-insensitively.
        for std_folder in os.listdir(root):
            if std_folder.lower() != standard.lower():
                continue
            std_path = os.path.join(root, std_folder)
            if not os.path.isdir(std_path):
                continue
            # Exact type file, else a case-insensitive / prefix match.
            exact = os.path.join(std_path, f"{profile_type}.sldlfp")
            if os.path.isfile(exact):
                return exact
            for f in os.listdir(std_path):
                if f.lower().endswith(".sldlfp") and f[:-7].lower() == profile_type.lower():
                    return os.path.join(std_path, f)
            for f in os.listdir(std_path):
                if f.lower().endswith(".sldlfp") and profile_type.lower() in f.lower():
                    return os.path.join(std_path, f)

    # Build a hint listing what IS available.
    available = {}
    for root in _weldment_profile_roots():
        if os.path.isdir(root):
            for std_folder in os.listdir(root):
                sp = os.path.join(root, std_folder)
                if os.path.isdir(sp):
                    available[std_folder] = [f[:-7] for f in os.listdir(sp) if f.lower().endswith(".sldlfp")]
            break
    raise RuntimeError(
        f"Weldment profile not found: standard='{standard}', type='{profile_type}'. "
        f"Available: {available if available else '(profile library not installed)'}"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_weldment_profile(
    standard: str,
    profile_type: str,
    size: str,
    sketch_name: str,
    groups: Optional[list] = None,
    unit: Optional[str] = None,
) -> dict:
    """Create a structural member (weldment profile) along sketch segments.

    This is the SolidWorks Weldments > Structural Member command. It sweeps a
    standard profile (I-beam, tube, channel, angle, etc.) along lines drawn in
    a 3D or 2D sketch.

    standard: profile library (e.g. 'iso', 'ansi inch', 'ansi metric', 'din').
    profile_type: shape family (e.g. 'c channel', 'square tube', 'angle iron',
                  'rectangular tube', 'pipe', 'w profile').
    size: specific size (e.g. '80 x 40 x 4', 'C6 x 8.2', 'W6 x 9').
    sketch_name: name of the sketch whose line segments define the paths.
    groups: optional list of segment groups. Each group is a list of 0-based
            segment indices within the sketch. If omitted, all segments are
            used as a single group."""

    def _impl():
        doc = _active_doc()

        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        # Resolve <standard>/<type>.sldlfp; the size is a configuration inside it.
        profile_path = _get_weldment_profile_path(standard, profile_type)

        # Structural members require both a weldment base feature and one or
        # more IStructuralMemberGroup objects. Selecting the sketch itself (or
        # passing None for Groups) does not populate those groups.
        sketch_feature = None
        for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
            candidate = win32com.client.Dispatch(raw_feature)
            feature_type = candidate.GetTypeName2
            if callable(feature_type):
                feature_type = feature_type()
            if candidate.Name == sketch_name and feature_type in ("ProfileFeature", "3DProfileFeature"):
                sketch_feature = candidate
                break
        if sketch_feature is None:
            raise RuntimeError(f"Sketch '{sketch_name}' not found.")

        sketch = win32com.client.Dispatch(sketch_feature.GetSpecificFeature2)
        segments = tuple(sketch.GetSketchSegments or ())
        if not segments:
            raise RuntimeError(f"Sketch '{sketch_name}' has no segments for a structural member.")

        if not any(
            win32com.client.Dispatch(raw_feature).GetTypeName2 == "WeldmentFeature"
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
        ):
            # InsertWeldmentFeature is exposed as a zero-argument COM property.
            doc.FeatureManager.InsertWeldmentFeature

        requested_groups = groups if groups is not None else [list(range(len(segments)))]
        if not isinstance(requested_groups, list) or not requested_groups:
            raise ValueError("groups must be a non-empty list of segment-index lists.")

        member_groups = []
        for group_indices in requested_groups:
            if not isinstance(group_indices, list) or not group_indices:
                raise ValueError("Each groups entry must be a non-empty list of segment indices.")
            if any(
                not isinstance(index, int) or index < 0 or index >= len(segments)
                for index in group_indices
            ):
                raise ValueError(
                    f"Segment indices must be between 0 and {len(segments) - 1}; got {group_indices}."
                )
            group = win32com.client.Dispatch(doc.FeatureManager.CreateStructuralMemberGroup)
            group_segments = [win32com.client.Dispatch(segments[index]) for index in group_indices]
            # Win32 COM must receive a SAFEARRAY(VT_DISPATCH); a Python tuple is
            # accepted silently but creates an empty IStructuralMemberGroup.
            group.Segments = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, group_segments
            )
            if group.GetSegmentsCount != len(group_segments):
                raise RuntimeError(
                    f"Could not assign sketch segments {group_indices} to a structural-member group."
                )
            member_groups.append(group)

        groups_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, member_groups
        )

        # InsertStructuralWeldment5(Path, ConnectedSegmentsOption, AllowProtrusion,
        #   Groups, ConfigurationName). 1 is swConnectedSegments_SimpleCut;
        # 0 is not a valid connected-segment option and returns no feature.
        feat = doc.FeatureManager.InsertStructuralWeldment5(
            profile_path,
            1,       # swConnectedSegments_SimpleCut
            False,   # AllowProtrusion
            groups_variant,
            size,    # ConfigurationName (the profile size)
        )

        if feat is None:
            raise RuntimeError(
                f"Structural member creation returned no feature. Profile: '{profile_path}', "
                f"size(config)='{size}'. Confirm the size matches a configuration in the "
                "profile file, and that the sketch has connected line segments. "
                "NOTE: the SolidWorks structural-member API is finicky; if this persists, "
                "insert the member interactively (Weldments > Structural Member)."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "standard": standard,
            "profile_type": profile_type,
            "size": size,
            "sketch": sketch_name,
            "profile_path": profile_path,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def trim_extend_structural(
    body_to_trim: str,
    trim_boundary: str,
    trim_type: str = "trim",
) -> dict:
    """Trim or extend a structural member (weldment body) at an intersection.

    When two beams cross, this trims one so it fits against the other, like a
    welded joint. This is the SolidWorks Weldments > Trim/Extend command.

    body_to_trim: name of the structural body to be trimmed (from list_features,
                  look for 'SolidBody' type entries or use the cut-list name).
    trim_boundary: name of the body or face that acts as the cutting boundary.
    trim_type: 'trim' (remove material at intersection) or 'extend' (grow to
               reach the boundary)."""

    def _impl():
        doc = _active_doc()

        # EndCond for InsertWeldmentTrimFeature2: 0 = trim to a body/face boundary.
        types = {"trim": 0, "extend": 1}
        tt = types.get(trim_type.lower())
        if tt is None:
            raise ValueError(f"trim_type must be 'trim' or 'extend', got '{trim_type}'.")

        def _find_body(name):
            for raw in (doc.GetBodies2(0, True) or []):
                b = win32com.client.Dispatch(raw)
                bn = b.Name if not callable(getattr(b, "Name", None)) else b.Name()
                if bn == name:
                    return b
            return None

        trim_body = _find_body(body_to_trim)
        if trim_body is None:
            raise RuntimeError(
                f"Body to trim '{body_to_trim}' not found. Use list_features / the cut-list "
                "to see body names."
            )
        boundary_body = _find_body(trim_boundary)
        if boundary_body is None:
            raise RuntimeError(
                f"Trim boundary '{trim_boundary}' not found. Use list_features to see body names."
            )

        bodies_to_trim = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [trim_body._oleobj_])
        bodies_or_faces = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [boundary_body._oleobj_])

        # InsertWeldmentTrimFeature2(EndCond, Options, GapValue, BodiesToTrim, BodiesOrFaces)
        feat = doc.FeatureManager.InsertWeldmentTrimFeature2(
            tt, 0, 0.0, bodies_to_trim, bodies_or_faces,
        )

        if feat is None:
            raise RuntimeError(
                f"Trim/Extend failed. body='{body_to_trim}', boundary='{trim_boundary}'. "
                "Ensure both are weldment bodies and they intersect (trim) or can reach (extend)."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "body_trimmed": body_to_trim,
            "boundary": trim_boundary,
            "type": trim_type,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_gusset(
    thickness: float = 5,
    x1: float = 0, y1: float = 0, z1: float = 0,
    x2: float = 0, y2: float = 0, z2: float = 0,
    profile: str = "triangular",
    leg1: float = 50, leg2: float = 50, leg3: float = 25,
    profile_angle: float = 45,
    unit: Optional[str] = None,
) -> dict:
    """Add a gusset plate (reinforcement triangle) between two planar faces.

    A gusset is the triangular steel plate welded at the junction of two beams
    or between a column and a base plate. Select two flat faces that meet at
    an edge or corner.

    x1/y1/z1: a point on the first face.
    x2/y2/z2: a point on the second face.
    thickness: plate thickness.
    profile: 'triangular' (straight hypotenuse) or 'flat' (polygonal infill).
    leg1/leg2: the gusset's two legs along the supporting faces.
    leg3: the third distance of a 'flat' (polygonal) profile; ignored for a
    triangular one.
    profile_angle: the polygonal profile's angle, in degrees.

    The legs used to be hard-coded as 50 and converted with the CALLER's unit,
    so unit="m" produced a 50-metre gusset and there was no way to ask for the
    size the structure actually needs."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        for label, value in (("leg1", leg1), ("leg2", leg2)):
            if value <= 0:
                raise ValueError(f"{label} must be positive, got {value}.")
        if not 0 < profile_angle < 180:
            raise ValueError(
                f"profile_angle must be between 0 and 180 degrees, got {profile_angle}."
            )
        if profile.lower() == "flat" and leg3 <= 0:
            raise ValueError(
                f"A 'flat' (polygonal) gusset needs a positive leg3, got {leg3}."
            )
        doc = _active_doc()
        t_m = to_meters(thickness, unit)

        # InsertGussetFeature2's BIsProfile flag is True for a polygon and
        # False for a triangle. The older implementation inverted this flag
        # and passed None instead of the required array of supporting faces.
        polygon_profiles = {"triangular": False, "flat": True}
        is_polygon = polygon_profiles.get(profile.lower())
        if is_polygon is None:
            raise ValueError(f"profile must be 'triangular' or 'flat', got '{profile}'.")

        f1x, f1y, f1z = to_meters(x1, unit), to_meters(y1, unit), to_meters(z1, unit)
        f2x, f2y, f2z = to_meters(x2, unit), to_meters(y2, unit), to_meters(z2, unit)

        face1 = _planar_face_at_point(doc, f1x, f1y, f1z)
        face2 = _planar_face_at_point(doc, f2x, f2y, f2z)
        if face1._oleobj_ == face2._oleobj_:
            raise ValueError("The two gusset points resolve to the same face; select two supporting faces.")
        supporting_faces = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
            [face1._oleobj_, face2._oleobj_],
        )

        # InsertGussetFeature2(Depth, DirType, LocType, BIsProfile, ProfileD1,
        #   ProfileD2, ProfileD3, ProfileAngle, ProfileD4, BOffset, DProfileOffset,
        #   CrvIndex, BReverseDir, BReverseFace, BUseLenDim, Faces) — 16 args.
        d1 = to_meters(leg1, unit)
        d2 = to_meters(leg2, unit)
        # A polygonal gusset needs a third distance plus either an angle or a
        # fourth distance. Use the documented d3 + angle form; setting d3 to
        # zero produces no profile in SolidWorks.
        d3 = to_meters(leg3, unit) if is_polygon else 0.0
        d4 = 0.0
        use_length_dimension = False
        feat = doc.FeatureManager.InsertGussetFeature2(
            t_m,               # Depth (thickness)
            1,                 # DirType (swGussetThicknessBothSides=1)
            1,                 # LocType (swGussetProfileLocationCenter=1)
            is_polygon,        # BIsProfile (True = polygon, False = triangle)
            d1,                # ProfileD1
            d2,                # ProfileD2
            d3,                # ProfileD3 (required for polygon profiles)
            math.radians(profile_angle),  # ProfileAngle
            d4,                # ProfileD4 (used when BUseLenDim is True)
            False,             # BOffset
            0.0,               # DProfileOffset
            0,                 # CrvIndex
            False,             # BReverseDir
            False,             # BReverseFace
            use_length_dimension,  # BUseLenDim
            supporting_faces,  # array of the two supporting faces
        )

        if feat is None:
            raise RuntimeError(
                "Gusset failed. Ensure two flat faces are selected that share an edge "
                "or meet at a corner (typical: a beam face and a base plate face)."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "thickness": thickness,
            "profile": profile,
            "leg1": leg1,
            "leg2": leg2,
            "leg3": leg3 if is_polygon else None,
            "profile_angle_deg": profile_angle,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_end_cap(
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    thickness: float = 2,
    offset: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Add an end cap to a structural member (weldment body).

    An end cap is a thin plate welded to close the open end of a tube, channel,
    or other hollow profile. Select the open face at the end of the structural
    member.

    KNOWN ISSUE (SolidWorks 2025): InsertEndCapFeature3 has been observed to
    return None regardless of arguments -- including the exact values from
    SolidWorks' own API-help example, every value of its direction enum, and
    both one- and two-face selections. This tool correctly identifies and
    selects the target face; if it still fails, that is very likely this
    SolidWorks-side limitation rather than a bad face_x/y/z.

    face_x/y/z: a point on the open face at the end of the profile.
    thickness: cap plate thickness.
    offset: inward offset from the face (0 = flush with the end)."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        doc = _active_doc()
        t_m = to_meters(thickness, unit)
        off_m = to_meters(offset, unit) if offset != 0 else 0.0

        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        requested_point = (fx, fy, fz)

        # A hollow member's end face shares its edges with the side faces.
        # SelectByID2 at a coordinate can therefore select a side face even
        # when the coordinate is on the end. Locate the closest planar end
        # face by its bounding box instead.
        #
        # A hollow profile's end is an annular (picture-frame) face: its
        # centre has no material, so a caller relaying a pick_point from
        # list_faces (which projects away from that void) naturally lands
        # right on the inner boundary. Re-deriving the face via a ray cast
        # from that boundary point is degenerate -- the ray grazes the
        # material/void edge instead of crossing it. The bounding-box match
        # below already identifies the correct IFace2 object, so select it
        # directly instead of discarding it and re-finding it by ray.
        end_face_candidates = []
        for raw_body in doc.GetBodies2(0, True) or ():
            body = win32com.client.Dispatch(raw_body)
            for raw_face in body.GetFaces() or ():
                face = win32com.client.Dispatch(raw_face)
                box = tuple(face.GetBox)
                mins, maxs = box[:3], box[3:]
                spans = [maxs[index] - mins[index] for index in range(3)]
                axis = min(range(3), key=lambda index: spans[index])
                if spans[axis] > 1e-7:
                    continue
                distance_sq = sum(
                    (
                        0.0
                        if mins[index] <= requested_point[index] <= maxs[index]
                        else min(
                            abs(requested_point[index] - mins[index]),
                            abs(requested_point[index] - maxs[index]),
                        )
                    ) ** 2
                    for index in range(3)
                )
                end_face_candidates.append((distance_sq, face))

        if not end_face_candidates:
            raise RuntimeError(
                f"No planar structural-member end face was found near ({face_x}, {face_y}, {face_z})."
            )

        _, end_face = min(end_face_candidates, key=lambda item: item[0])
        doc.ClearSelection2(True)
        if not end_face.Select4(False, pythoncom.Nothing):
            raise RuntimeError(
                f"Could not select the structural-member end face near ({face_x}, {face_y}, {face_z})."
            )

        # InsertEndCapFeature3 is the supported API in SolidWorks 2025. The
        # final value must be a swEndCapThicknessDirection_e value; 1 means
        # extend the cap outward from the selected end.
        feat = doc.FeatureManager.InsertEndCapFeature3(
            t_m,               # Depth (cap thickness)
            bool(offset != 0), # BIsGivenOffset
            False,             # BIsChamfer
            off_m,             # OffsetValue
            0.5,               # WallThicknessRatio
            0.0,               # ChamferValue / fillet radius
            False,             # BIsCornerTreatment
            0.0,               # DepthOffset
            False,             # BIsReverse
            1,                 # swExtendOutward
        )

        if feat is None:
            raise RuntimeError(
                f"End cap failed on face at ({face_x}, {face_y}, {face_z}). "
                "Ensure the face is the open end of a structural/weldment member."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "thickness": thickness,
            "offset": offset,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_3d_sketch() -> dict:
    """Open a new 3D sketch.

    A 3D sketch lets you draw lines, arcs, and splines in free 3D space, not
    constrained to a single plane. This is essential for defining the paths of
    structural members (weldments) in space — e.g. the frame of a platform
    or a tank support structure.

    After calling this, use draw_line_3d to add geometry, then close_sketch
    when done."""

    def _impl():
        doc = _active_doc()
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        doc.SketchManager.Insert3DSketch(True)

        active = doc.SketchManager.ActiveSketch
        if active is None:
            raise RuntimeError("Failed to open a 3D sketch.")

        name = active.Name if hasattr(active, "Name") else "3DSketch"
        return {"sketch": name, "type": "3D"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def draw_line_3d(
    x1: float = 0, y1: float = 0, z1: float = 0,
    x2: float = 100, y2: float = 0, z2: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Draw a line in a 3D sketch from (x1,y1,z1) to (x2,y2,z2).

    Use inside a 3D sketch (opened with create_3d_sketch). Unlike draw_line
    which only works in 2D (Z=0), this places line segments anywhere in 3D
    space — essential for defining structural member paths."""

    def _impl():
        if x1 == x2 and y1 == y2 and z1 == z2:
            raise ValueError("Start and end points are identical — line has zero length.")
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            raise RuntimeError("No sketch is active. Call create_3d_sketch first.")
        line = doc.SketchManager.CreateLine(
            to_meters(x1, unit), to_meters(y1, unit), to_meters(z1, unit),
            to_meters(x2, unit), to_meters(y2, unit), to_meters(z2, unit),
        )
        if line is None:
            raise RuntimeError("Failed to draw 3D line.")
        return {
            "from": [x1, y1, z1],
            "to": [x2, y2, z2],
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


# ===========================================================================
# Sheet Metal tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_base_flange(
    thickness: float = 2,
    depth: float = 100,
    direction: str = "blind",
    both_directions: bool = False,
    bend_radius: float = 1,
    unit: Optional[str] = None,
) -> dict:
    """Create a sheet metal base flange from the last sketch.

    This is the first step to create a sheet metal part. Draw a closed sketch
    profile, then call this to turn it into a flat sheet metal body. Add edge
    flanges afterwards to create folded walls.

    thickness: sheet metal gauge thickness.
    depth, direction, and both_directions are retained for API compatibility.
    For a closed base-flange outline, SolidWorks creates a flat sheet whose
    profile dimensions come from the sketch; its thickness comes from
    ``thickness``.
    bend_radius: default inside bend radius for all bends."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        if depth <= 0:
            raise ValueError(f"Depth must be positive, got {depth}.")
        if bend_radius <= 0:
            raise ValueError(f"Bend radius must be positive, got {bend_radius}.")

        doc = _active_doc()
        t_m = to_meters(thickness, unit)
        d_m = to_meters(depth, unit)
        br_m = to_meters(bend_radius, unit)

        sketch_name = _select_last_sketch(doc)

        # InsertSheetMetalBaseFlange2 is obsolete in the current API and
        # returns None under the SolidWorks 2025 Python COM binding. Create a
        # base-flange definition instead, initialize its sheet-metal data, and
        # then create the feature. The definition object is returned as an
        # untyped IDispatch, so Initialize and GetCustomBendAllowance must be
        # invoked by their documented DISPIDs with explicit COM types.
        base_flange = win32com.client.Dispatch(doc.FeatureManager.CreateDefinition(34))
        custom_bend_allowance = win32com.client.Dispatch(
            base_flange._oleobj_.InvokeTypes(38, 0, 1, (9, 0), ())
        )
        custom_bend_allowance.Type = 3  # swBendAllowanceDirect
        custom_bend_allowance.BendAllowance = t_m * 0.5

        # swEndCondThroughAll = 1. This is the documented base-flange
        # definition setup; the actual closed outline supplies the sheet size.
        base_flange.D1EndConditionType = 1
        base_flange.D1EndConditionDistance = d_m
        base_flange.D2EndConditionType = 1
        base_flange.D2EndConditionDistance = d_m
        base_flange.OffsetDirections = 2
        base_flange.ReverseDirection = False
        base_flange.OverrideDefaultSheetMetalParameters = True
        base_flange.Thickness = t_m
        base_flange.BendRadius = br_m
        base_flange._oleobj_.InvokeTypes(
            52, 0, 1, (24, 0),
            ((11, 1), (11, 1), (9, 1), (11, 1), (3, 1), (11, 1),
             (5, 1), (5, 1), (5, 1)),
            False,  # UseMaterialSheetMetalParameters
            False,  # OverrideDefaultBendAllowance
            custom_bend_allowance,
            False,  # OverrideDefaultBendRelief
            1,      # swSheetMetalReliefRectangular
            True,   # UseReliefRatio
            0.5,    # ReliefRatio
            0.0,    # ReliefWidth
            0.0,    # ReliefDepth
        )
        feat = doc.FeatureManager.CreateFeature(base_flange)

        if feat is None:
            raise RuntimeError(
                f"Base flange failed on sketch '{sketch_name}'. "
                "Ensure the sketch has a valid open or closed profile."
            )

        return {
            "sketch": sketch_name,
            "thickness": thickness,
            "depth": depth,
            "bend_radius": bend_radius,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_sheet_metal_bend(
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    bend_radius: float = 1,
    unit: Optional[str] = None,
) -> dict:
    """Convert a shelled/thin part into sheet metal, adding bends at sharp edges.

    NOTE: SolidWorks has no single "bend this edge" API call. To fold a lip from
    an edge, use add_sheet_metal_edge_flange. This tool instead performs the
    "Insert Bends" (rip-and-bend) operation: it turns a constant-thickness thin
    part into a sheet metal body, rounding its sharp internal edges into bends of
    the given radius — the classic way to convert a folded shell into sheet metal.

    face_x/y/z: a point on the fixed face that stays put while the rest unfolds.
    bend_radius: inside radius applied to the auto-created bends."""

    def _impl():
        if bend_radius <= 0:
            raise ValueError(f"Bend radius must be positive, got {bend_radius}.")
        doc = _active_doc()

        doc.ClearSelection2(True)
        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
            raise RuntimeError(
                f"No fixed face found at ({face_x}, {face_y}, {face_z}). "
                "Pick a point on the flat face that should stay in place."
            )

        r_m = to_meters(bend_radius, unit)

        sheet_metal_before = {
            win32com.client.Dispatch(raw_feature).Name
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "SheetMetal"
        }
        # InsertBends2 belongs to IPartDoc, not IFeatureManager. It requires
        # either a K-factor or a bend allowance; -1 for both silently fails
        # on a newly shelled part. It returns a Boolean rather than an IFeature.
        ok = doc.InsertBends2(
            r_m, "", 0.5, -1.0, True, 0.5, True,
        )

        if not ok:
            raise RuntimeError(
                "Insert-bends failed. The active part must be a constant-thickness "
                "thin/shelled body (use shell_body first), and you must select a "
                "fixed face. To add a folded lip instead, use add_sheet_metal_edge_flange."
            )

        sheet_metal_after = [
            win32com.client.Dispatch(raw_feature).Name
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "SheetMetal"
        ]
        feat_name = next(
            (name for name in sheet_metal_after if name not in sheet_metal_before),
            sheet_metal_after[-1] if sheet_metal_after else "SheetMetal",
        )

        return {
            "feature": feat_name,
            "bend_radius": bend_radius,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_sheet_metal_edge_flange(
    edge_x: float = 0, edge_y: float = 0, edge_z: float = 0,
    flange_length: float = 20,
    flange_angle: float = 90,
    unit: Optional[str] = None,
) -> dict:
    """Add an edge flange to a sheet metal part.

    Extends a new flange from an edge of the sheet metal body — like folding
    a lip or tab upward/downward from an edge. Common for creating walls on
    trays, tank flanges, and enclosures.

    edge_x/y/z: a point on the edge to add the flange to.
    flange_length: length of the new flange (how far it extends).
    flange_angle: angle in degrees (90 = perpendicular to the face)."""

    def _impl():
        if flange_length <= 0:
            raise ValueError(f"Flange length must be positive, got {flange_length}.")
        if flange_angle <= 0 or flange_angle > 180:
            raise ValueError(f"Flange angle must be between 0 and 180 degrees, got {flange_angle}.")

        doc = _active_doc()
        doc.ClearSelection2(True)
        ex, ey, ez = to_meters(edge_x, unit), to_meters(edge_y, unit), to_meters(edge_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "EDGE", ex, ey, ez, False, 0, empty, 0):
            raise RuntimeError(f"No edge found at ({edge_x}, {edge_y}, {edge_z}).")

        fl_m = to_meters(flange_length, unit)
        angle_rad = math.radians(flange_angle)
        edge = doc.SelectionManager.GetSelectedObject6(1, 0)

        # An edge flange is not created from scalar parameters alone.  The
        # SolidWorks API requires a profile sketch associated with every
        # selected edge.  Create the profile using InsertSketchForEdgeFlange,
        # convert the selected model edge into sketch geometry, then add the
        # profile and edge to the current EdgeFlangeFeatureData definition.
        #
        # GetActiveSketch2 and GetSketchSegments are exposed through the COM
        # type library but are not always resolved by late-bound Python COM.
        # Their documented DISPIDs are used below so this remains compatible
        # with the SolidWorks 2025 late-bound connection used by this server.
        sketch_feature = doc.InsertSketchForEdgeFlange(edge, angle_rad, False)
        if sketch_feature is None:
            raise RuntimeError(
                "SolidWorks could not create the edge-flange profile sketch. "
                "Ensure the selected edge is linear and belongs to a sheet metal body."
            )

        # IFeature::Select2(False, 0), invoked directly because the returned
        # feature is an untyped IDispatch object in the Python COM binding.
        if not sketch_feature._oleobj_.InvokeTypes(
            67, 0, 1, (11, 0), ((11, 1), (3, 1)), False, 0
        ):
            raise RuntimeError("SolidWorks could not select the edge-flange profile sketch.")

        sketch_open = False
        try:
            doc.EditSketch()
            sketch_open = True
            raw_sketch = doc._oleobj_.InvokeTypes(66054, 0, 1, (9, 0), ())
            edge_flange_sketch = win32com.client.Dispatch(raw_sketch)

            doc.ClearSelection2(True)
            if not doc.Extension.SelectByID2("", "EDGE", ex, ey, ez, False, 0, empty, 0):
                raise RuntimeError(
                    "SolidWorks could not reselect the edge while creating the flange profile."
                )
            if not doc.SketchManager.SketchUseEdge2(False):
                raise RuntimeError("SolidWorks could not convert the selected edge into the flange profile.")

            sketch_segments = edge_flange_sketch._oleobj_.InvokeTypes(37, 0, 1, (12, 0), ())
            if not sketch_segments:
                raise RuntimeError("SolidWorks did not create sketch geometry for the selected edge.")

            base_line = win32com.client.Dispatch(sketch_segments[0])
            start_point = win32com.client.Dispatch(
                base_line._oleobj_.InvokeTypes(5, 0, 1, (9, 0), ())
            )
            end_point = win32com.client.Dispatch(
                base_line._oleobj_.InvokeTypes(7, 0, 1, (9, 0), ())
            )
            start_x = start_point._oleobj_.InvokeTypes(1, 0, 2, (5, 0), ())
            start_y = start_point._oleobj_.InvokeTypes(2, 0, 2, (5, 0), ())
            end_x = end_point._oleobj_.InvokeTypes(1, 0, 2, (5, 0), ())
            end_y = end_point._oleobj_.InvokeTypes(2, 0, 2, (5, 0), ())

            # Create a valid open flange profile.  Its first line is the
            # converted model edge; the remaining five lines define the lip
            # height and a small tapered top, following the API example.
            edge_width = end_x - start_x
            doc.SetAddToDB(True)
            doc.SetDisplayWhenAdded(False)
            try:
                doc.CreateLine2(start_x, start_y, 0, start_x, start_y + fl_m, 0)
                doc.CreateLine2(
                    start_x, start_y + fl_m, 0,
                    start_x + 0.1 * edge_width, start_y + 1.25 * fl_m, 0,
                )
                doc.CreateLine2(
                    start_x + 0.1 * edge_width, start_y + 1.25 * fl_m, 0,
                    end_x - 0.1 * edge_width, start_y + 1.25 * fl_m, 0,
                )
                doc.CreateLine2(
                    end_x - 0.1 * edge_width, start_y + 1.25 * fl_m, 0,
                    end_x, end_y + fl_m, 0,
                )
                doc.CreateLine2(end_x, end_y, 0, end_x, end_y + fl_m, 0)
            finally:
                doc.SetDisplayWhenAdded(True)
                doc.SetAddToDB(False)

            doc.InsertSketch2(True)
            sketch_open = False
        except Exception:
            if sketch_open:
                try:
                    doc.InsertSketch2(True)
                except Exception:
                    pass
            raise

        flange_edges = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (edge,)
        )
        flange_sketches = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, (edge_flange_sketch,)
        )
        flange_data = win32com.client.Dispatch(doc.FeatureManager.CreateDefinition(37))
        add_error = flange_data._oleobj_.InvokeTypes(
            37, 0, 1, (3, 0), ((12, 1), (12, 1)), flange_edges, flange_sketches
        )
        if add_error != 0:
            raise RuntimeError(f"SolidWorks rejected the edge-flange profile (error code {add_error}).")

        # swFlangeOffsetBlind=1, swFlangeDimTypeInnerVirtualSharp=2, and
        # swFlangePositionTypeMaterialInside=1.  Reuse the parent sheet
        # metal feature's bend allowance and relief settings.
        flange_data.UseDefaultBendRadius = False
        flange_data.BendRadius = 0.001
        flange_data.GapDistance = 0.001
        flange_data.BendAngle = angle_rad
        flange_data.LockAngle = True
        flange_data.OffsetType = 1
        flange_data.OffsetDistance = fl_m
        flange_data.OffsetDimType = 2
        flange_data.PositionType = 1
        flange_data.UsePositionOffset = True
        flange_data.PositionOffsetType = 1
        flange_data.PositionOffsetDistance = 0.01
        flange_data.UseDefaultBendAllowance = True
        flange_data.UseDefaultBendRelief = True
        feat = doc.FeatureManager.CreateFeature(flange_data)

        if feat is None:
            raise RuntimeError(
                f"Edge flange failed at ({edge_x}, {edge_y}, {edge_z}). "
                "Ensure the edge belongs to a sheet metal body and is a linear edge."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "flange_length": flange_length,
            "flange_angle": flange_angle,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def flatten_sheet_metal(state: str = "flat") -> dict:
    """Put a sheet metal part into its flat or folded state.

    Shows the sheet metal body unfolded into a single flat shape — exactly
    how it should be cut from a flat plate on a CNC, laser, or plasma cutter.

    state:
      "flat"   (default) ensure the flat pattern is showing. Idempotent:
               calling it twice leaves the part flat both times.
      "folded" ensure the part is folded back to 3D.
      "toggle" flip whichever state it is in now.

    The default used to be the toggle, which made a retry after a timeout
    silently fold the part again."""

    def _impl():
        # Validate before touching COM: rejecting a typo should not need a
        # SolidWorks connection, and it makes the rule testable on its own.
        wanted = state.strip().lower()
        if wanted not in ("flat", "folded", "toggle"):
            raise ValueError(
                f"state must be 'flat', 'folded' or 'toggle', got '{state}'."
            )
        doc = _active_doc()

        feat = doc.FirstFeature
        flat_pattern = None
        while feat is not None:
            try:
                tname = feat.GetTypeName2
                if tname in ("FlatPattern", "SMFlatPattern", "SM3dBend"):
                    flat_pattern = feat
                    break
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature
            except Exception:
                break

        if flat_pattern is not None:
            is_suppressed = flat_pattern.IsSuppressed
            if callable(is_suppressed):
                is_suppressed = is_suppressed()
            currently = "folded" if is_suppressed else "flat"
            target = ("folded" if currently == "flat" else "flat") \
                if wanted == "toggle" else wanted

            if target == currently:
                return {"state": currently, "action": "no change needed",
                        "requested": wanted, "changed": False}

            flat_pattern.Select2(False, 0)
            # These zero-argument ModelDoc2 commands are exposed as
            # already-invoked Boolean properties by late-bound pywin32.
            # Adding parentheses tries to call that Boolean and raises
            # TypeError after SolidWorks has already changed the state.
            if target == "flat":
                changed = doc.EditUnsuppress2
                action = "unsuppressed flat-pattern"
            else:
                changed = doc.EditSuppress2
                action = "suppressed flat-pattern (back to 3D)"
            doc.ClearSelection2(True)
            if not changed:
                raise RuntimeError(
                    f"SolidWorks could not put the flat pattern into the "
                    f"'{target}' state."
                )
            return {"state": target, "action": action,
                    "requested": wanted, "changed": True}

        # No Flat-Pattern feature in the tree at all. A part with no flat
        # pattern is already folded, so "folded" is satisfied without
        # creating one -- creating it would be a change the caller did not
        # ask for.
        if wanted == "folded":
            return {"state": "folded", "action": "no flat-pattern exists",
                    "requested": wanted, "changed": False}
        try:
            doc.FeatureManager.InsertSheetMetalFlatPattern2(True)
            return {"state": "flat", "action": "created flat-pattern",
                    "requested": wanted, "changed": True}
        except Exception as exc:
            raise RuntimeError(
                f"Could not flatten the part: {exc}. Ensure it is a sheet metal "
                f"part (created with create_base_flange or converted to sheet "
                f"metal)."
            ) from exc

    return await _run(_impl)


# ===========================================================================
# Threads / Machining tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_helix(
    diameter: float = 10,
    pitch: float = 1.5,
    revolutions: float = 10,
    start_angle: float = 0,
    clockwise: bool = True,
    unit: Optional[str] = None,
) -> dict:
    """Create a helix / spiral curve.

    A helix is the path that a screw thread follows: a spiral wrapping around
    a cylinder. Also used for compression springs, worm gears, and turbines.

    Requires an active sketch containing exactly one circle whose diameter
    matches (or nearly matches) the intended helix diameter. Call create_sketch
    then draw_circle first, then this tool.

    diameter: helix diameter (for reference — actual diameter comes from the sketch circle).
    pitch: distance between one revolution and the next (e.g. thread pitch).
    revolutions: total number of turns.
    start_angle: rotation offset at the start (degrees).
    clockwise: True for right-hand thread, False for left-hand."""

    def _impl():
        if pitch <= 0:
            raise ValueError(f"Pitch must be positive, got {pitch}.")
        if revolutions <= 0:
            raise ValueError(f"Revolutions must be positive, got {revolutions}.")

        doc = _active_doc()

        # The helix consumes a closed sketch containing one circle. Close it if
        # it is still open, then select it (InsertHelix operates on the selection).
        sketch_name = None
        try:
            if doc.SketchManager.ActiveSketch is not None:
                doc.InsertSketch2(True)  # close
        except Exception:
            pass
        sketch_name = _find_last_sketch(doc)
        if not sketch_name:
            raise RuntimeError(
                "No sketch found. Create a sketch with one circle first "
                "(create_sketch + draw_circle) — the circle defines the helix diameter."
            )
        doc.ClearSelection2(True)
        if not _select_by_id(doc, sketch_name, "SKETCH"):
            raise RuntimeError(f"Could not select sketch '{sketch_name}'.")

        pitch_m = to_meters(pitch, unit)
        start_rad = math.radians(start_angle)
        height_m = pitch_m * revolutions

        # InsertHelix is declared as void in the SolidWorks 2025 type library.
        # Capture the feature names first and locate the generated Helix feature
        # afterwards instead of treating its Python None return value as failure.
        features_before = {
            win32com.client.Dispatch(raw_feature).Name
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
        }

        # InsertHelix(Reversed, Clockwised, Tapered, Outward, Helixdef, Height,
        #   Pitch, Revolution, TaperAngle, Startangle) — 10 args, verified against
        #   the SW 2025 typelib. Helixdef 0 = swHelixDefinedByPitchAndRevolution.
        doc.InsertHelix(
            False,        # Reversed
            clockwise,    # Clockwised
            False,        # Tapered
            False,        # Outward
            0,            # Helixdef: 0 = pitch & revolution
            height_m,     # Height (ignored for pitch&rev, supplied for safety)
            pitch_m,      # Pitch
            revolutions,  # Revolution
            0.0,          # TaperAngle
            start_rad,    # Startangle
        )

        new_helixes = [
            win32com.client.Dispatch(raw_feature).Name
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if (
                win32com.client.Dispatch(raw_feature).GetTypeName2 == "Helix"
                and win32com.client.Dispatch(raw_feature).Name not in features_before
            )
        ]
        if not new_helixes:
            raise RuntimeError(
                "Helix creation failed. Ensure the sketch has exactly one circle "
                "and no other geometry, and that it is a closed profile."
            )
        feat_name = new_helixes[-1]

        return {
            "feature": feat_name,
            "diameter": diameter,
            "pitch": pitch,
            "revolutions": revolutions,
            "clockwise": clockwise,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_thread_feature(
    edge_x: float = 0, edge_y: float = 0, edge_z: float = 0,
    thread_type: str = "metric",
    size: str = "M6x1.0",
    length: float = 20,
    internal: bool = False,
    right_hand: bool = True,
    unit: Optional[str] = None,
) -> dict:
    """Mark where a real 3D thread should go (delegates to a cosmetic thread).

    NOTE: The interactive "Thread" feature that cuts real helical geometry is
    NOT exposed by the SolidWorks API (there is no InsertThread method). The
    only programmatic options are a cosmetic thread (dashed representation, used
    in 99% of manufacturing drawings) or a manual helix + swept-cut.

    This tool therefore creates a COSMETIC thread at the given edge and returns
    a note explaining the limitation. For true cut geometry, model it manually
    with create_helix + a swept cut, or add the Thread feature by hand.

    edge_x/y/z: a point on the circular edge where the thread starts.
    size: thread designation, recorded on the callout (e.g. 'M10x1.5').
    length: thread length (converted to the cosmetic thread depth).
    internal: True for a hole thread, False for a rod thread."""

    def _impl():
        if length <= 0:
            raise ValueError(f"Length must be positive, got {length}.")

        doc = _active_doc()
        doc.ClearSelection2(True)
        ex, ey, ez = to_meters(edge_x, unit), to_meters(edge_y, unit), to_meters(edge_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "EDGE", ex, ey, ez, False, 0, empty, 0):
            raise RuntimeError(f"No circular edge found at ({edge_x}, {edge_y}, {edge_z}).")

        length_m = to_meters(length, unit)
        # Approximate a minor diameter from the size string if it looks like Mxx.
        minor_d = 0.0
        try:
            digits = "".join(c for c in size.split("x")[0] if (c.isdigit() or c == "."))
            if digits:
                minor_d = to_meters(float(digits) * 0.85, unit)  # ~85% of nominal
        except Exception:
            pass

        # InsertCosmeticThread3 is exposed by IFeatureManager in SolidWorks
        # 2025. Use swStandardType_StandardNone (-2), which creates a valid
        # diameter-based cosmetic thread without relying on localized thread
        # library table names; retain the requested designation as its note.
        thread_note = (
            f"{size} ({thread_type}, {'internal' if internal else 'external'}, "
            f"{'RH' if right_hand else 'LH'})"
        )
        feat = None
        try:
            feat = doc.FeatureManager.InsertCosmeticThread3(
                -2, "", "", minor_d, 0, length_m, thread_note
            )
        except Exception as e:
            log.warning("Cosmetic thread creation failed: %s", e)
            feat = None

        if feat is None:
            raise RuntimeError(
                "Cosmetic thread creation failed. Ensure the selected edge is a circular "
                "edge of a cylindrical face. Real modeled Thread features are not exposed "
                "by the SolidWorks API; use a helix and swept cut for true cut geometry."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Thread"

        return {
            "feature": feat_name,
            "note": "Created as a cosmetic thread; real cut geometry requires a manual helix and swept cut.",
            "size": size,
            "length": length,
            "internal": internal,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_cosmetic_thread(
    edge_x: float = 0, edge_y: float = 0, edge_z: float = 0,
    minor_diameter: float = 5.0,
    length: Optional[float] = None,
    standard: str = "ISO",
    unit: Optional[str] = None,
) -> dict:
    """Add a cosmetic thread (visual/annotation only, no real geometry cut).

    Cosmetic threads appear as dashed circles in drawings and are used to mark
    where threads exist without paying the modelling cost of real thread
    geometry. This is the industry-standard way to represent threads in most
    manufacturing drawings.

    edge_x/y/z: a point on the circular edge where the thread starts.
    minor_diameter: thread minor diameter (root diameter — smaller for external
                    threads, larger for internal).
    length: thread length (None = through, i.e. entire length of the cylinder).
    standard: 'ISO', 'ANSI', 'BSI', 'DIN', 'JIS'."""

    def _impl():
        if minor_diameter <= 0:
            raise ValueError(f"Minor diameter must be positive, got {minor_diameter}.")

        doc = _active_doc()
        doc.ClearSelection2(True)
        ex, ey, ez = to_meters(edge_x, unit), to_meters(edge_y, unit), to_meters(edge_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "EDGE", ex, ey, ez, False, 0, empty, 0):
            raise RuntimeError(f"No circular edge found at ({edge_x}, {edge_y}, {edge_z}).")

        if length is not None and length <= 0:
            raise ValueError(f"Length must be positive when provided, got {length}.")

        md_m = to_meters(minor_diameter, unit)
        end_cond = 2 if length is None else 0  # swEndConditionThrough=2, Blind=0
        length_m = to_meters(length, unit) if length is not None else 0.0

        # InsertCosmeticThread3(Standard, StandardType, Size, Diameter, EndType,
        # Depth, Note) belongs to IFeatureManager in SolidWorks 2025. Use
        # swStandardType_StandardNone (-2) so a plain diameter-based cosmetic
        # thread does not depend on a localized library-table entry.
        feat = None
        try:
            feat = doc.FeatureManager.InsertCosmeticThread3(
                -2, "", "",   # StandardNone, StandardType, Size
                md_m,         # Diameter
                end_cond,     # EndType
                length_m,     # Depth
                f"{standard} cosmetic thread",  # Note
            )
        except Exception as e:
            log.warning("InsertCosmeticThread3 failed: %s", e)
            try:
                feat = doc.FeatureManager.InsertCosmeticThread2(
                    0, md_m, length_m, f"{standard} cosmetic thread"
                )
            except Exception as e2:
                log.warning("InsertCosmeticThread2 failed: %s", e2)
                feat = None

        if feat is None:
            raise RuntimeError(
                f"Cosmetic thread creation failed at ({edge_x}, {edge_y}, {edge_z}). "
                "Ensure the selected edge is a circular edge of a cylinder or hole."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "CosmeticThread"

        return {
            "feature": feat_name,
            "minor_diameter": minor_diameter,
            "length": length or "through",
            "standard": standard,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_knurl(
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    pattern: str = "diamond",
    pitch: float = 0.5,
    depth: float = 0.3,
    angle: float = 30,
    unit: Optional[str] = None,
) -> dict:
    """Create a knurled pattern on a cylindrical face.

    Knurling is the diamond or straight-cross-hatch texture applied to the
    grip surface of thumb screws, knobs, and hand-tightened parts. SolidWorks
    doesn't have a native knurl feature, so this is simulated with a
    wrap-and-cut of a cross-hatched sketch onto the cylinder.

    face_x/y/z: a point on the cylindrical face to knurl.
    pattern: 'diamond' (crossed lines) or 'straight' (axial grooves only).
    pitch: distance between neighboring grooves.
    depth: groove depth (typically 0.2–0.5 mm).
    angle: helix angle for diamond pattern (30 = standard)."""

    def _impl():
        if pitch <= 0:
            raise ValueError(f"Pitch must be positive, got {pitch}.")
        if depth <= 0:
            raise ValueError(f"Depth must be positive, got {depth}.")
        if pattern.lower() not in ("diamond", "straight"):
            raise ValueError(f"pattern must be 'diamond' or 'straight', got '{pattern}'.")

        if not 5 <= angle <= 85:
            raise ValueError(f"Angle must be between 5 and 85 degrees, got {angle}.")

        doc = _active_doc()
        doc.ClearSelection2(True)
        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
            raise RuntimeError(f"No cylindrical face found at ({face_x}, {face_y}, {face_z}).")

        p_m = to_meters(pitch, unit)
        d_m = to_meters(depth, unit)
        angle_rad = math.radians(angle)

        # Wrap requires a *closed* 2D profile (open sketch lines are rejected)
        # and specific selection marks: target face=1, sketch=4.  Create a
        # compact three-cell texture on the front plane, then engrave it onto
        # the selected cylindrical face. This avoids the expensive, fragile
        # 80-plus open-contour approximation previously used here.
        doc.ClearSelection2(True)
        front_plane = _standard_plane_name(doc, "front")
        if not _select_by_id(doc, front_plane, "PLANE"):
            raise RuntimeError("Could not select the front plane for the knurl profile.")

        sketch_open = False
        try:
            doc.InsertSketch2(True)
            sketch_open = True

            center_u = 2.5 * p_m
            center_v = 2.5 * p_m
            half_width = max(p_m * 0.22, to_meters(0.1, "mm"))
            half_height = half_width / math.tan(angle_rad)

            for index in (-1, 0, 1):
                cell_u = center_u + index * p_m
                if pattern.lower() == "diamond":
                    points = [
                        (cell_u, center_v + half_height),
                        (cell_u + half_width, center_v),
                        (cell_u, center_v - half_height),
                        (cell_u - half_width, center_v),
                    ]
                else:
                    # A straight knurl uses three narrow, closed axial-groove
                    # profiles. Closed profiles are required for Engrave.
                    points = [
                        (cell_u - half_width * 0.45, center_v + half_height),
                        (cell_u + half_width * 0.45, center_v + half_height),
                        (cell_u + half_width * 0.45, center_v - half_height),
                        (cell_u - half_width * 0.45, center_v - half_height),
                    ]
                for start, end in zip(points, points[1:] + points[:1]):
                    if doc.SketchManager.CreateLine(start[0], start[1], 0, end[0], end[1], 0) is None:
                        raise RuntimeError("SolidWorks could not create a closed knurl profile cell.")

            doc.InsertSketch2(True)
            sketch_open = False
        except Exception:
            if sketch_open:
                try:
                    doc.InsertSketch2(True)
                except Exception:
                    pass
            raise

        sketch_name = _find_last_sketch(doc)
        if sketch_name is None:
            raise RuntimeError("SolidWorks did not create the knurl profile sketch.")

        doc.ClearSelection2(True)
        if not doc.Extension.SelectByID2(sketch_name, "SKETCH", 0, 0, 0, False, 4, empty, 0):
            raise RuntimeError(f"Could not select knurl sketch '{sketch_name}'.")
        if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, True, 1, empty, 0):
            raise RuntimeError("Could not re-select the cylindrical face for the knurl.")

        feat = doc.FeatureManager.InsertWrapFeature2(
            1,          # swWrapSketchType_Engrave
            d_m,
            False,
            0,          # swWrapMethods_Analytical
            1,          # lowest valid mesh factor
        )
        doc.ClearSelection2(True)
        if feat is None:
            raise RuntimeError(
                "Knurl engraving failed. Ensure the selected face is a simple cylindrical face "
                "reachable from the front-plane profile."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Wrap",
            "pattern": pattern.lower(),
            "pitch": pitch,
            "depth": depth,
            "angle": angle,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_rib(
    thickness: float = 3,
    direction: str = "parallel",
    flip: bool = False,
    unit: Optional[str] = None,
) -> dict:
    """Add a rib (thin reinforcing wall) using the last sketch as its centerline.

    A rib is a thin wall of material that reinforces a joint between two
    surfaces — very common in cast, injection-molded, and welded parts.
    Draw a simple sketch (usually one or a few lines) that represents the
    rib's centerline on a plane between the surfaces to be reinforced.

    thickness: rib wall thickness.
    direction: 'parallel' or 'normal', relative to the sketch plane.
    flip: reverse the material-growth direction if the rib grows the wrong way."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        direction_normalized = direction.lower()
        if direction_normalized not in {"parallel", "normal"}:
            raise ValueError("direction must be either 'parallel' or 'normal'.")

        doc = _active_doc()
        t_m = to_meters(thickness, unit)
        sketch_name = _select_last_sketch(doc)

        # IFeatureManager::InsertRib is a void method in SolidWorks 2025.
        # Its first two parameters describe two-sided thickness and thickness
        # reversal (not an "edge type"), so the legacy seven-argument call
        # silently failed.  Capture the feature tree, invoke its documented
        # ten-argument signature, then identify the new Rib feature.
        before_names = set()
        for raw_feature in doc.FeatureManager.GetFeatures(False):
            feature = win32com.client.Dispatch(raw_feature)
            before_names.add(feature.Name)

        doc.FeatureManager.InsertRib(
            True,                              # Is2Sided: centered wall thickness
            False,                             # ReverseThicknessDir: N/A for two-sided
            t_m,                               # Thickness (meters)
            0,                                 # ReferenceEdgeIndex
            flip,                              # ReverseMaterialDir
            False,                             # IsDrafted
            False,                             # DraftOutward
            0.0,                               # DraftAngle
            direction_normalized == "normal",  # IsNormToSketch
            False,                             # IsDraftedFromWall
        )
        doc.ForceRebuild3(False)

        feat = None
        for raw_feature in doc.FeatureManager.GetFeatures(False):
            feature = win32com.client.Dispatch(raw_feature)
            if feature.Name in before_names:
                continue
            if feature.GetTypeName2 == "Rib":
                feat = feature
                break

        doc.ClearSelection2(True)
        if feat is None:
            raise RuntimeError(
                f"Rib failed on sketch '{sketch_name}'. "
                "The sketch should sit on a plane between the two surfaces to reinforce, "
                "and its lines must touch or extend to those surfaces when extruded."
            )

        return {
            "feature": feat.Name,
            "sketch": sketch_name,
            "thickness": thickness,
            "direction": direction_normalized,
            "flip": flip,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


# ===========================================================================
# 3D Reference / Body operations
# ===========================================================================

def _select_plane_by_name(doc, plane_name: str, append: bool = False, mark: int = 0) -> bool:
    """Select a plane by feature-tree name. Tries the standard front/top/right
    keywords first (positional), otherwise looks it up as a named feature."""
    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

    lower = plane_name.lower()
    if lower in ("front", "top", "right"):
        resolved = _standard_plane_name(doc, lower)
        return bool(doc.Extension.SelectByID2(resolved, "PLANE", 0, 0, 0, append, mark, empty, 0))

    return bool(doc.Extension.SelectByID2(plane_name, "PLANE", 0, 0, 0, append, mark, empty, 0))


def _ensure_pattern_axis(doc, direction: str) -> str:
    """Return a reusable reference axis aligned to a model standard direction.

    Origin-axis display names are localized and are not normal FeatureManager
    entries on every SolidWorks installation. A named reference axis made from
    two standard planes provides the same global direction and remains stable
    for feature patterns in all supported UI languages.
    """
    normalized = direction.lower()
    axis_planes = {
        "x": ("front", "top"),
        "y": ("front", "right"),
        "z": ("top", "right"),
    }
    plane_pair = axis_planes.get(normalized)
    if plane_pair is None:
        raise ValueError(f"Direction must be 'x', 'y', or 'z', got '{direction}'.")

    axis_name = f"MCP Pattern Axis {normalized.upper()}"
    axis_features = []
    for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
        feature = win32com.client.Dispatch(raw_feature)
        if feature.GetTypeName2 == "RefAxis":
            axis_features.append(feature)
            if feature.Name == axis_name:
                return axis_name

    doc.ClearSelection2(True)
    if not _select_plane_by_name(doc, plane_pair[0], append=False, mark=0):
        raise RuntimeError(f"Could not select standard plane '{plane_pair[0]}'.")
    if not _select_plane_by_name(doc, plane_pair[1], append=True, mark=0):
        raise RuntimeError(f"Could not select standard plane '{plane_pair[1]}'.")

    axis_count_before = len(axis_features)
    try:
        # InsertAxis2 is an IModelDoc2 method and returns void over COM.
        doc.InsertAxis2(True)
    except Exception as exc:
        raise RuntimeError(
            f"Could not create the {normalized}-direction reference axis."
        ) from exc

    axes_after = [
        win32com.client.Dispatch(raw_feature)
        for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
        if win32com.client.Dispatch(raw_feature).GetTypeName2 == "RefAxis"
    ]
    if len(axes_after) <= axis_count_before:
        raise RuntimeError(f"SolidWorks did not create the {normalized}-direction reference axis.")

    created_axis = axes_after[-1]
    try:
        created_axis.Name = axis_name
    except Exception:
        # The generated name is still usable if a protected document prevents
        # renaming, although subsequent calls will then create a new axis.
        axis_name = created_axis.Name
    return axis_name


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def mirror_feature(
    feature_name: str,
    plane: str = "front",
) -> dict:
    """Mirror an existing feature (hole, cut, boss, etc.) across a plane.

    Creates a symmetric copy of the feature on the other side of the mirror
    plane. Essential for symmetric parts — you only model half, then mirror.

    feature_name: name of the feature to mirror (from list_features).
    plane: mirror plane — 'front', 'top', 'right', or the name of a
           user-created reference plane."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)

        if not _select_plane_by_name(doc, plane, append=False, mark=2):
            raise RuntimeError(f"Could not select mirror plane '{plane}'.")

        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(feature_name, "BODYFEATURE", 0, 0, 0, True, 1, empty, 0):
            raise RuntimeError(f"Feature '{feature_name}' not found.")

        feat = doc.FeatureManager.InsertMirrorFeature2(
            False,  # BScopeOptions (partial preview)
            False,  # BGeometryPattern
            False,  # BMerge
            False,  # BKnit
            0,      # ScopeOptions: 0=AllBodies
        )

        if feat is None:
            raise RuntimeError(
                f"Mirror failed for feature '{feature_name}' across plane '{plane}'. "
                "Check that the feature exists and the plane intersects the body sensibly."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Mirror",
            "mirrored": feature_name,
            "plane": plane,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def mirror_body(
    body_name: str,
    plane: str = "front",
    merge: bool = False,
) -> dict:
    """Mirror an entire solid body across a plane.

    Copies the whole body (not just a single feature) to the other side of the
    plane. Useful for building symmetric multi-body parts and weldments.

    body_name: name of the solid body (from list_features, look under SolidBodies).
    plane: mirror plane name ('front', 'top', 'right', or a named plane).
    merge: True to merge the mirrored body with the original; False to keep
           them as separate bodies."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)

        if not _select_plane_by_name(doc, plane, append=False, mark=2):
            raise RuntimeError(f"Could not select mirror plane '{plane}'.")

        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(body_name, "SOLIDBODY", 0, 0, 0, True, 256, empty, 0):
            raise RuntimeError(f"Body '{body_name}' not found. Use list_features to see body names.")

        feat = doc.FeatureManager.InsertMirrorFeature2(
            True,   # BMirrorBody: mirror selected solid bodies, not body features
            False,  # BGeometryPattern
            merge,  # BMerge
            False,  # BKnit
            1,      # ScopeOptions: 1=SelectedBodies
        )

        if feat is None:
            raise RuntimeError(
                f"Body mirror failed. body='{body_name}', plane='{plane}'. "
                "Ensure both the body and the plane exist."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Mirror",
            "body": body_name,
            "plane": plane,
            "merged": merge,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def move_copy_body(
    body_name: str,
    dx: float = 0, dy: float = 0, dz: float = 0,
    rx: float = 0, ry: float = 0, rz: float = 0,
    copy: bool = False,
    num_copies: int = 1,
    unit: Optional[str] = None,
) -> dict:
    """Move or copy a solid body by translation and/or rotation.

    Translates the body by (dx, dy, dz) and rotates it by (rx, ry, rz) degrees
    about its origin. A rotation references the matching global X, Y, or Z
    axis in SolidWorks, rather than depending on the selected body's local
    orientation. If copy=True, keeps the original and creates copies; if False,
    just moves the original.

    body_name: name of the solid body to move/copy.
    dx/dy/dz: translation along each axis.
    rx/ry/rz: rotation angles in degrees around X, Y, Z axes.
    copy: True to create copies; False to move in place.
    num_copies: how many copies to make (only used if copy=True)."""

    def _impl():
        if copy and num_copies < 1:
            raise ValueError(f"num_copies must be >= 1, got {num_copies}.")

        rotations = {"x": rx, "y": ry, "z": rz}
        requested_axes = [axis for axis, angle in rotations.items() if abs(angle) > 1e-9]
        if len(requested_axes) > 1:
            raise ValueError(
                "Move/copy body supports one rotation axis per call. "
                "Apply separate calls for X, Y, and Z rotations."
            )

        doc = _active_doc()
        rotation_axis = requested_axes[0] if requested_axes else None
        doc.ClearSelection2(True)

        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(body_name, "SOLIDBODY", 0, 0, 0, False, 1, empty, 0):
            raise RuntimeError(f"Body '{body_name}' not found.")

        dx_m = to_meters(dx, unit)
        dy_m = to_meters(dy, unit)
        dz_m = to_meters(dz, unit)
        # The public tool accepts degrees; InsertMoveCopyBody2 consumes its
        # numeric global-axis rotation values in radians.

        n = num_copies if copy else 1

        # InsertMoveCopyBody2(TransX, TransY, TransZ, TransDist, RotPointX,
        #   RotPointY, RotPointZ, RotAngleX, RotAngleY, RotAngleZ, BCopy,
        #   NumCopies) — 12 args.
        feat = doc.FeatureManager.InsertMoveCopyBody2(
            dx_m, dy_m, dz_m,
            0.0,             # TransDist
            0.0, 0.0, 0.0,   # rotation origin
            math.radians(rx), math.radians(ry), math.radians(rz),
            copy,
            n,
        )

        if feat is None:
            raise RuntimeError(f"Move/Copy failed on body '{body_name}'.")

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else ("BodyCopy" if copy else "BodyMove"),
            "body": body_name,
            "translation": [dx, dy, dz],
            "rotation_deg": [rx, ry, rz],
            "rotation_axis": rotation_axis,
            "copied": copy,
            "copies": n if copy else 0,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def combine_bodies(
    operation: str = "add",
    main_body: Optional[str] = None,
    tool_bodies: Optional[list] = None,
) -> dict:
    """Combine two or more solid bodies with a boolean operation.

    operation: 'add' (union), 'subtract' (main minus tools), or 'common' (intersection).
    main_body: target body name, required only for 'subtract'. For 'add' and
               'common', an optional value is included in the bodies to combine.
    tool_bodies: list of body names to combine. If omitted for 'add'/'common',
                 combines ALL bodies in the part."""

    def _impl():
        # swBodyOperationType: SWBODYADD=15903, SWBODYCUT(subtract)=15902,
        # SWBODYINTERSECT(common)=15901.
        op_map = {"add": 15903, "subtract": 15902, "common": 15901}
        is_subtract = operation.lower() == "subtract"
        op = op_map.get(operation.lower())
        if op is None:
            raise ValueError(f"operation must be 'add', 'subtract', or 'common', got '{operation}'.")

        doc = _active_doc()
        doc.ClearSelection2(True)

        if is_subtract and not main_body:
            raise ValueError("'subtract' requires main_body to be specified.")

        def _find_body(name):
            bodies = doc.GetBodies2(0, True)
            for raw in (bodies or []):
                b = win32com.client.Dispatch(raw)
                bn = b.Name if not callable(getattr(b, "Name", None)) else b.Name()
                if bn == name:
                    return b
            return None

        all_bodies = doc.GetBodies2(0, True)
        if not all_bodies:
            raise RuntimeError("No solid bodies found in the active document.")
        all_disp = [win32com.client.Dispatch(b) for b in all_bodies]

        if is_subtract:
            main_obj = _find_body(main_body)
            if main_obj is None:
                raise RuntimeError(f"Main body '{main_body}' not found.")
            if tool_bodies:
                tools = []
                for name in tool_bodies:
                    body = _find_body(name)
                    if body is None:
                        raise RuntimeError(f"Tool body '{name}' not found.")
                    tools.append(body)
            else:
                main_name = main_obj.Name if not callable(getattr(main_obj, "Name", None)) else main_obj.Name()
                tools = [body for body in all_disp
                         if (body.Name if not callable(getattr(body, "Name", None)) else body.Name()) != main_name]
        else:
            # The SolidWorks API requires MainBody = Nothing for add and
            # intersect operations.  It receives every participating body in
            # ToolVar; passing the first body as MainBody makes the API return
            # no feature even when the bodies touch or overlap.
            main_obj = None
            requested_names = ([] if main_body is None else [main_body]) + list(tool_bodies or [])
            if requested_names:
                tools = []
                for name in requested_names:
                    body = _find_body(name)
                    if body is None:
                        raise RuntimeError(f"Body '{name}' not found.")
                    if all(body.Name != existing.Name for existing in tools):
                        tools.append(body)
            else:
                tools = all_disp

        if len(tools) < (1 if is_subtract else 2):
            raise RuntimeError("Combine requires at least 2 bodies (1 main + 1 tool).")

        tool_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
            tuple(tools),
        )

        # InsertCombineFeature(OperationType, MainBody, ToolVar).  For add and
        # common, MainBody is an explicit null IDispatch (see API documentation).
        # Passing ``None`` or raw ``_oleobj_`` instances causes a type mismatch
        # in the dynamic pywin32 binding used by the active FeatureManager.
        main_body_arg = main_obj if main_obj is not None else win32com.client.VARIANT(
            pythoncom.VT_DISPATCH, None
        )
        feat = doc.FeatureManager.InsertCombineFeature(op, main_body_arg, tool_variant)

        if feat is None:
            raise RuntimeError(
                f"Combine ({operation}) failed. Ensure the bodies actually "
                "intersect (for 'common'/'subtract') or touch (for 'add')."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Combine",
            "operation": operation,
            "main_body": main_body,
            "tool_bodies": tool_bodies or "all",
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_reference_plane(
    reference: str = "front",
    offset: float = 50,
    angle: float = 0,
    angle_about: Optional[str] = None,
    flip: bool = False,
    unit: Optional[str] = None,
) -> dict:
    """Create a new reference plane offset (or rotated) from an existing plane.

    Reference planes let you sketch on custom locations — not just front/top/right.
    This is essential for creating angled sketches, mid-planes for mirrors, and
    construction geometry.

    reference: the existing plane to reference — 'front', 'top', 'right', or a
               user-created plane's name.
    offset: distance from the reference plane along its normal.
    angle: rotation angle in degrees. 0 gives a parallel offset plane. A
           nonzero angle needs something to rotate ABOUT, so pass angle_about
           with the name of a reference axis or edge; without one SolidWorks
           has no axis and the call is refused rather than quietly producing
           a parallel plane.
    angle_about: name of the axis or edge to rotate about. Used only when
           angle is nonzero.
    flip: reverse the offset direction.

    Returns the plane's name and, where SolidWorks reports it, the origin and
    normal the plane actually ended up with, so a wrong plane stops being
    silent."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)

        if not _select_plane_by_name(doc, reference, append=False, mark=0):
            raise RuntimeError(f"Could not select reference plane '{reference}'.")

        offset_m = to_meters(offset, unit)
        angle_rad = math.radians(angle)

        if flip:
            offset_m = -offset_m

        # swRefPlaneReferenceConstraints_e: 8 = Distance, 16 = Angle.
        # The constraint is decided HERE, before the COM call. The previous
        # version always tried Distance first and only fell back to Angle if
        # that returned None -- and Distance nearly always succeeds, so
        # angle=30 silently produced a plane PARALLEL to the reference while
        # the return value still reported "angle": 30.
        if angle == 0:
            constraint, value = 8, offset_m
        else:
            constraint, value = 16, angle_rad
            if angle_about:
                if not _select_plane_by_name(doc, angle_about, append=True, mark=0):
                    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
                    for sel_type in ("AXIS", "EDGE"):
                        if doc.Extension.SelectByID2(angle_about, sel_type, 0, 0, 0,
                                                     True, 0, empty, 0):
                            break
                    else:
                        doc.ClearSelection2(True)
                        raise RuntimeError(
                            f"Could not select '{angle_about}' as the axis to "
                            f"rotate about. Create it first with "
                            f"create_reference_axis."
                        )

        try:
            feat = doc.FeatureManager.InsertRefPlane(
                constraint, value,
                0, 0.0,      # Constraint2 unused
                0, 0.0,      # Constraint3 unused
            )
        except Exception as exc:
            doc.ClearSelection2(True)
            raise RuntimeError(
                f"InsertRefPlane failed for reference='{reference}', "
                f"offset={offset}, angle={angle}: {exc}"
            ) from exc

        if feat is None:
            doc.ClearSelection2(True)
            hint = ""
            if angle != 0 and not angle_about:
                hint = (" An angled plane needs an axis to rotate about: pass "
                        "angle_about with a reference axis or edge name.")
            raise RuntimeError(
                f"Reference plane creation failed. reference='{reference}', "
                f"offset={offset}, angle={angle}.{hint}"
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Plane"
        doc.ClearSelection2(True)

        factor = 1.0 / UNIT_TO_METERS[unit or _default_unit]
        result = {
            "plane": name,
            "reference": reference,
            "offset": offset,
            "offset_applied": round(offset_m * factor, 6),
            "angle": angle,
            "angle_about": angle_about,
            "constraint": "angle" if constraint == 16 else "distance",
            "flip": flip,
            "unit": unit or _default_unit,
        }
        # Read the plane back. flip=True with reference='right' has been seen
        # to land the plane at X=0 instead of the requested offset; reporting
        # the real origin is what turns that from a silent wrong answer into
        # something the caller can check.
        try:
            ref_plane = win32com.client.Dispatch(
                _com_member(feat_dispatch, "GetSpecificFeature2"))
            transform = win32com.client.Dispatch(_com_member(ref_plane, "Transform"))
            data = tuple(_com_member(transform, "ArrayData"))
            result["origin"] = [round(c * factor, 4) for c in data[9:12]]
            result["normal"] = [round(c, 6) for c in data[6:9]]
        except Exception:
            log.debug("Could not read back the position of plane %r", name,
                      exc_info=True)
            result["position_read"] = "unavailable on this SolidWorks build"
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_reference_axis(
    method: str = "two_planes",
    ref1: str = "front",
    ref2: str = "top",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Create a reference axis.

    Reference axes are used as rotation centers for revolves and circular
    patterns, and as alignment references in assemblies.

    method:
      'two_planes'   — intersection of two planes (uses ref1, ref2 plane names).
      'cylinder'     — axis of a cylindrical face (uses face_x/y/z point on face).
      'point_edge'   — not implemented here.

    ref1/ref2: plane names when method='two_planes'.
    face_x/y/z: point on cylindrical face when method='cylinder'."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        m = method.lower()
        if m == "two_planes":
            if not _select_plane_by_name(doc, ref1, append=False, mark=0):
                raise RuntimeError(f"Could not select first plane '{ref1}'.")
            if not _select_plane_by_name(doc, ref2, append=True, mark=0):
                raise RuntimeError(f"Could not select second plane '{ref2}'.")
        elif m == "cylinder":
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No cylindrical face found at ({face_x}, {face_y}, {face_z}).")
        else:
            raise ValueError(f"method must be 'two_planes' or 'cylinder', got '{method}'.")

        axis_count_before = sum(
            1
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "RefAxis"
        )
        # InsertAxis2 belongs to IModelDoc2 and infers the axis type from the
        # current selection (two planes, one cylindrical face, etc.).
        try:
            doc.InsertAxis2(True)
        except Exception:
            pass

        axes_after = [
            win32com.client.Dispatch(raw_feature)
            for raw_feature in doc.FeatureManager.GetFeatures(False) or ()
            if win32com.client.Dispatch(raw_feature).GetTypeName2 == "RefAxis"
        ]
        if len(axes_after) <= axis_count_before:
            raise RuntimeError(
                f"Reference axis creation failed. method='{method}'. "
                "For 'two_planes' the planes must actually intersect; for 'cylinder' "
                "the point must lie on a cylindrical/conical face."
            )
        name = axes_after[-1].Name

        return {
            "axis": name,
            "method": method,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def split_body(
    tool_type: str = "plane",
    plane: str = "front",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    consume_original: bool = False,
    unit: Optional[str] = None,
) -> dict:
    """Split a solid body into multiple pieces using a cutting tool.

    Useful for creating multi-body parts, cut lists for weldments, or separating
    a large part into sub-parts for manufacture.

    tool_type: 'plane' (use a plane as the cutter — reference plane by name)
               or 'face' (use a planar face at face_x/y/z).
    plane: plane name for tool_type='plane'.
    face_x/y/z: face location for tool_type='face'.
    consume_original: if True, deletes the source body; if False, keeps it."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        tt = tool_type.lower()
        if tt == "plane":
            if not _select_plane_by_name(doc, plane, append=False, mark=0):
                raise RuntimeError(f"Could not select cutting plane '{plane}'.")
        elif tt == "face":
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")
        else:
            raise ValueError(f"tool_type must be 'plane' or 'face', got '{tool_type}'.")

        # The split workflow is two-stage: PreSplitBody2 resolves the regions
        # produced by the selected cutter, and PostSplitBody2 creates the
        # feature from those regions.  InsertSplitLineFeature only creates a
        # cosmetic face split and cannot divide solid bodies.
        candidate_bodies = doc.FeatureManager.PreSplitBody2
        if callable(candidate_bodies):
            candidate_bodies = candidate_bodies()
        candidate_bodies = tuple(candidate_bodies or ())
        if len(candidate_bodies) < 2:
            raise RuntimeError(
                f"Split failed. tool_type='{tool_type}'. Ensure the cutter fully "
                "intersects the body and produces at least two solid regions."
            )

        bodies_to_mark = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, candidate_bodies
        )
        origins = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
            tuple(None for _ in candidate_bodies),
        )
        save_paths = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_BSTR,
            tuple("" for _ in candidate_bodies),
        )
        doc.ClearSelection2(True)
        feat = doc.FeatureManager.PostSplitBody2(
            bodies_to_mark,
            consume_original,
            origins,
            save_paths,
            "",
        )
        if feat is None:
            raise RuntimeError("SolidWorks did not create the split-body feature.")

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Split",
            "tool_type": tool_type,
            "consume_original": consume_original,
            "bodies_created": len(candidate_bodies),
        }

    return await _run(_impl)


# ===========================================================================
# Detailed Drawing tools
# ===========================================================================

def _active_drawing():
    doc = _active_doc()
    if _doc_type(doc) != 3:
        raise RuntimeError("The active document is not a drawing. Create one with create_new_drawing.")
    return doc


def _get_selected_view(doc):
    """Return the currently active/selected drawing view, or the first view.

    Kept for the tools that genuinely act on "whatever the user selected".
    Anything that places geometry or a dimension must NOT use this: on a sheet
    with four views the active one is rarely the one the coordinates fall in.
    Those callers go through ``_find_view_for_point`` instead.
    """
    try:
        view = _com_member(doc, "ActiveDrawingView")
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        log.debug("ActiveDrawingView unavailable", exc_info=True)
    try:
        views = _com_member(doc, "GetFirstView")  # sheet is first "view"
        view = _com_member(win32com.client.Dispatch(views), "GetNextView")
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        log.debug("GetFirstView/GetNextView unavailable", exc_info=True)
    return None


_DRAWING_EDGE_IID = "{83A33D42-27C5-11CE-BFD4-00400513BB57}"

# swViewEntityType_e: edges.
_VIEW_ENTITY_EDGE = 1

# No angular-dimension constant is hard-coded here on purpose: the unit of a
# dimension is settled in _read_dimension_value by comparing SystemValue (SI)
# against Value (display units), which needs no enum and so cannot be wrong
# about a build whose enum differs. A guessed constant would silently report
# radians as millimetres.

# swThisConfiguration, for IDimension.GetSystemValue3.
_THIS_CONFIGURATION = 1

# swUserPreferenceToggle_e.swInputDimValOnCreate. Left on, SolidWorks opens a
# modal value box when a dimension is created and the COM call never returns
# -- the same trap add_sketch_dimension already works around.
_INPUT_DIM_VAL_ON_CREATE = 10

# Stable short IDs for drawing entities, shared by the read and write tools.
_ENTITY_REGISTRY = ad.EntityRegistry()


# ---------------------------------------------------------------------------
# Drawing reads: sheets, views, entities, dimensions
# ---------------------------------------------------------------------------


def _sheet_names(doc) -> list:
    names = _com_member(doc, "GetSheetNames") or ()
    return [str(n) for n in names]


def _sheet_and_views(doc, sheet_name: str):
    """(ISheet, [IView]) for one sheet, excluding any sheet pseudo-view.

    ISheet.GetViews returns the REAL views only -- confirmed live on
    SolidWorks 2025 PT-BR: a sheet with one view returns one element, a sheet
    with two returns two. An earlier version of this function assumed element
    0 was the sheet's own pseudo-view (which is how IDrawingDoc.GetFirstView
    behaves) and dropped it, so get_drawing_layout reported zero views on a
    sheet that had one, and _find_view could not find anything.

    The sheet pseudo-view is filtered by NAME rather than by position, so a
    build that does include it is still handled without dropping a real view.
    """
    sheet = win32com.client.Dispatch(doc.Sheet(sheet_name))
    raw = _com_member(sheet, "GetViews") or ()
    views = []
    for item in raw:
        view = win32com.client.Dispatch(item)
        try:
            if str(_com_member(view, "Name")) == sheet_name:
                continue  # the sheet's own pseudo-view, not a model view
        except Exception:
            log.debug("Could not read a view name on sheet %r", sheet_name,
                      exc_info=True)
        views.append(view)
    return sheet, views


def _all_drawing_views(doc) -> list:
    """Every real view of every sheet, as (sheet_name, view) pairs."""
    out = []
    for name in _sheet_names(doc):
        try:
            _sheet, views = _sheet_and_views(doc, name)
        except Exception:
            log.warning("Could not read views of sheet %r", name, exc_info=True)
            continue
        out.extend((name, v) for v in views)
    return out


def _view_name(view) -> str:
    return str(_com_member(view, "Name"))


def _find_view(doc, view_name: str):
    """The view with this name, on any sheet.

    Raises with the available names, because a wrong view name is the error
    the model is most likely to make and the least able to recover from
    without being told what exists.
    """
    available = []
    for _sheet_name, view in _all_drawing_views(doc):
        name = _view_name(view)
        available.append(name)
        if name == view_name:
            return view
    raise RuntimeError(
        f"No drawing view named '{view_name}'. Available: "
        f"{', '.join(available) if available else '(none)'}. "
        f"Call get_drawing_layout to list them."
    )


def _view_outline_m(view) -> list:
    return ad.normalize_box(tuple(_com_member(view, "GetOutline")))


def _view_xform(view) -> tuple:
    xform = tuple(_com_member(view, "GetViewXform"))
    if len(xform) != 13:
        raise RuntimeError(
            f"SolidWorks returned a {len(xform)}-element view transform for "
            f"'{_view_name(view)}'; 13 were expected."
        )
    return xform


def _view_scale_ratio(view) -> tuple:
    try:
        ratio = tuple(view.ScaleRatio)
        if len(ratio) >= 2 and ratio[1]:
            return (float(ratio[0]), float(ratio[1]))
    except Exception:
        log.debug("ScaleRatio unreadable on %r", _view_name(view), exc_info=True)
    return (1.0, 1.0)


def _view_position_m(view) -> list:
    try:
        pos = tuple(view.Position)
        return [float(pos[0]), float(pos[1])]
    except Exception:
        return ad.box_center(_view_outline_m(view))


def _sheet_properties(sheet) -> dict:
    """Paper size, scale and projection angle of a sheet.

    ISheet.GetProperties2 returns
    [paperSize, template, scale1, scale2, firstAngle, width, height, ...]
    with width and height in metres.
    """
    props = tuple(_com_member(sheet, "GetProperties2") or ())
    if len(props) < 7:
        raise RuntimeError(
            f"ISheet.GetProperties2 returned {len(props)} values; at least 7 were expected."
        )
    return {
        "paper_size_code": int(props[0]),
        "sheet_scale": (float(props[2]), float(props[3])),
        "first_angle": bool(props[4]),
        "width_m": float(props[5]),
        "height_m": float(props[6]),
    }


def _view_edge_owners(view):
    """[(component_name, component_variant)] whose edges to enumerate.

    IView.GetVisibleEntities2 takes the component as its first argument. The
    old code always passed a null variant, which works for a part view but
    enumerates nothing for an assembly view -- exactly where balloons and
    weld symbols are used. For an assembly, iterate the drawing components.
    """
    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    try:
        raw = _com_member(view, "GetVisibleDrawingComponents") or ()
    except Exception:
        raw = ()
    owners = []
    for item in raw:
        try:
            drawing_component = win32com.client.Dispatch(item)
            component = _com_member(drawing_component, "Component")
            if component is None:
                continue
            component = win32com.client.Dispatch(component)
            owners.append((str(_com_member(component, "Name2")), component))
        except Exception:
            log.debug("Skipping an unreadable drawing component", exc_info=True)
    return owners or [(None, empty)]


def _read_edge_geometry(view, raw_entity, xform):
    """Classify one visible edge, reading only what COM can actually give.

    A circle's radius lives on the underlying ICurve, not on the edge, so it
    needs a second hop that the old code never made.
    """
    edge = win32com.client.Dispatch(raw_entity, "IEdge", _DRAWING_EDGE_IID)
    params = tuple(_com_member(edge, "GetCurveParams"))
    is_circle, circle_params = False, None
    try:
        curve = win32com.client.Dispatch(_com_member(edge, "GetCurve"))
        is_circle = bool(_com_member(curve, "IsCircle"))
        if is_circle:
            circle_params = tuple(_com_member(curve, "CircleParams"))
    except Exception:
        log.debug("Curve type unreadable for an edge; treating it as a line",
                  exc_info=True)
        is_circle, circle_params = False, None
    return ad.classify_edge(xform, params, circle_params=circle_params,
                            is_circle=is_circle)


def _read_view_entities(doc, view, view_name: str, register: bool = True) -> list:
    """Every visible edge of a view as an ad.EdgeInfo with a stable ID."""
    xform = _view_xform(view)
    doc_title = _doc_title(doc)
    entities, index = [], 0
    for component_name, component in _view_edge_owners(view):
        try:
            raw_entities = _com_member(view, "GetVisibleEntities2",
                                       component, _VIEW_ENTITY_EDGE) or ()
        except Exception:
            log.warning("GetVisibleEntities2 failed for view %r component %r",
                        view_name, component_name, exc_info=True)
            continue
        for raw_entity in raw_entities:
            try:
                info = _read_edge_geometry(view, raw_entity, xform)
            except Exception:
                log.debug("Skipping an unclassifiable edge in %r", view_name,
                          exc_info=True)
                continue
            index += 1
            info.component = component_name
            if register:
                info.id = _ENTITY_REGISTRY.register(
                    doc_title, view_name, index,
                    {"entity": raw_entity, "component": component,
                     "kind": info.kind},
                )
            else:
                info.id = f"{view_name}#E{index}"
            entities.append(info)
    return entities


def _read_dimension_value(dim):
    """(value, unit) of an IDimension: millimetres or degrees.

    Measured live on SolidWorks 2025 PT-BR for one linear drawing dimension
    of 400 mm:

        GetSystemValue3(1, None) -> None      <- the documented call
        GetSystemValue3(1, "")   -> None
        GetSystemValue2("")      -> 0.4       (metres)
        SystemValue              -> 0.4       (metres)
        Value                    -> 400.0     (document display units)
        GetValue3(1, None)       -> (400.0,)  (display units)

    So the documented GetSystemValue3 cannot be relied on, and the earlier
    version of this reader multiplied its None by 1000.

    The unit is settled WITHOUT a swDimensionType_e constant, by using the
    two readings against each other: SystemValue is always SI (metres for a
    length, radians for an angle) while Value is in display units. If
    Value matches SystemValue x 1000 it is a length; if it matches
    degrees(SystemValue) it is an angle. Guessing an enum value for angular
    dimensions, and being wrong, would silently report radians-as-millimetres.
    """
    system = None
    for getter in ("SystemValue",):
        try:
            system = _com_member(dim, getter)
            if system is not None:
                break
        except Exception:
            log.debug("IDimension.%s unavailable", getter, exc_info=True)
    if system is None:
        for call, args in (("GetSystemValue2", ("",)),
                           ("GetSystemValue3", (_THIS_CONFIGURATION, None))):
            try:
                raw = _com_member(dim, call, *args)
                if isinstance(raw, (tuple, list)):
                    raw = raw[0] if raw else None
                if raw is not None:
                    system = raw
                    break
            except Exception:
                log.debug("IDimension.%s unavailable", call, exc_info=True)
    if system is None:
        raise RuntimeError(
            "SolidWorks reported no value for this dimension "
            "(SystemValue, GetSystemValue2 and GetSystemValue3 all returned "
            "nothing)."
        )

    system = float(system)
    as_mm = round(system * 1000.0, 3)
    as_deg = round(math.degrees(system), 4)

    display = None
    try:
        display = _com_member(dim, "Value")
    except Exception:
        log.debug("IDimension.Value unavailable", exc_info=True)
    if display is None:
        try:
            raw = _com_member(dim, "GetValue3", _THIS_CONFIGURATION, None)
            if isinstance(raw, (tuple, list)) and raw:
                display = raw[0]
        except Exception:
            log.debug("IDimension.GetValue3 unavailable", exc_info=True)

    if display is not None:
        display = float(display)
        if abs(display - as_mm) <= max(0.01, abs(as_mm) * 1e-6):
            return as_mm, "mm"
        if abs(display - as_deg) <= max(0.01, abs(as_deg) * 1e-6):
            return as_deg, "deg"

    # Nothing to cross-check against: a length is overwhelmingly the common
    # case, and saying which assumption was made beats implying certainty.
    return as_mm, "mm?"


def _read_view_dimensions(doc, view, view_name: str) -> list:
    """Every dimension displayed in a view, with its measured value.

    This is the read the old code never had: without it nothing can tell
    whether a dimension landed on the right edge, is duplicated, or has come
    adrift from the model.
    """
    doc_title = _doc_title(doc)
    # (com_identity, entity_id) pairs, compared with ==. NOT a dict: the COM
    # identity is unhashable -- see _entity_key.
    known: list = []
    for entity_id in _ENTITY_REGISTRY.ids_for_view(doc_title, view_name):
        try:
            item = _ENTITY_REGISTRY.resolve(doc_title, entity_id)
            known.append((_entity_key(item["entity"]), entity_id))
        except Exception:
            log.debug("Could not key registered entity %s", entity_id, exc_info=True)

    def _entity_id_of(com_entity):
        if com_entity is None:
            return None
        try:
            key = _entity_key(com_entity)
        except Exception:
            return "unregistered"
        for candidate, entity_id in known:
            try:
                if candidate == key:
                    return entity_id
            except Exception:
                continue
        return "unregistered"

    out, index = [], 0
    try:
        display = _com_member(view, "GetFirstDisplayDimension5")
    except Exception:
        log.warning("GetFirstDisplayDimension5 failed on view %r", view_name,
                    exc_info=True)
        return out
    while display is not None:
        display = win32com.client.Dispatch(display)
        index += 1
        entry = {"id": f"{view_name}#D{index}"}
        try:
            dimension = win32com.client.Dispatch(_com_member(display, "GetDimension2", 0))
            value, unit = _read_dimension_value(dimension)
            try:
                dim_type = int(_com_member(display, "Type2"))
            except Exception:
                dim_type = None
            entry.update({
                "name": str(_com_member(dimension, "FullName")),
                "type_code": dim_type,
                "unit": unit,
                "value_mm": value,
                "driven": bool(_com_member(dimension, "DrivenState") == 1),
            })
        except Exception as exc:
            entry["read_error"] = str(exc)
            log.warning("Could not read dimension %s", entry["id"], exc_info=True)

        # One try per concern: a failure reading the text position used to
        # abandon the attachment read as well, and the attachment read is the
        # one the coverage check depends on.
        annotation = None
        try:
            annotation = win32com.client.Dispatch(_com_member(display, "GetAnnotation"))
        except Exception as exc:
            entry.setdefault("read_error", f"GetAnnotation: {exc}")

        if annotation is not None:
            try:
                position = tuple(_com_member(annotation, "GetPosition") or ())
                entry["text_position_mm"] = [ad.mm(c) for c in position[:2]]
            except Exception as exc:
                entry.setdefault("read_error", f"GetPosition: {exc}")

            try:
                attached = _com_member(annotation, "GetAttachedEntities3") or ()
                types = _com_member(annotation, "GetAttachedEntityTypes") or ()
                entry["attached"] = [_entity_id_of(e) for e in attached]
                entry["attached_count"] = sum(1 for e in attached if e is not None)
                # IAnnotation.GetAttachedEntityTypes: a dissociated annotation
                # reports a null entity and swSelNOTHING for that slot.
                entry["dangling"] = (
                    any(e is None for e in attached)
                    or (len(attached) == 0 and len(types) == 0)
                )
            except Exception as exc:
                entry.setdefault("read_error", f"GetAttachedEntities3: {exc}")

        entry.setdefault("attached", [])
        entry.setdefault("attached_count", 0)
        entry.setdefault("dangling", None)

        out.append(entry)
        try:
            display = _com_member(display, "GetNext5")
        except Exception:
            break
    return out


def _entity_key(com_entity):
    """A COMPARABLE -- not hashable -- key for a COM entity.

    Comparing proxies by ``id()`` does not work: pywin32 builds a fresh
    wrapper on every access, so the same edge gets a different ``id()`` each
    time and no dimension would ever match a registered entity. The COM
    identity of the underlying IUnknown is what is actually stable.

    The returned PyIUnknown supports ``==`` but NOT ``hash()``. Using it as a
    dict key raises "unhashable type: 'PyIUnknown'", which is exactly what
    made every dimension report attached=[] and made verify_drawing flag
    plainly dimensioned edges as having no dimension. Match with ``==`` over
    a list of pairs instead; a drawing view has tens of entities, so the
    linear scan costs nothing.
    """
    try:
        return win32com.client.Dispatch(com_entity)._oleobj_.QueryInterface(
            pythoncom.IID_IUnknown)
    except Exception:
        try:
            return com_entity._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        except Exception:
            return id(com_entity)


# ---------------------------------------------------------------------------
# Drawing selection
# ---------------------------------------------------------------------------


def _find_view_for_point(doc, x_m: float, y_m: float):
    """(view, view_name) of the view whose outline contains a sheet point.

    This is the fix for the "dimension landed in the wrong view" class of
    error. The previous code searched only the active view, so on a sheet
    with front, top, side and iso views a point in the side view was matched
    against the front view's edges -- yielding either "no edge found" or,
    worse, a nearby edge of the wrong view with no error reported.
    """
    candidates = []
    for _sheet_name, view in _all_drawing_views(doc):
        try:
            outline = _view_outline_m(view)
        except Exception:
            continue
        if ad.point_in_box((x_m, y_m), outline):
            candidates.append((view, _view_name(view), ad.box_size(outline)))
    if not candidates:
        names = [_view_name(v) for _s, v in _all_drawing_views(doc)]
        raise RuntimeError(
            f"The point ({ad.mm(x_m)}, {ad.mm(y_m)}) mm is not inside any drawing "
            f"view. Views on this drawing: {', '.join(names) if names else '(none)'}. "
            f"Call get_drawing_layout for their outlines."
        )
    # Nested outlines happen (a detail view drawn over its parent). The
    # smallest containing view is the one the point is "in".
    view, name, _size = min(candidates, key=lambda c: c[2][0] * c[2][1])
    return view, name


def _select_drawing_edge_near(doc, x_m: float, y_m: float,
                              tolerance_m: float = 0.002,
                              append: bool = False,
                              view=None, view_name: Optional[str] = None):
    """Select the visible drawing edge a sheet point means.

    ``SelectByID2`` does not reliably hit projected model edges in drawings,
    so this enumerates the view's entities and selects the object -- the
    approach the live test report found to be the only one that works.

    Three things it does that the previous version did not: it looks in the
    view that actually contains the point, it understands circles (clicking
    inside a hole selects the hole), and it refuses an ambiguous point
    instead of picking by enumeration order.

    Returns (view, view_name, EdgeInfo).
    """
    if view is None:
        view, view_name = _find_view_for_point(doc, x_m, y_m)
    entities = _read_view_entities(doc, view, view_name)
    if not entities:
        raise RuntimeError(
            f"View '{view_name}' reports no visible edges. If it is an assembly "
            f"view, check that its components are resolved."
        )
    try:
        edge, _distance = ad.pick_nearest_edge(entities, x_m, y_m,
                                               tolerance_m=tolerance_m)
    except ad.AmbiguousPick as exc:
        raise RuntimeError(str(exc)) from exc
    except LookupError as exc:
        raise RuntimeError(f"{exc} (searched view '{view_name}')") from exc

    item = _ENTITY_REGISTRY.resolve(_doc_title(doc), edge.id)
    if not append:
        doc.ClearSelection2(True)
    if not view.SelectEntity(item["entity"], append):
        raise RuntimeError(
            f"SolidWorks refused to select {edge.id} in view '{view_name}'."
        )
    return view, view_name, edge


def _layout_dict(doc, config: Optional["ad.SheetConfig"] = None) -> dict:
    """The whole drawing as data: sheets, formats, views, outlines, issues.

    Shared by get_drawing_layout and verify_drawing so the two can never
    disagree about where a view is.
    """
    config = config or ad.SheetConfig()
    sheets = []
    for sheet_name in _sheet_names(doc):
        sheet, views = _sheet_and_views(doc, sheet_name)
        props = _sheet_properties(sheet)
        width_mm, height_mm = ad.mm(props["width_m"]), ad.mm(props["height_m"])
        usable = config.usable_area(width_mm, height_mm)
        title_block = config.title_block_box(width_mm, height_mm)
        view_dicts = []
        for view in views:
            name = _view_name(view)
            try:
                outline_m = _view_outline_m(view)
            except Exception:
                log.warning("No outline for view %r", name, exc_info=True)
                continue
            ratio = _view_scale_ratio(view)
            entry = {
                "name": name,
                "outline_mm": [ad.mm(c) for c in outline_m],
                "center_mm": [ad.mm(c) for c in _view_position_m(view)],
                "scale": ad.format_scale(*ratio),
                "scale_ratio": ratio,
                "scale_is_normalized": ad.is_normalized_scale(*ratio),
            }
            size = ad.box_size(entry["outline_mm"])
            entry["size_mm"] = [round(s, 3) for s in size]
            for attribute, key in (("Type", "type"),
                                   ("GetReferencedModelName", "model"),
                                   ("ReferencedConfiguration", "configuration")):
                try:
                    entry[key] = _com_member(view, attribute)
                    if entry[key] is not None and key != "type":
                        entry[key] = str(entry[key])
                except Exception:
                    entry[key] = None
            view_dicts.append(entry)
        sheets.append({
            "name": sheet_name,
            "format": ad.identify_format(width_mm, height_mm),
            "size_mm": [width_mm, height_mm],
            "sheet_scale": ad.format_scale(*props["sheet_scale"]),
            "projection": "first_angle" if props["first_angle"] else "third_angle",
            "usable_area_mm": usable,
            "title_block_mm": title_block,
            "views": view_dicts,
            "issues": ad.check_sheet_layout(view_dicts, usable, title_block, config),
        })
    return {"drawing": _doc_title(doc), "sheets": sheets}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_drawing_layout() -> dict:
    """Describe every sheet of the active drawing as data.

    Returns, per sheet: paper format and size in mm, sheet scale, projection
    angle (first/third), the usable area inside the margins, the title-block
    rectangle, and every view with its name, referenced model and
    configuration, effective scale, centre and outline in mm. Layout problems
    already found (views overlapping, a view outside the usable area or over
    the title block, a scale that is not on the normalised series) come back
    in each sheet's `issues`.

    Call this BEFORE positioning a view or placing a dimension: the view
    names it returns are what every other drawing tool takes, and the
    outlines are what makes a sheet coordinate meaningful."""

    def _impl():
        return _layout_dict(_active_drawing())

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_view_entities(view_name: str, kinds: str = "line,circle,arc",
                            min_length_mm: float = 0.0,
                            max_results: int = 200) -> dict:
    """List the visible edges of a drawing view, each with a stable ID.

    kinds: comma-separated subset of line, circle, arc.
    min_length_mm filters out detail edges (chamfer run-outs, fillet blends).

    Every entity comes back with sheet coordinates (what a click would
    target), the true MODEL length or diameter in mm (what a dimension will
    read), its orientation, and an ID of the form "<view>#E<n>". Pass those
    IDs to dimension_by_entity_ids instead of guessing sheet coordinates.

    IDs stay valid until the drawing is rebuilt. If the drawing changes, call
    this again -- an expired ID is refused, never silently resolved.

    For a view with hundreds of edges, a summary is returned instead of the
    full list once max_results is exceeded; narrow it with kinds and
    min_length_mm."""

    def _impl():
        wanted = {k.strip().lower() for k in kinds.split(",") if k.strip()}
        unknown = wanted - {"line", "circle", "arc"}
        if unknown:
            raise ValueError(
                f"Unknown entity kind(s): {', '.join(sorted(unknown))}. "
                f"Use line, circle and/or arc."
            )
        doc = _active_drawing()
        view = _find_view(doc, view_name)
        entities = _read_view_entities(doc, view, view_name)
        selected = [
            e for e in entities
            if e.kind in wanted
            and (e.model_length is None
                 or ad.mm(e.model_length) >= min_length_mm)
        ]
        result = {
            "view": view_name,
            "total_visible": len(entities),
            "matched": len(selected),
            "ids_valid_until": "the next rebuild of this drawing",
        }
        counts: dict = {}
        for e in selected:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        result["counts_by_kind"] = counts
        if len(selected) > max_results:
            diameters = sorted({e.as_dict().get("diameter_model_mm")
                                for e in selected if e.kind == "circle"})
            result["truncated"] = True
            result["summary"] = (
                f"{len(selected)} entities matched, more than max_results="
                f"{max_results}. Hole diameters present: "
                f"{', '.join(f'{d:g}' for d in diameters) if diameters else 'none'}. "
                f"Raise min_length_mm or narrow kinds."
            )
            result["entities"] = [e.as_dict() for e in selected[:max_results]]
        else:
            result["truncated"] = False
            result["entities"] = [e.as_dict() for e in selected]
        return result

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_view_dimensions(view_name: str) -> dict:
    """List the dimensions already shown in a drawing view.

    Each dimension comes back with its MEASURED value (mm, or degrees for an
    angular one), its type code, whether it is driven, the sheet position of
    its text, which registered entity IDs it is attached to, and whether it
    has come adrift from the model (dangling).

    Use it to find out what is already dimensioned before adding more, to
    catch duplicates, and to confirm that a dimension measures what was
    intended. Entity IDs resolve only for entities read by get_view_entities
    in this session; others show as "unregistered"."""

    def _impl():
        doc = _active_drawing()
        view = _find_view(doc, view_name)
        dimensions = _read_view_dimensions(doc, view, view_name)
        return {"view": view_name, "count": len(dimensions),
                "dimensions": dimensions}

    return await _run(_impl)


def _add_dimension_at(doc, px_m: float, py_m: float):
    """AddDimension2 with the modal value box disabled.

    Left enabled, SolidWorks pops a "Modify" box on creation and the COM call
    never returns -- the tool just times out. add_sketch_dimension already
    guards this; the drawing path did not.
    """
    app = _connect()
    original = None
    try:
        original = bool(app.GetUserPreferenceToggle(_INPUT_DIM_VAL_ON_CREATE))
        app.SetUserPreferenceToggle(_INPUT_DIM_VAL_ON_CREATE, False)
    except Exception:
        log.warning("Could not disable swInputDimValOnCreate; a modal dialog "
                    "may block this call", exc_info=True)
    try:
        return doc.AddDimension2(px_m, py_m, 0)
    finally:
        if original is not None:
            try:
                app.SetUserPreferenceToggle(_INPUT_DIM_VAL_ON_CREATE, original)
            except Exception:
                log.warning("Could not restore swInputDimValOnCreate",
                            exc_info=True)


def _measure_display_dimension(display, view_name: str, index_hint: int = 1) -> dict:
    """Read back what a freshly created dimension actually measures."""
    display = win32com.client.Dispatch(display)
    out = {"id": f"{view_name}#D{index_hint}"}
    dimension = win32com.client.Dispatch(_com_member(display, "GetDimension2", 0))
    value, unit = _read_dimension_value(dimension)
    try:
        dim_type = int(_com_member(display, "Type2"))
    except Exception:
        dim_type = None
    out.update({
        "name": str(_com_member(dimension, "FullName")),
        "type_code": dim_type,
        "unit": unit,
        "value": value,
    })
    try:
        annotation = win32com.client.Dispatch(_com_member(display, "GetAnnotation"))
        attached = _com_member(annotation, "GetAttachedEntities3") or ()
        out["attached_count"] = sum(1 for e in attached if e is not None)
        position = tuple(_com_member(annotation, "GetPosition") or ())
        out["text_position_mm"] = [ad.mm(c) for c in position[:2]]
    except Exception:
        log.warning("Could not read the new dimension's attachments",
                    exc_info=True)
        out["attached_count"] = None
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def dimension_by_entity_ids(entity_ids: list[str], side: str = "auto",
                                  expected_mm: Optional[float] = None,
                                  tolerance_mm: float = 0.5,
                                  place_x: Optional[float] = None,
                                  place_y: Optional[float] = None,
                                  unit: Optional[str] = None) -> dict:
    """Dimension one or two entities by the IDs from get_view_entities.

    One ID gives that entity's own measure (an edge's length, a circle's
    diameter); two IDs give the distance between them. Both must be in the
    same view.

    side places the text outside the view: above, below, left, right, or
    "auto" to follow drawing convention (horizontal measures above/below,
    vertical ones left/right, each to the nearer outside). place_x/place_y
    override it with explicit sheet coordinates.

    expected_mm turns the call into an assertion. Pass the value the model is
    supposed to have and the measured value is compared against it: if the
    dimension landed on the wrong entity you get a critical E01 issue with
    both numbers, instead of a success message. This is the single most
    useful argument here -- use it whenever the intended value is known.

    Returns the measured value, how many entities the dimension actually
    attached to, where the text went, and an `issues` list."""

    def _impl():
        if not 1 <= len(entity_ids) <= 2:
            raise ValueError(
                f"Pass 1 or 2 entity IDs, got {len(entity_ids)}. One ID measures "
                f"that entity; two measure the distance between them."
            )
        if side != "auto" and side not in ad.SIDES:
            raise ValueError(
                f"side must be 'auto' or one of {', '.join(ad.SIDES)}, got '{side}'."
            )
        doc = _active_drawing()
        config = ad.SheetConfig()
        doc_title = _doc_title(doc)
        items = [_ENTITY_REGISTRY.resolve(doc_title, eid) for eid in entity_ids]
        view_names = {item["view"] for item in items}
        if len(view_names) > 1:
            raise ValueError(
                f"The entities are in different views ({', '.join(sorted(view_names))}); "
                f"a dimension cannot span views."
            )
        view_name = items[0]["view"]
        view = _find_view(doc, view_name)

        try:
            doc.ActivateView(view_name)
        except Exception:
            log.warning("ActivateView(%r) failed; continuing", view_name,
                        exc_info=True)
        doc.ClearSelection2(True)
        for index, (entity_id, item) in enumerate(zip(entity_ids, items)):
            if not view.SelectEntity(item["entity"], index > 0):
                raise RuntimeError(
                    f"SolidWorks refused to select {entity_id} in view "
                    f"'{view_name}'. Re-read the view with get_view_entities."
                )
        # The bug this replaces: the second SelectByID2 was never checked, so
        # a failed second pick silently became "length of the first edge"
        # reported as "distance between two points".
        selected_count = doc.SelectionManager.GetSelectedObjectCount2(-1)
        if selected_count != len(items):
            doc.ClearSelection2(True)
            raise RuntimeError(
                f"Selection is inconsistent: SolidWorks holds {selected_count} "
                f"entities, {len(items)} were selected. Aborting rather than "
                f"creating a dimension on the wrong geometry."
            )

        outline_m = _view_outline_m(view)
        if place_x is not None and place_y is not None:
            px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)
            chosen_side = "explicit"
        else:
            edges = _read_view_entities(doc, view, view_name, register=False)
            reference = next((e for e in edges if e.kind == items[0]["kind"]), None)
            chosen_side = side
            if side == "auto":
                chosen_side = (ad.side_for_edge(reference, outline_m)
                               if reference is not None else "above")
            px_m, py_m = ad.place_dimension_text(
                outline_m, chosen_side, ad.to_m(config.dim_offset_mm))

        display = _add_dimension_at(doc, px_m, py_m)
        if display is None:
            doc.ClearSelection2(True)
            raise RuntimeError(
                "AddDimension2 created no dimension for the selected entities. "
                "The pair may not be dimensionable (for example two skew edges)."
            )
        measured = _measure_display_dimension(display, view_name)
        doc.ClearSelection2(True)

        issues = ad.check_measured_value(
            measured["value"],
            expected_mm=expected_mm,
            tolerance_mm=tolerance_mm,
            attached_count=measured.get("attached_count"),
            expected_attachments=len(items),
            ref=measured["id"],
        )
        return {
            "ok": not any(i["severity"] == "critical" for i in issues),
            "view": view_name,
            "entity_ids": list(entity_ids),
            "value": measured["value"],
            "unit": measured["unit"],
            "expected_mm": expected_mm,
            "attached_count": measured.get("attached_count"),
            "side": chosen_side,
            "text_position_mm": measured.get(
                "text_position_mm", [ad.mm(px_m), ad.mm(py_m)]),
            "issues": issues,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def verify_drawing(require_hole_dimensions: bool = True,
                         require_overall_extents: bool = True,
                         require_cut_angles: bool = True,
                         min_edge_length_mm: float = 3.0) -> dict:
    """Check the active drawing and return every problem found. Read-only.

    Runs the full set of detectors: views overlapping (E06), a view outside
    the usable area or over the title block (E07), a scale that is not on the
    normalised series (E08), holes and overall extents with no dimension
    (E02), duplicate dimensions (E03), dimension text at the sheet origin or
    sitting on another view (E05), and dangling dimensions (E16).

    Each issue carries a code, a severity (critical/warning/info), a message
    and the view, entity or dimension IDs it refers to -- so a finding turns
    straight into the next action.

    Call this before telling anyone a drawing is finished. `status` is
    "approved" only when nothing was found."""

    def _impl():
        doc = _active_drawing()
        config = ad.SheetConfig()
        rules = ad.CoverageRules(
            require_overall_extents=require_overall_extents,
            require_hole_diameters=require_hole_dimensions,
            require_cut_angles=require_cut_angles,
            min_edge_length_mm=min_edge_length_mm,
        )
        layout = _layout_dict(doc, config)
        issues = [i for sheet in layout["sheets"] for i in sheet["issues"]]
        checked_views = []
        for sheet in layout["sheets"]:
            text_check_views = []
            for view_entry in sheet["views"]:
                view_name = view_entry["name"]
                try:
                    view = _find_view(doc, view_name)
                    edges = _read_view_entities(doc, view, view_name)
                    dimensions = _read_view_dimensions(doc, view, view_name)
                except Exception as exc:
                    issues.append(ad.issue(
                        "E02", "warning",
                        f"could not inspect view {view_name}: {exc}", view_name))
                    continue
                issues.extend(ad.check_dimension_coverage(
                    view_name, edges, dimensions, rules))
                text_check_views.append({
                    "name": view_name,
                    "outline_mm": view_entry["outline_mm"],
                    "usable_area_mm": sheet["usable_area_mm"],
                    "dimensions": dimensions,
                })
                checked_views.append({
                    "view": view_name,
                    "visible_edges": len(edges),
                    "dimensions": len(dimensions),
                })
            issues.extend(ad.check_text_positions(text_check_views, config))
        return {
            "status": ad.worst_status(issues),
            "count": len(issues),
            "critical": sum(1 for i in issues if i["severity"] == "critical"),
            "warnings": sum(1 for i in issues if i["severity"] == "warning"),
            "views_checked": checked_views,
            "issues": issues,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_section_view(
    x1: float, y1: float, x2: float, y2: float,
    place_x: float = 250, place_y: float = 150,
    unit: Optional[str] = None,
) -> dict:
    """Insert a section view (cut-through) into the active drawing.

    Draws a cutting line from (x1,y1) to (x2,y2) across an existing view, then
    creates a new view showing the interior as if sliced along that line.
    Reveals wall thicknesses, internal holes, and hidden structure.

    x1/y1, x2/y2: endpoints of the section cut line (sheet coordinates).
    place_x/place_y: where to place the resulting section view."""

    def _impl():
        doc = _active_drawing()

        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
        px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)

        skmgr = doc.SketchManager
        line = skmgr.CreateLine(x1_m, y1_m, 0, x2_m, y2_m, 0)
        # The section line must be the current selection when the view is cut.
        try:
            if line is not None:
                seg = win32com.client.Dispatch(line)
                seg.Select4(False, win32com.client.VARIANT(pythoncom.VT_DISPATCH, None))
        except Exception:
            pass

        try:
            view = doc.CreateSectionViewAt5(
                px_m, py_m, 0,
                "",         # section label (auto)
                4097,       # options: swCreateSectionViewAtOptions (cut + auto-hatch)
                None,       # excludeComps
                0.0,        # section depth
            )
        except Exception as e:
            log.warning("CreateSectionViewAt5 failed: %s", e)
            view = None

        if view is None:
            raise RuntimeError(
                "Section view creation failed. Ensure the cut line crosses an existing "
                "model view and that a view is selected first."
            )

        view = win32com.client.Dispatch(view)
        return {
            "view": view.Name if hasattr(view, "Name") else "SectionView",
            "cut_line": [[x1, y1], [x2, y2]],
            "placed_at": [place_x, place_y],
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_detail_view(
    center_x: float, center_y: float, radius: float,
    place_x: float = 300, place_y: float = 150,
    scale: float = 2.0,
    unit: Optional[str] = None,
) -> dict:
    """Insert a detail view (magnified close-up) into the active drawing.

    Draws a circle around a region of an existing view and creates a new,
    enlarged view of just that region — the 'Detalhe A/B/C' callouts seen on
    fabrication drawings.

    center_x/center_y: center of the detail circle (sheet coordinates).
    radius: radius of the detail circle.
    place_x/place_y: where to place the enlarged view.
    scale: magnification factor (2.0 = 2:1)."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        if scale <= 0:
            raise ValueError(f"Scale must be positive, got {scale}.")

        doc = _active_drawing()

        cx_m, cy_m = to_meters(center_x, unit), to_meters(center_y, unit)
        r_m = to_meters(radius, unit)
        px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)

        circle = doc.SketchManager.CreateCircle(cx_m, cy_m, 0, cx_m + r_m, cy_m, 0)
        # The detail circle must be selected when the detail view is created.
        try:
            if circle is not None:
                seg = win32com.client.Dispatch(circle)
                seg.Select4(False, win32com.client.VARIANT(pythoncom.VT_DISPATCH, None))
        except Exception:
            pass

        # CreateDetailViewAt4(X, Y, Z, Style, Scale1, Scale2, LabelIn, Showtype,
        #   FullOutline, JaggedOutline, NoOutline, ShapeIntensity) — 12 args.
        try:
            view = doc.CreateDetailViewAt4(
                px_m, py_m, 0,
                1,          # Style: swDetViewSTANDARD
                scale,      # Scale1 (numerator)
                1.0,        # Scale2 (denominator)
                "",         # LabelIn
                0,          # Showtype
                False,      # FullOutline
                False,      # JaggedOutline
                False,      # NoOutline
                0.0,        # ShapeIntensity
            )
        except Exception as e:
            log.warning("CreateDetailViewAt4 failed: %s", e)
            view = None

        if view is None:
            raise RuntimeError(
                "Detail view creation failed. Ensure the circle is drawn over an "
                "existing model view and that a parent view is selected."
            )

        view = win32com.client.Dispatch(view)
        return {
            "view": view.Name if hasattr(view, "Name") else "DetailView",
            "circle": {"center": [center_x, center_y], "radius": radius},
            "placed_at": [place_x, place_y],
            "scale": scale,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_broken_view(
    break_position1: float, break_position2: float,
    orientation: str = "vertical",
    gap: float = 10,
    unit: Optional[str] = None,
) -> dict:
    """Add a broken view (removes the middle of a long part to save paper space).

    Adds two break lines to the currently selected view and removes the region
    between them, so a very long beam or tank fits on the sheet at a larger scale.

    break_position1/2: sheet coordinates of the two break lines along the
                       break axis. They must fall within the selected view.
    orientation: 'vertical' (break lines are vertical, for horizontally-long parts)
                 or 'horizontal'.
    gap: visual gap left between the two pieces."""

    def _impl():
        doc = _active_drawing()
        view = _get_selected_view(doc)
        if view is None:
            raise RuntimeError("No drawing view is selected. Select a view first.")

        orientation_key = orientation.lower()
        if orientation_key not in {"vertical", "horizontal"}:
            raise ValueError("orientation must be either 'vertical' or 'horizontal'.")

        # swBreakLineOrientation_e: horizontal=1, vertical=2.  A break is
        # created in two stages in the SolidWorks API: insert the break line
        # in the IView, then apply it through IDrawingDoc.BreakView.
        orient = 2 if orientation_key == "vertical" else 1
        p1_sheet_m = to_meters(break_position1, unit)
        p2_sheet_m = to_meters(break_position2, unit)
        gap_m = to_meters(gap, unit)
        if gap_m <= 0:
            raise ValueError("gap must be positive.")

        try:
            # The drawing must operate on the selected model view.  Selecting
            # it explicitly also makes this robust when the caller did not
            # click a view before invoking the MCP tool.
            view_name = view.Name
            doc.ActivateView(view_name)
            doc.ClearSelection2(True)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2(
                view_name, "DRAWINGVIEW", 0, 0, 0, False, 0, empty, 0
            ):
                raise RuntimeError(f"Could not select drawing view '{view_name}'.")

            selected = doc.SelectionManager.GetSelectedObject6(1, -1)
            if selected is not None:
                view = win32com.client.Dispatch(selected)

            # InsertBreak positions are relative to the drawing view origin,
            # while the MCP contract accepts sheet coordinates.  Translate
            # them and reject positions outside this exact view before the COM
            # call, which otherwise only returns a null break line.
            view_position = tuple(view.Position)
            view_outline = tuple(view.GetOutline)
            axis_origin = view_position[0] if orient == 2 else view_position[1]
            axis_min = view_outline[0] if orient == 2 else view_outline[1]
            axis_max = view_outline[2] if orient == 2 else view_outline[3]
            if not (axis_min < p1_sheet_m < axis_max and axis_min < p2_sheet_m < axis_max):
                raise ValueError(
                    "Break positions must both lie inside the selected view "
                    f"({axis_min:.6f} to {axis_max:.6f} m on the break axis)."
                )
            if p1_sheet_m == p2_sheet_m:
                raise ValueError("break_position1 and break_position2 must differ.")
            p1_m = p1_sheet_m - axis_origin
            p2_m = p2_sheet_m - axis_origin

            # IView.InsertBreak expects the two break-line positions and a
            # style. BreakLineGap controls the visible separation afterwards.
            brk = view.InsertBreak(orient, p1_m, p2_m, 2)  # 2 = ZigZag
            if brk is None:
                raise RuntimeError("SolidWorks did not create a break line.")
            brk = win32com.client.Dispatch(brk)
            if not brk.SetPosition(p1_m, p2_m):
                raise RuntimeError("SolidWorks could not position the break lines.")
            view.BreakLineGap = gap_m
            doc.BreakView()
            is_broken = view.IsBroken
            if callable(is_broken):
                is_broken = is_broken()
            if not is_broken:
                raise RuntimeError("SolidWorks did not apply the break to the drawing view.")
        except Exception as exc:
            raise RuntimeError(
                "Broken view failed. Ensure the break positions lie inside the "
                "selected model view."
            ) from exc

        try:
            doc.EditRebuild3()
        except Exception:
            pass

        return {
            "view": view.Name if hasattr(view, "Name") else "View",
            "break_positions": [break_position1, break_position2],
            "orientation": orientation,
            "gap": gap,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_auxiliary_view(
    edge_x: float, edge_y: float,
    place_x: float = 300, place_y: float = 200,
    unit: Optional[str] = None,
) -> dict:
    """Insert an auxiliary view projected perpendicular to a selected edge.

    Auxiliary views show inclined faces in true size by projecting at an angle.
    Select a reference edge (in an existing view) that the new view will be
    projected normal to.

    edge_x/edge_y: a point on the reference edge (sheet coordinates).
    place_x/place_y: where to place the projected view."""

    def _impl():
        doc = _active_drawing()

        ex_m, ey_m = to_meters(edge_x, unit), to_meters(edge_y, unit)
        px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)

        _select_drawing_edge_near(doc, ex_m, ey_m)

        # CreateAuxiliaryViewAt2(X, Y, Z, NotAligned, Label, Showarrow, Flip) — 7 args.
        try:
            view = doc.CreateAuxiliaryViewAt2(px_m, py_m, 0, False, "", True, False)
        except Exception as e:
            log.warning("CreateAuxiliaryViewAt2 failed: %s", e)
            view = None

        if view is None:
            raise RuntimeError(
                "Auxiliary view failed. Select a straight edge in an existing view first."
            )

        view = win32com.client.Dispatch(view)
        return {
            "view": view.Name if hasattr(view, "Name") else "AuxView",
            "reference_edge": [edge_x, edge_y],
            "placed_at": [place_x, place_y],
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_drawing_dimension(
    x1: float, y1: float,
    x2: Optional[float] = None, y2: Optional[float] = None,
    place_x: Optional[float] = None, place_y: Optional[float] = None,
    expected_mm: Optional[float] = None,
    tolerance_mm: float = 0.5,
    unit: Optional[str] = None,
) -> dict:
    """Add a dimension to the active drawing, picking entities by sheet point.

    Resolves (x1,y1) -- and (x2,y2) if given -- to real drawing entities in
    the view that CONTAINS each point, selects them as objects, and returns
    the value SolidWorks actually measured.

    A point inside a hole selects that hole. An ambiguous point (two entities
    equally close) is refused with the candidate IDs rather than resolved by
    luck. If place_x/place_y are omitted the text goes just outside the view,
    never to the sheet origin.

    expected_mm turns the call into an assertion: pass the intended value and
    a mismatch comes back as a critical E01 issue with both numbers.

    Prefer dimension_by_entity_ids: get_view_entities gives IDs and true
    model lengths, so there is nothing left to estimate. This tool stays for
    the case where only a sheet position is known."""

    def _impl():
        doc = _active_drawing()
        config = ad.SheetConfig()
        doc.ClearSelection2(True)

        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        view, view_name, first_edge = _select_drawing_edge_near(doc, x1_m, y1_m)
        picked = [first_edge]

        if (x2 is None) != (y2 is None):
            raise ValueError(
                "Give both x2 and y2 for a two-entity dimension, or neither."
            )
        if x2 is not None and y2 is not None:
            x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
            second_view, second_name = _find_view_for_point(doc, x2_m, y2_m)
            if second_name != view_name:
                doc.ClearSelection2(True)
                raise RuntimeError(
                    f"({x1}, {y1}) is in view '{view_name}' but ({x2}, {y2}) is in "
                    f"'{second_name}'. A dimension cannot span two views."
                )
            # Appending, and checked -- the unchecked second selection is what
            # used to turn "distance between two points" into "length of one
            # edge" with a success message.
            _view, _name, second_edge = _select_drawing_edge_near(
                doc, x2_m, y2_m, append=True, view=view, view_name=view_name)
            if second_edge.id == first_edge.id:
                doc.ClearSelection2(True)
                raise RuntimeError(
                    f"Both points resolved to the same entity ({first_edge.id}). "
                    f"For a distance, aim at two different entities."
                )
            picked.append(second_edge)
            selected_count = doc.SelectionManager.GetSelectedObjectCount2(-1)
            if selected_count != 2:
                doc.ClearSelection2(True)
                raise RuntimeError(
                    f"SolidWorks holds {selected_count} entities after selecting 2. "
                    f"Aborting rather than dimensioning the wrong geometry."
                )

        outline_m = _view_outline_m(view)
        if place_x is not None and place_y is not None:
            px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)
            chosen_side = "explicit"
        else:
            chosen_side = ad.side_for_edge(first_edge, outline_m)
            px_m, py_m = ad.place_dimension_text(
                outline_m, chosen_side, ad.to_m(config.dim_offset_mm))

        display = _add_dimension_at(doc, px_m, py_m)
        if display is None:
            doc.ClearSelection2(True)
            raise RuntimeError(
                "AddDimension2 created no dimension for the selected entities."
            )
        measured = _measure_display_dimension(display, view_name)
        doc.ClearSelection2(True)

        issues = ad.check_measured_value(
            measured["value"],
            expected_mm=expected_mm,
            tolerance_mm=tolerance_mm,
            attached_count=measured.get("attached_count"),
            expected_attachments=len(picked),
            ref=measured["id"],
        )
        return {
            "ok": not any(i["severity"] == "critical" for i in issues),
            "view": view_name,
            "entity_ids": [e.id for e in picked],
            "value": measured["value"],
            "unit": measured["unit"],
            "expected_mm": expected_mm,
            "attached_count": measured.get("attached_count"),
            "side": chosen_side,
            "text_position_mm": measured.get(
                "text_position_mm", [ad.mm(px_m), ad.mm(py_m)]),
            "issues": issues,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_drawing_annotation(
    text: str,
    x: float = 100, y: float = 100,
    height: float = 3.5,
    unit: Optional[str] = None,
) -> dict:
    """Add a free text note to the active drawing.

    Used for general notes, applicable standards (e.g. 'NBR 8800'), material
    specs, and callouts.

    text: the note text.
    x/y: position on the drawing sheet.
    height: text height in mm (typographic)."""

    def _impl():
        doc = _active_drawing()
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)

        note = doc.InsertNote(text)
        if note is None:
            raise RuntimeError("Failed to insert note.")
        note = win32com.client.Dispatch(note)

        try:
            annotation = note.GetAnnotation
            if callable(annotation):
                annotation = annotation()
            annotation = win32com.client.Dispatch(annotation)
            annotation.SetPosition(x_m, y_m, 0)
        except Exception:
            pass

        try:
            tf = note.GetTextFormat
            if callable(tf):
                tf = tf()
            tf = win32com.client.Dispatch(tf)
            tf.CharHeight = to_meters(height, "mm")
            note.SetTextFormat(0, False, tf)
        except Exception:
            pass

        return {"text": text, "placed_at": [x, y], "height": height, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_centerline(
    x1: float, y1: float, x2: float, y2: float,
    unit: Optional[str] = None,
) -> dict:
    """Add a centerline (dashed axis line) to the active drawing.

    Marks axes of symmetry and hole centers. Provide the two endpoints, or
    select two parallel edges near these points to auto-generate a centerline
    between them.

    x1/y1, x2/y2: endpoints (or points on two edges) in sheet coordinates."""

    def _impl():
        doc = _active_drawing()
        doc.ClearSelection2(True)

        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        e1 = doc.Extension.SelectByID2("", "EDGE", x1_m, y1_m, 0, False, 0, empty, 0)
        e2 = doc.Extension.SelectByID2("", "EDGE", x2_m, y2_m, 0, True, 0, empty, 0)

        if e1 and e2:
            try:
                view = _get_selected_view(doc)
                if view is not None:
                    view.InsertCenterLine2()
                    return {"type": "centerline (between 2 edges)",
                            "from": [x1, y1], "to": [x2, y2], "unit": unit or _default_unit}
            except Exception:
                pass

        line = doc.SketchManager.CreateCenterLine(x1_m, y1_m, 0, x2_m, y2_m, 0)
        if line is None:
            raise RuntimeError("Failed to add centerline.")
        return {"type": "centerline (manual)", "from": [x1, y1], "to": [x2, y2],
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_weld_symbol(
    x: float, y: float,
    weld_type: str = "fillet",
    size: float = 5,
    unit: Optional[str] = None,
) -> dict:
    """Add a welding symbol to the active drawing at a point on an edge.

    weld_type: 'fillet', 'square', 'bevel', 'vee', 'plug'.
    size: weld leg/throat size.
    x/y: point on the edge/joint to attach the symbol (sheet coordinates)."""

    def _impl():
        doc = _active_drawing()
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        if size <= 0:
            raise ValueError("size must be positive.")

        # IDrawingDoc.InsertWeldSymbol is the API that creates an attached,
        # configured drawing annotation. InsertWeldSymbol3 only creates a
        # generic symbol object and cannot reliably attach it in this binding.
        symbols = {
            "fillet": "<WELD-FILL>",
            "square": "<WELD-BUTT>",
            "bevel": "<WELD-BUSB>",
            "vee": "<WELD-BUSV>",
            "plug": "<WELD-PLUG>",
        }
        symbol = symbols.get(weld_type.lower())
        if symbol is None:
            raise ValueError(f"Unsupported weld_type '{weld_type}'. Use: {', '.join(symbols)}.")

        source_view, _sel_view_name, _sel_edge = _select_drawing_edge_near(doc, x_m, y_m)
        before = source_view.GetWeldSymbolCount
        if callable(before):
            before = before()
        doc.InsertWeldSymbol(
            f"{size:g}", symbol, "",
            False, False, False, False, False, False, "",
        )
        after = source_view.GetWeldSymbolCount
        if callable(after):
            after = after()
        if after <= before:
            raise RuntimeError("SolidWorks did not create the requested weld symbol.")

        return {"weld_type": weld_type, "size": size, "placed_at": [x, y],
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_surface_finish(
    x: float, y: float,
    ra_value: float = 3.2,
    symbol_type: str = "machined",
    unit: Optional[str] = None,
) -> dict:
    """Add a surface finish symbol (roughness callout) to the active drawing.

    ra_value: roughness Ra value in micrometers (e.g. 3.2, 1.6, 0.8).
    symbol_type: 'basic', 'machined' (with bar), 'nomachining' (circle).
    x/y: point on the surface/edge to attach the symbol (sheet coordinates)."""

    def _impl():
        doc = _active_drawing()
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        if ra_value <= 0:
            raise ValueError("ra_value must be positive.")
        _select_drawing_edge_near(doc, x_m, y_m)

        types = {"basic": 0, "machined": 1, "nomachining": 2}
        sym_code = types.get(symbol_type.lower(), 1)

        # InsertSurfaceFinishSymbol3(SymType, LeaderType, LocX, LocY, LocZ,
        #   LaySymbol, ArrowType, MachAllowance, OtherVals, ProdMethod, SampleLen,
        #   MaxRoughness, MinRoughness, RoughnessSpacing) — 14 args.
        sf = None
        try:
            sf = doc.Extension.InsertSurfaceFinishSymbol3(
                sym_code,          # SymType
                0,                 # LeaderType
                x_m, y_m, 0.0,     # LocX, LocY, LocZ
                0,                 # LaySymbol
                0,                 # ArrowType
                "",                # MachAllowance
                "",                # OtherVals
                "",                # ProdMethod
                "",                # SampleLen
                str(ra_value),     # MaxRoughness
                "",                # MinRoughness
                "",                # RoughnessSpacing
            )
        except Exception as e:
            log.warning("InsertSurfaceFinishSymbol3 failed: %s", e)
            sf = None

        if sf is None:
            raise RuntimeError("Surface finish symbol insertion failed.")

        return {"ra": ra_value, "symbol_type": symbol_type, "placed_at": [x, y],
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_gdt_symbol(
    x: float, y: float,
    symbol: str = "position",
    tolerance: float = 0.1,
    datum: str = "A",
    unit: Optional[str] = None,
) -> dict:
    """Add a geometric tolerance (GD&T) feature control frame to the drawing.

    symbol: 'position', 'concentricity', 'perpendicularity', 'parallelism',
            'flatness', 'straightness', 'circularity', 'cylindricity',
            'angularity', 'symmetry', 'runout'.
    tolerance: the tolerance value.
    datum: reference datum letter(s) (e.g. 'A', 'A|B').
    x/y: attachment point (sheet coordinates)."""

    def _impl():
        doc = _active_drawing()
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        if tolerance <= 0:
            raise ValueError("tolerance must be positive.")

        # A projected drawing edge is not discoverable through SelectByID2 by sheet
        # coordinates. Resolve and select it through the owning drawing view instead.
        _select_drawing_edge_near(doc, x_m, y_m)

        # InsertGtol is surfaced as an already-evaluated IDispatch member by the
        # SolidWorks 2025 COM proxy; calling it again raises "member not found".
        gtol = win32com.client.Dispatch(doc.InsertGtol)
        symbol_map = {
            "position": "<IGTOL-POSI>",
            "concentricity": "<IGTOL-CONC>",
            "perpendicularity": "<IGTOL-PERP>",
            "parallelism": "<IGTOL-PARA>",
            "flatness": "<IGTOL-FLAT>",
            "straightness": "<IGTOL-STRAIGHT>",
            "circularity": "<IGTOL-CIRC>",
            "cylindricity": "<IGTOL-CYL>",
            "angularity": "<IGTOL-ANGULAR>",
            "symmetry": "<IGTOL-SYMMETRY>",
            "runout": "<IGTOL-TRUN>",
        }
        try:
            symbol_code = symbol_map[symbol.lower()]
        except KeyError as e:
            raise ValueError(
                "Unsupported GD&T symbol. Use one of: "
                + ", ".join(symbol_map)
            ) from e

        # SetFrameSymbols2 uses the documented <library-symbol> representation;
        # SetFrameValues2 then fills the tolerance and datum cells in frame one.
        gtol.SetFrameSymbols2(1, symbol_code, False, "", False, "", "", "", "")
        if not gtol.SetFrameValues2(1, str(tolerance), "", datum, "", ""):
            raise RuntimeError("SolidWorks did not set the GD&T frame values.")

        attached = gtol.IsAttached
        if callable(attached):
            attached = attached()
        if not attached:
            raise RuntimeError("SolidWorks created a GD&T frame without attaching it to the selected edge.")

        return {"symbol": symbol, "tolerance": tolerance, "datum": datum,
                "placed_at": [x, y], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_balloon(
    x: float, y: float,
    unit: Optional[str] = None,
) -> dict:
    """Add a BOM balloon (numbered callout) to a component in the drawing.

    Balloons are the numbered circles that link each part in an assembly drawing
    to its row in the bill of materials. Select a component edge near (x,y);
    the balloon auto-fills the item number from the BOM.

    x/y: point on the component to attach the balloon (sheet coordinates)."""

    def _impl():
        doc = _active_drawing()
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        _select_drawing_edge_near(doc, x_m, y_m)

        # In SolidWorks 2025, IModelDocExtension.InsertBOMBalloon2 takes one
        # IBalloonOptions object (the old six-argument method belongs to IModelDoc2).
        note = None
        try:
            # The dynamic proxy omits CreateBalloonOptions; cast the extension
            # to its published interface before accessing the 2025 API.
            extension = win32com.client.Dispatch(
                doc.Extension,
                "IModelDocExtension",
                "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}",
            )
            options = extension.CreateBalloonOptions()
            options.Style = 1              # swBS_Circular
            options.Size = 5               # swBF_5Chars
            options.UpperTextContent = 1   # swBalloonTextItemNumber
            options.UpperText = ""
            options.LowerTextContent = 0   # swBalloonTextCustom
            options.LowerText = ""
            note = extension.InsertBOMBalloon2(options)
        except Exception as e:
            log.warning("InsertBOMBalloon2 failed: %s", e)
            note = None

        if note is None:
            raise RuntimeError(
                "Balloon insertion failed. Ensure a component edge is selected and a "
                "BOM table exists (insert_bom_table first for auto-numbering)."
            )

        return {"placed_at": [x, y], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_bom_table(
    x: float = 300, y: float = 200,
    template: str = "",
    unit: Optional[str] = None,
) -> dict:
    """Insert a Bill of Materials (BOM) table into the active drawing.

    Lists every part in the assembly with item number, quantity, part number,
    description, and material — the parts list seen on fabrication drawings.
    Requires a drawing view of an assembly.

    x/y: top-left placement of the table (sheet coordinates).
    template: path to a custom BOM template (.sldbomtbt), or '' for default."""

    def _impl():
        doc = _active_drawing()
        view = _get_selected_view(doc)
        if view is None:
            raise RuntimeError("No drawing view selected. Insert an assembly view first.")

        x_m, y_m = to_meters(x, unit), to_meters(y, unit)

        # Resolve a BOM template if none supplied. The user preference may contain
        # either a template file or semicolon-separated template directories.
        tmpl = template
        if not tmpl:
            try:
                app = _connect()
                # swFileLocationsBOMTemplates = 92
                loc = app.GetUserPreferenceStringValue(92)
                if loc:
                    for base in loc.split(";"):
                        base = base.strip()
                        candidates = (
                            (base,) if base.lower().endswith(".sldbomtbt")
                            else (os.path.join(base, "bom-standard.sldbomtbt"),)
                        )
                        for candidate in candidates:
                            if os.path.isfile(candidate):
                                tmpl = candidate
                                break
                        if tmpl:
                            break
            except Exception:
                pass

        if not tmpl:
            executable = _find_solidworks_exe()
            if executable:
                install_dir = os.path.dirname(executable)
                for language in ("portuguese-brazilian", "english"):
                    candidate = os.path.join(install_dir, "lang", language, "bom-standard.sldbomtbt")
                    if os.path.isfile(candidate):
                        tmpl = candidate
                        break

        if not tmpl:
            raise RuntimeError("Could not find a SolidWorks BOM table template (.sldbomtbt).")

        configuration = view.ReferencedConfiguration
        if callable(configuration):
            configuration = configuration()
        if not configuration:
            raise RuntimeError("The selected drawing view does not reference an assembly configuration.")

        # InsertBomTable6(UseAnchorPoint, X, Y, AnchorType, BomType,
        #   Configuration, TableTemplate, Hidden, IndentedNumberingType,
        #   DetailedCutList, DissolvePartLevelRows, DisplayAsOneItem).
        try:
            bom = view.InsertBomTable6(
                False,      # UseAnchorPoint
                x_m, y_m,   # X, Y
                1,          # AnchorType: top-left
                2,          # BomType: swBomType_PartsOnly
                configuration,
                tmpl,
                False,      # Hidden
                1,          # IndentedNumberingType
                False,      # DetailedCutList
                False,      # DissolvePartLevelRows
                False,      # DisplayAsOneItem
            )
        except Exception as e:
            log.warning("InsertBomTable4 failed: %s", e)
            bom = None

        if bom is None:
            raise RuntimeError(
                "BOM table insertion failed. Ensure the selected view is of an "
                "assembly and a BOM template is available."
            )

        return {"placed_at": [x, y], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def insert_cut_list_table(
    x: float = 300, y: float = 200,
    template: str = "",
    unit: Optional[str] = None,
) -> dict:
    """Insert a weldment cut list table into the active drawing.

    A cut list is the fabrication-specific parts list for weldments: it groups
    identical structural members and gives the cut length of each — exactly what
    a shop needs to cut beams and tubes to size.

    x/y: top-left placement of the table.
    template: path to a custom cut-list template, or '' for default."""

    def _impl():
        doc = _active_drawing()
        view = _get_selected_view(doc)
        if view is None:
            raise RuntimeError("No drawing view selected. Insert a weldment view first.")

        x_m, y_m = to_meters(x, unit), to_meters(y, unit)

        # InsertWeldmentTable(UseAnchorPoint, X, Y, AnchorType, Configuration,
        #   TableTemplate) — on the drawing view.
        try:
            table = view.InsertWeldmentTable(False, x_m, y_m, 1, "", template)
        except Exception as e:
            log.warning("InsertWeldmentTable failed: %s", e)
            table = None

        if table is None:
            raise RuntimeError(
                "Cut list table insertion failed. Ensure the selected view is of a "
                "weldment part (created with create_weldment_profile)."
            )

        return {"placed_at": [x, y], "unit": unit or _default_unit}

    return await _run(_impl)


# ===========================================================================
# Advanced Assembly tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_exploded_view(
    explode_distance: float = 100,
    direction: str = "y",
    unit: Optional[str] = None,
) -> dict:
    """Create an exploded view of the active assembly.

    Separates each component along an axis so the assembly's structure is visible
    — the classic 'blown-apart' assembly illustration. This creates one explode
    step per component, spacing them out along the chosen direction.

    explode_distance: spacing between components.
    direction: 'x', 'y', or 'z' — axis to explode along."""

    def _impl():
        assy = _active_assembly()

        comps = assy.GetComponents(True)
        if not comps:
            raise RuntimeError("No components to explode in this assembly.")

        axis = direction.lower()
        if axis not in ("x", "y", "z"):
            raise ValueError(f"direction must be 'x', 'y', or 'z', got '{direction}'.")

        dist_m = to_meters(explode_distance, unit)
        dir_index = {"x": 0, "y": 1, "z": 2}[axis]

        # Explode steps belong to IConfiguration, not IAssemblyDoc. Create and
        # activate an explode view before selecting components for its steps.
        created_view = assy.CreateExplodedView
        if callable(created_view):
            created_view = created_view()
        if not created_view:
            raise RuntimeError("SolidWorks could not create an exploded view for the active configuration.")
        explode_names = assy.GetExplodedViewNames
        if callable(explode_names):
            explode_names = explode_names()
        if isinstance(explode_names, str):
            explode_names = (explode_names,)
        explode_name = (explode_names or ())[-1] if explode_names else ""
        if not explode_name or not assy.ShowExploded2(True, explode_name):
            raise RuntimeError("SolidWorks could not activate the new exploded view.")
        configuration = win32com.client.Dispatch(
            assy.ConfigurationManager.ActiveConfiguration,
            "IConfiguration",
            "{83A33D98-27C5-11CE-BFD4-00400513BB57}",
        )

        # AddExplodeStep2(ExplDist, ExplDirIndex, ReverseDir, ExplAng, RotAxisIndex,
        #   ReverseAng, RotateAboutOrigin, AutoSpaceComponentsOnDrag, Error) — one
        #   step per component, each moved a bit further along the axis.
        created_steps = 0
        last_err = None
        for i, raw in enumerate(comps):
            comp = win32com.client.Dispatch(raw)
            assy.ClearSelection2(True)
            try:
                if not comp.SelectByMark(False, 1):
                    continue
            except Exception:
                continue
            step_dist = dist_m * (i + 1)
            try:
                result = configuration.AddExplodeStep2(
                    step_dist, dir_index, False, 0.0, 0, False, False, False
                )
                step, outputs = _split_com_result(result)
                error_code = int(outputs[0]) if outputs else 0
                if step is not None and error_code == 0:
                    created_steps += 1
                else:
                    last_err = error_code
            except Exception as e:
                last_err = e

        if created_steps == 0:
            raise RuntimeError(
                "Exploded view creation could not add steps automatically "
                f"({last_err}). Create the explode manually if needed."
            )

        rebuilt = assy.EditRebuild3
        if callable(rebuilt):
            rebuilt()

        return {"steps": created_steps, "direction": direction,
                "distance": explode_distance, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_mate(name: str) -> dict:
    """Delete a named assembly mate and verify it no longer exists."""

    def _impl():
        assy = _active_assembly()
        mate_name = name.strip()
        if not mate_name:
            raise ValueError("Mate name cannot be empty.")
        known = {
            str(feature.Name).casefold()
            for feature in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature, "GetTypeName2", "")).startswith("Mate")
        }
        if mate_name.casefold() not in known:
            raise ValueError(f"Mate '{mate_name}' does not exist.")
        assy.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not assy.Extension.SelectByID2(mate_name, "MATE", 0, 0, 0, False, 0, empty, 0):
            raise RuntimeError(f"SolidWorks could not select mate '{mate_name}'.")
        if not bool(assy.Extension.DeleteSelection2(0)):
            raise RuntimeError(f"SolidWorks could not delete mate '{mate_name}'.")
        remaining = {
            str(feature.Name).casefold()
            for feature in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature, "GetTypeName2", "")).startswith("Mate")
        }
        if mate_name.casefold() in remaining:
            raise RuntimeError(f"Mate '{mate_name}' remains in the feature tree after deletion.")
        _redraw_document(assy)
        return {"deleted": mate_name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_advanced_mate(
    mate_type: str,
    x1: float, y1: float, z1: float,
    x2: float, y2: float, z2: float,
    value: float = 0,
    gear_ratio_numerator: float = 1.0,
    gear_ratio_denominator: float = 1.0,
    reverse: bool = False,
    unit: Optional[str] = None,
) -> dict:
    """Add an advanced mate between two entities selected by point.

    Goes beyond basic mates to include distance, angle, width, and symmetry
    constraints needed to precisely position parts on platforms and tanks.

    mate_type: 'distance', 'angle', 'width', 'symmetric', 'lock', 'gear', 'tangent',
               'coincident', 'concentric', 'parallel', or 'perpendicular'.
    x1/y1/z1: a point on the first face/edge.
    x2/y2/z2: a point on the second face/edge.
    value: distance (in current unit) for 'distance', or angle in degrees for 'angle'.
    gear_ratio_numerator/gear_ratio_denominator: positive gear ratio for ``gear``.
    reverse: reverse the driven direction for a gear mate."""

    def _impl():
        assy = _active_assembly()

        types = {
            "coincident": 0, "concentric": 1, "perpendicular": 2, "parallel": 3,
            "tangent": 4, "distance": 5, "angle": 6, "symmetric": 8,
            "gear": 10, "width": 11, "lock": 16,
        }
        mate_key = mate_type.lower().strip()
        code = types.get(mate_key)
        if code is None:
            raise ValueError(f"Unknown mate_type '{mate_type}'. Use: {', '.join(types)}")
        if mate_key == "gear" and (gear_ratio_numerator <= 0 or gear_ratio_denominator <= 0):
            raise ValueError("Gear ratio numerator and denominator must be positive.")

        x1_m, y1_m, z1_m = to_meters(x1, unit), to_meters(y1, unit), to_meters(z1, unit)
        x2_m, y2_m, z2_m = to_meters(x2, unit), to_meters(y2, unit), to_meters(z2, unit)

        assy.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not assy.Extension.SelectByID2("", "FACE", x1_m, y1_m, z1_m, False, 1, empty, 0):
            raise RuntimeError(f"No entity found at point 1 ({x1}, {y1}, {z1}).")
        if not assy.Extension.SelectByID2("", "FACE", x2_m, y2_m, z2_m, True, 1, empty, 0):
            raise RuntimeError(f"No entity found at point 2 ({x2}, {y2}, {z2}).")

        if mate_key == "angle":
            mate_value = math.radians(value)
        else:
            mate_value = to_meters(value, unit)

        mate_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        mate = assy.AddMate5(
            code, 0, bool(reverse) if mate_key == "gear" else False,
            mate_value, mate_value, mate_value,
            float(gear_ratio_numerator) if mate_key == "gear" else 0.0,
            float(gear_ratio_denominator) if mate_key == "gear" else 0.0,
            0.0, 0.0, 0.0,
            False, False, 0, mate_err,
        )

        # swAddMateError_NoError is 1; COM can return an object even on failure,
        # so the status code decides just as it does in add_mate.
        if mate is None or mate_err.value != 1:
            raise RuntimeError(
                f"Advanced mate '{mate_type}' failed (error {mate_err.value}). "
                "Confirm both entities support this mate type."
            )

        return {"mate_type": mate_key, "value": value if code in (5, 6) else None,
                "gear_ratio": [gear_ratio_numerator, gear_ratio_denominator] if mate_key == "gear" else None,
                "reverse": bool(reverse) if mate_key == "gear" else None,
                "error_code": mate_err.value, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_cam_follower_mate(
    cam_ray_origin: list[float],
    cam_ray_direction: list[float],
    follower_ray_origin: list[float],
    follower_ray_direction: list[float],
    ray_radius: float,
    cam_selection_type: int = 2,
    follower_selection_type: int = 3,
    alignment: str = "closest",
    unit: Optional[str] = None,
) -> dict:
    """Create a stable cam-follower mate using ``CreateMateData``/``CreateMate``.

    This deliberately avoids ``AddMate5`` for cam followers.  The SOLIDWORKS
    API requires selection mark 1 for the cam and mark 8 for the follower;
    each ray is ``[x, y, z]`` and directions are unitless vectors.

    ``cam_selection_type`` and ``follower_selection_type`` use SolidWorks
    selection-type IDs accepted by ``SelectByRay`` (the official sample uses
    2 and 3 respectively).  ``alignment`` is ``aligned``, ``anti_aligned``,
    or ``closest``.
    """

    def _impl():
        assy = _active_assembly()
        if len(cam_ray_origin) != 3 or len(cam_ray_direction) != 3:
            raise ValueError("cam_ray_origin and cam_ray_direction must each contain three values.")
        if len(follower_ray_origin) != 3 or len(follower_ray_direction) != 3:
            raise ValueError("follower_ray_origin and follower_ray_direction must each contain three values.")
        if ray_radius <= 0:
            raise ValueError("ray_radius must be positive.")
        if math.isclose(sum(float(value) ** 2 for value in cam_ray_direction), 0.0):
            raise ValueError("cam_ray_direction cannot be zero.")
        if math.isclose(sum(float(value) ** 2 for value in follower_ray_direction), 0.0):
            raise ValueError("follower_ray_direction cannot be zero.")
        alignments = {"aligned": 0, "anti_aligned": 1, "closest": 2}
        alignment_key = alignment.strip().lower()
        if alignment_key not in alignments:
            raise ValueError("alignment must be aligned, anti_aligned, or closest.")

        cam_origin = [to_meters(value, unit) for value in cam_ray_origin]
        follower_origin = [to_meters(value, unit) for value in follower_ray_origin]
        radius_m = to_meters(ray_radius, unit)
        before = {
            str(feature.Name)
            for feature in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature, "GetTypeName2", "")).startswith("MateCam")
        }
        assy.ClearSelection2(True)
        selected_cam = assy.Extension.SelectByRay(
            *cam_origin, *[float(value) for value in cam_ray_direction], radius_m,
            int(cam_selection_type), True, 1, 0,
        )
        selected_follower = assy.Extension.SelectByRay(
            *follower_origin, *[float(value) for value in follower_ray_direction], radius_m,
            int(follower_selection_type), True, 8, 0,
        )
        selected_count = int(assy.SelectionManager.GetSelectedObjectCount2(-1))
        if not selected_cam or not selected_follower or selected_count != 2:
            assy.ClearSelection2(True)
            raise RuntimeError(
                "Cam-follower selection failed; confirm both rays intersect the intended cam and follower entities."
            )

        mate_data = assy.CreateMateData(9)  # swMateCAMFOLLOWER
        mate_data.MateAlignment = alignments[alignment_key]
        # Python 3.14's dynamic COM proxy exposes CreateMate as a property.
        # Invoke the typed DISPIDs directly, matching the registered 2025 API.
        raw_feature = assy._oleobj_.InvokeTypes(195, 0, 1, (9, 0), ((9, 1),), mate_data)
        feature = win32com.client.Dispatch(raw_feature) if raw_feature is not None else None
        assy.ClearSelection2(True)
        if feature is None:
            status = getattr(mate_data, "ErrorStatus", "unknown")
            raise RuntimeError(f"SolidWorks did not create the cam-follower mate (status {status}).")
        rebuilt = bool(assy.ForceRebuild3(False))
        after = [
            feature_item for feature_item in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature_item, "GetTypeName2", "")).startswith("MateCam")
            and str(feature_item.Name) not in before
        ]
        if not after:
            raise RuntimeError("Cam-follower API returned a feature but no MateCam feature was added to the tree.")
        _redraw_document(assy)
        return {
            "feature": str(after[-1].Name),
            "feature_type": str(after[-1].GetTypeName2),
            "alignment": alignment_key,
            "rebuilt": rebuilt,
            "selection_marks": {"cam": 1, "follower": 8},
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_screw_mate(
    first_ray_origin: list[float],
    first_ray_direction: list[float],
    second_ray_origin: list[float],
    second_ray_direction: list[float],
    ray_radius: float,
    revolution_value: float,
    revolution_type: str = "distance_per_revolution",
    reverse: bool = False,
    first_selection_type: int = 2,
    second_selection_type: int = 1,
    unit: Optional[str] = None,
) -> dict:
    """Create a screw mate from two cylindrical entities selected by rays.

    Uses the documented ``ScrewMateFeatureData`` path instead of ``AddMate5``.
    The two ray origins and ``ray_radius`` use ``unit``; directions are unitless.
    ``revolution_type`` is ``distance_per_revolution`` or
    ``revolutions_per_unit_length``. Distance values are converted by the MCP
    to meters; revolutions-per-unit-length is a scalar.
    """

    def _impl():
        assy = _active_assembly()
        ray_data = (first_ray_origin, first_ray_direction, second_ray_origin, second_ray_direction)
        if any(len(values) != 3 for values in ray_data):
            raise ValueError("Each screw-mate ray origin and direction must contain three values.")
        if ray_radius <= 0 or revolution_value <= 0:
            raise ValueError("ray_radius and revolution_value must be positive.")
        if any(math.isclose(sum(float(value) ** 2 for value in direction), 0.0)
               for direction in (first_ray_direction, second_ray_direction)):
            raise ValueError("Screw-mate ray directions cannot be zero.")
        revolution_types = {"distance_per_revolution": 0, "revolutions_per_unit_length": 1}
        revolution_key = revolution_type.strip().lower()
        if revolution_key not in revolution_types:
            raise ValueError("revolution_type must be distance_per_revolution or revolutions_per_unit_length.")

        first_origin = [to_meters(value, unit) for value in first_ray_origin]
        second_origin = [to_meters(value, unit) for value in second_ray_origin]
        radius_m = to_meters(ray_radius, unit)
        # Distance values are stored by the API in meters; revolutions/length is unitless.
        value = to_meters(revolution_value, unit) if revolution_key == "distance_per_revolution" else float(revolution_value)
        before = {
            str(feature.Name)
            for feature in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature, "GetTypeName2", "")) == "MateScrew"
        }
        assy.ClearSelection2(True)
        selected_first = assy.Extension.SelectByRay(
            *first_origin, *[float(value) for value in first_ray_direction], radius_m,
            int(first_selection_type), True, 1, 0,
        )
        selected_second = assy.Extension.SelectByRay(
            *second_origin, *[float(value) for value in second_ray_direction], radius_m,
            int(second_selection_type), True, 1, 0,
        )
        if not selected_first or not selected_second or int(assy.SelectionManager.GetSelectedObjectCount2(-1)) != 2:
            assy.ClearSelection2(True)
            raise RuntimeError("Screw-mate selection failed; both rays must hit cylindrical mate entities.")

        mate_data = assy.CreateMateData(17)  # swMateSCREW
        mate_data.RevolutionType = revolution_types[revolution_key]
        mate_data.RevolutionVal = value
        mate_data.Reverse = bool(reverse)
        raw_feature = assy._oleobj_.InvokeTypes(195, 0, 1, (9, 0), ((9, 1),), mate_data)
        feature = win32com.client.Dispatch(raw_feature) if raw_feature is not None else None
        assy.ClearSelection2(True)
        if feature is None:
            raise RuntimeError("SolidWorks did not create the screw mate.")
        rebuilt = bool(assy.ForceRebuild3(False))
        after = [
            item for item in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(item, "GetTypeName2", "")) == "MateScrew" and str(item.Name) not in before
        ]
        if not after:
            raise RuntimeError("Screw API returned a feature but no MateScrew feature was added to the tree.")
        _redraw_document(assy)
        return {
            "feature": str(after[-1].Name), "feature_type": str(after[-1].GetTypeName2),
            "revolution_type": revolution_key, "revolution_value": revolution_value,
            "reverse": bool(reverse), "rebuilt": rebuilt, "selection_marks": [1, 1],
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_rack_pinion_mate(
    rack_ray_origin: list[float],
    rack_ray_direction: list[float],
    pinion_ray_origin: list[float],
    pinion_ray_direction: list[float],
    ray_radius: float,
    diameter_value: float,
    diameter_type: str = "pinion_pitch_diameter",
    reverse: bool = False,
    rack_selection_type: int = 1,
    pinion_selection_type: int = 2,
    unit: Optional[str] = None,
) -> dict:
    """Create a rack-and-pinion mate from a rack edge and a pinion cylinder.

    This uses ``RackPinionMateFeatureData`` with the API-required selection
    marks 64 (rack) and 128 (pinion), then verifies ``MateRackPinionDim``.
    ``diameter_type`` is ``pinion_pitch_diameter`` or
    ``rack_travel_per_revolution``.
    """

    def _impl():
        assy = _active_assembly()
        ray_data = (rack_ray_origin, rack_ray_direction, pinion_ray_origin, pinion_ray_direction)
        if any(len(values) != 3 for values in ray_data):
            raise ValueError("Each rack-pinion ray origin and direction must contain three values.")
        if ray_radius <= 0 or diameter_value <= 0:
            raise ValueError("ray_radius and diameter_value must be positive.")
        if any(math.isclose(sum(float(value) ** 2 for value in direction), 0.0)
               for direction in (rack_ray_direction, pinion_ray_direction)):
            raise ValueError("Rack-pinion ray directions cannot be zero.")
        diameter_types = {"pinion_pitch_diameter": 0, "rack_travel_per_revolution": 1}
        diameter_key = diameter_type.strip().lower()
        if diameter_key not in diameter_types:
            raise ValueError("diameter_type must be pinion_pitch_diameter or rack_travel_per_revolution.")

        rack_origin = [to_meters(value, unit) for value in rack_ray_origin]
        pinion_origin = [to_meters(value, unit) for value in pinion_ray_origin]
        radius_m = to_meters(ray_radius, unit)
        before = {
            str(feature.Name)
            for feature in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(feature, "GetTypeName2", "")) == "MateRackPinionDim"
        }
        assy.ClearSelection2(True)
        selected_rack = assy.Extension.SelectByRay(
            *rack_origin, *[float(value) for value in rack_ray_direction], radius_m,
            int(rack_selection_type), True, 64, 0,
        )
        selected_pinion = assy.Extension.SelectByRay(
            *pinion_origin, *[float(value) for value in pinion_ray_direction], radius_m,
            int(pinion_selection_type), True, 128, 0,
        )
        if not selected_rack or not selected_pinion or int(assy.SelectionManager.GetSelectedObjectCount2(-1)) != 2:
            assy.ClearSelection2(True)
            raise RuntimeError("Rack-pinion selection failed; select a linear rack edge and a cylindrical pinion entity.")

        mate_data = assy.CreateMateData(13)  # swMateRACKPINION
        mate_data.DiameterType = diameter_types[diameter_key]
        mate_data.DiameterVal = to_meters(diameter_value, unit)
        mate_data.Reverse = bool(reverse)
        raw_feature = assy._oleobj_.InvokeTypes(195, 0, 1, (9, 0), ((9, 1),), mate_data)
        feature = win32com.client.Dispatch(raw_feature) if raw_feature is not None else None
        assy.ClearSelection2(True)
        if feature is None:
            raise RuntimeError("SolidWorks did not create the rack-and-pinion mate.")
        rebuilt = bool(assy.ForceRebuild3(False))
        after = [
            item for item in (assy.FeatureManager.GetFeatures(False) or ())
            if str(getattr(item, "GetTypeName2", "")) == "MateRackPinionDim" and str(item.Name) not in before
        ]
        if not after:
            raise RuntimeError("Rack-pinion API returned a feature but no MateRackPinionDim feature was added to the tree.")
        _redraw_document(assy)
        return {
            "feature": str(after[-1].Name), "feature_type": str(after[-1].GetTypeName2),
            "diameter_type": diameter_key, "diameter_value": diameter_value,
            "reverse": bool(reverse), "rebuilt": rebuilt, "selection_marks": {"rack": 64, "pinion": 128},
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def interference_check() -> dict:
    """Check the active assembly for interferences (parts overlapping in space).

    Reports every pair of components whose solid volumes intersect — a critical
    check before fabrication, since real steel can't occupy the same space twice."""

    def _impl():
        assy = _active_assembly()
        assy.ClearSelection2(True)

        # IInterferenceDetectionMgr calculates actual interference pairs. In
        # contrast, ToolsCheckInterference2 is void and only fills output arrays,
        # so treating its return as a count silently reports false negatives.
        manager = None
        try:
            manager = win32com.client.Dispatch(
                assy.InterferenceDetectionManager,
                "IInterferenceDetectionMgr",
                "{EAE282BD-588A-4C1B-AD99-5FE6081C4585}",
            )
            manager.TreatCoincidenceAsInterference = False
            # GetInterferenceCount is a zero-argument COM member; the dynamic
            # IDispatch proxy sometimes exposes it as an already-evaluated int
            # property rather than a callable method (same quirk documented
            # elsewhere in this file for GetModelViewNames, RevisionNumber,
            # etc.) -- confirmed live, 2026-10-05: calling it unconditionally
            # raised "'int' object is not callable". Evaluate conditionally.
            raw_count = manager.GetInterferenceCount
            if callable(raw_count):
                raw_count = raw_count()
            count = int(raw_count)
        except Exception as e:
            raise RuntimeError(
                f"Interference check is not available through this SolidWorks API build ({e})."
            ) from e
        finally:
            if manager is not None:
                try:
                    manager.Done()
                except Exception:
                    pass

        return {
            "interferences": count,
            "clear": count == 0,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_assembly_pattern(
    component_name: str,
    pattern_type: str = "linear",
    direction: str = "x",
    count: int = 3,
    spacing: float = 100,
    axis_face_x: float = 0, axis_face_y: float = 0, axis_face_z: float = 0,
    angle: float = 90,
    unit: Optional[str] = None,
) -> dict:
    """Pattern a component within the active assembly.

    Repeats a component in a linear or circular arrangement — e.g. the balusters
    of a guardrail, or bolts around a flange.

    component_name: the component to repeat (from list_components).
    pattern_type: 'linear' or 'circular'.
    direction: 'x', 'y', or 'z' (for linear patterns).
    count: total number of instances (including the original).
    spacing: distance between instances (linear).
    axis_face_x/y/z: point on a cylindrical face/axis to rotate around (circular).
    angle: angle between instances in degrees (circular)."""

    def _impl():
        if count < 2:
            raise ValueError(f"count must be >= 2, got {count}.")
        assy = _active_assembly()

        if pattern_type.lower() == "linear":
            spacing_m = to_meters(spacing, unit)
            axis = direction.lower()
            if axis not in {"x", "y", "z"}:
                raise ValueError(f"direction must be 'x', 'y', or 'z', got '{direction}'.")

            # Component patterns require the seed with mark 1 and the direction
            # reference with mark 2. A reusable assembly reference axis gives the
            # public x/y/z contract a deterministic SolidWorks selection target.
            axis_name = _ensure_pattern_axis(assy, axis)
            component = _find_component(assy, component_name)
            if component is None:
                raise RuntimeError(f"Component '{component_name}' not found in the assembly.")
            assy.ClearSelection2(True)
            if not component.SelectByMark(False, 1):
                raise RuntimeError(f"Could not select component '{component_name}' for patterning.")
            if not assy.Extension.SelectByID2(
                axis_name, "AXIS", 0, 0, 0, True, 2,
                win32com.client.VARIANT(pythoncom.VT_DISPATCH, None), 0,
            ):
                raise RuntimeError(f"Could not select the {axis} direction reference for patterning.")

            try:
                feat = assy.FeatureManager.FeatureLinearPattern4(
                    count, spacing_m, 1, 0.0,
                    False, False, "", "", False, False,
                    False, False, False, False, False, False,
                    False, False, 0.0, 0.0,
                )
            except Exception:
                feat = None

            if feat is None:
                raise RuntimeError(
                    "Linear component pattern failed. The linear direction reference "
                    "could not be resolved automatically; select an edge/axis manually."
                )

            return {"pattern": "linear", "component": component_name,
                    "count": count, "spacing": spacing, "unit": unit or _default_unit}

        elif pattern_type.lower() == "circular":
            if angle <= 0 or angle > 360:
                raise ValueError(f"angle must be between 0 and 360, got {angle}.")
            fx, fy, fz = to_meters(axis_face_x, unit), to_meters(axis_face_y, unit), to_meters(axis_face_z, unit)
            angle_rad = math.radians(angle)
            component = _find_component(assy, component_name)
            if component is None:
                raise RuntimeError(f"Component '{component_name}' not found in the assembly.")
            assy.ClearSelection2(True)
            if not component.SelectByMark(False, 1):
                raise RuntimeError(f"Could not select component '{component_name}' for patterning.")
            if not assy.Extension.SelectByID2(
                "", "FACE", fx, fy, fz, True, 2,
                win32com.client.VARIANT(pythoncom.VT_DISPATCH, None), 0,
            ):
                raise RuntimeError("Circular component pattern could not select the requested axis face.")

            try:
                feat = assy.FeatureManager.FeatureCircularPattern5(
                    count, angle_rad, False, "", False, True, False,
                    False, False, False, 1, 0.0, "", False,
                )
            except Exception:
                feat = None

            if feat is None:
                raise RuntimeError(
                    "Circular component pattern failed. Provide a valid axis face point."
                )

            return {"pattern": "circular", "component": component_name,
                    "count": count, "angle": angle, "unit": unit or _default_unit}
        else:
            raise ValueError(f"pattern_type must be 'linear' or 'circular', got '{pattern_type}'.")

    return await _run(_impl)


# ===========================================================================
# Material & Appearance tools
# ===========================================================================

# SolidWorks stores a material library as XML with physical properties under
# <physicalproperties>, keyed by the finite-element short names rather than by
# readable labels: DENS is density in kg/m3, SIGYLD yield strength in Pa.
_MATERIAL_PROPERTY_KEYS = {
    "DENS": ("density_kg_m3", 1.0),
    "EX": ("elastic_modulus_pa", 1.0),
    "NUXY": ("poisson_ratio", 1.0),
    "SIGYLD": ("yield_strength_pa", 1.0),
    "SIGXT": ("tensile_strength_pa", 1.0),
    "ALPX": ("thermal_expansion_per_k", 1.0),
    "KX": ("thermal_conductivity_w_mk", 1.0),
    "C": ("specific_heat_j_kgk", 1.0),
}

_material_library_cache: dict = {}


def _read_material_library(database_path: str) -> dict:
    """Parse a .sldmat into {material name: {property: value}}.

    Reading the library directly is the only way this server can state a
    density it has actually verified: IPartDoc.SetMaterialPropertyName2 is
    silently ineffective on some builds (see set_material), and a mass that
    is wrong without an error is the worst outcome for a BOM.
    """
    cached = _material_library_cache.get(database_path)
    if cached is not None:
        return cached
    import xml.etree.ElementTree as ElementTree

    try:
        root = ElementTree.parse(database_path).getroot()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read the material library {database_path}: {exc}"
        ) from exc

    materials: dict = {}
    for element in root.iter("material"):
        name = element.get("name")
        if not name:
            continue
        entry: dict = {"name": name, "matid": element.get("matid")}
        for properties in element.iter("physicalproperties"):
            for child in properties:
                mapped = _MATERIAL_PROPERTY_KEYS.get(child.tag.upper())
                if mapped is None:
                    continue
                key, factor = mapped
                try:
                    entry[key] = float(child.get("value")) * factor
                except (TypeError, ValueError):
                    continue
        materials[name] = entry
    _material_library_cache[database_path] = materials
    return materials


def _library_density(material: str, database_path: str):
    """Density of one material straight from the library file, or None."""
    try:
        entry = _read_material_library(database_path).get(material)
    except Exception:
        return None
    return (entry or {}).get("density_kg_m3")


def _document_density(doc):
    """Density (kg/m3) the document is currently computing mass with.

    Derived from mass/volume rather than read from a property, because that
    is the number the BOM will actually use and it cannot be faked by a
    material name that was stored without taking effect.
    """
    try:
        status = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        props = doc.Extension.GetMassProperties2(1, status, False)
        if props and len(props) > 5 and props[3]:
            return props[5] / props[3]
    except Exception:
        log.debug("Could not compute the document density", exc_info=True)
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def lookup_material_properties(material: str = "",
                                     database: str = "SOLIDWORKS Materials",
                                     search: str = "") -> dict:
    """Read a material's real physical properties from the SolidWorks library.

    Returns density (kg/m3), elastic modulus, Poisson's ratio, yield and
    tensile strength, and thermal properties, parsed from the .sldmat file
    itself -- so the numbers are the library's, not an estimate.

    material: exact name, e.g. "Alloy Steel", "AISI 304", "6061 Alloy".
    search: instead of an exact name, list every material whose name contains
            this text. Use it to find the exact spelling.
    database: library name ("SOLIDWORKS Materials") or a full .sldmat path.

    Use this to compute a weight that does not depend on the part's assigned
    material -- necessary while set_material cannot apply a density on this
    SolidWorks build -- and for hand calculations (yield strength for a
    stress check, density for a weight estimate). Read-only; it touches no
    document."""

    def _impl():
        database_path = _resolve_material_database_path(database)
        if not os.path.isfile(database_path):
            raise RuntimeError(
                f"Could not resolve material database '{database}' to a "
                f".sldmat file (tried: {database_path})."
            )
        materials = _read_material_library(database_path)
        if search:
            needle = search.lower()
            matches = sorted(n for n in materials if needle in n.lower())
            return {"database": database_path, "search": search,
                    "match_count": len(matches),
                    "matches": [materials[n] for n in matches[:50]],
                    "truncated": len(matches) > 50}
        if not material:
            return {"database": database_path,
                    "material_count": len(materials),
                    "names": sorted(materials)[:200],
                    "hint": "pass material= for one material, or search= to filter"}
        entry = materials.get(material)
        if entry is None:
            needle = material.lower()
            close = sorted(n for n in materials if needle in n.lower())[:10]
            raise ValueError(
                f"No material named '{material}' in {os.path.basename(database_path)}. "
                + (f"Did you mean: {', '.join(close)}?" if close
                   else "Use search= to list candidates.")
            )
        return {"database": database_path, **entry}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_material(
    material: str = "AISI 1020",
    database: str = "SOLIDWORKS Materials",
) -> dict:
    """Assign a material to the active part.

    Materials drive mass calculations and appear in the bill of materials.
    Common names: 'AISI 1020', 'AISI 304', 'ASTM A36 Steel', 'Alloy Steel',
    '6061 Alloy', 'Plain Carbon Steel', 'Cast Alloy Steel'.

    material: the material name exactly as it appears in the SolidWorks library.
    database: material database name (usually 'SOLIDWORKS Materials')."""

    def _impl():
        doc = _active_doc()
        if _doc_type(doc) != 1:
            raise RuntimeError("Material can only be set on a part document.")

        resolved_database = _resolve_material_database_path(database)

        # SetMaterialPropertyName2 accepts an unresolved database name (e.g. the
        # "SOLIDWORKS Materials" default) without raising, but silently skips
        # applying real density data when it can't find a matching .sldmat file
        # -- confirmed live, 2026-10-04: mass stayed at water density (1000
        # kg/m3) regardless of which material/density was requested, with no
        # error at any step. _resolve_material_database_path's job is exactly
        # to prevent that; verify it actually found a real file BEFORE calling,
        # since the call itself won't complain either way.
        if not os.path.isfile(resolved_database):
            raise RuntimeError(
                f"Could not resolve material database '{database}' to an actual "
                f".sldmat file on disk (tried: {resolved_database}). "
                f"SetMaterialPropertyName2 will accept this silently but the "
                f"part's density stays at the SolidWorks default (water, 1000 "
                f"kg/m3) -- measure_body's mass_kg would be wrong without "
                f"raising its own error. Pass the full path to the .sldmat file "
                f"explicitly as 'database' instead."
            )

        # Re-confirmed live, 2026-10-05, on a fresh box part: even with a
        # correctly resolved .sldmat path, mass stayed at exactly water
        # density for BOTH "6061 Alloy" and "Cast Alloy Steel" -- the path
        # fix above was not the whole story. Root cause: the material name
        # and database path were swapped. The real signature is
        # SetMaterialPropertyName2(ConfigurationName, SName /* material */,
        # SDatabase /* path */) -- symmetric with the already-working reader
        # _native_material_name's GetMaterialPropertyName2(ConfigurationName,
        # ByRef SDatabase) -> material name as the return value, database as
        # the out-param. The old code passed (resolved_database, material),
        # i.e. asked SolidWorks for a material literally named the .sldmat
        # file path inside a "database" named e.g. "6061 Alloy" -- neither
        # resolves, so the call is a silent no-op with no exception raised.
        # The documented contract for ConfigurationName="" is "apply to every
        # configuration that uses the document material" -- in principle that
        # should cover the active configuration too. Confirmed live,
        # 2026-10-05: it does not. After fixing the argument-order bug above,
        # SetMaterialPropertyName2("", material, resolved_database) still
        # left mass_kg exactly at water density (1000 kg/m3, verified by
        # mass/volume, independent of any read-back call) on a freshly built
        # part. Passing the ACTIVE configuration's real name instead of ""
        # is the next-most-specific thing the API accepts; used by both the
        # write and (where _native_material_name is called with no config
        # override) the read, so they stay symmetric.
        #
        # STILL UNRESOLVED, re-confirmed live 2026-10-05 immediately after
        # this change: using the real active configuration name instead of
        # "" made no difference -- mass_kg stayed at exactly water density
        # for both "Alloy Steel" and "6061 Alloy", on a freshly built part,
        # argument order already correct. The call raises no COM error in
        # either case; whatever is actually wrong is silent on both ends.
        # Do NOT re-try more argument-order or config-name permutations
        # blind -- two independent hypotheses have now been tested and
        # falsified by density math, not just by the read-back check. Next
        # debugging step needs either execute_python (gated off by default)
        # to inspect doc.GetMaterialPropertyName2's actual return value and
        # type directly, or a live SolidWorks UI comparison (apply a material
        # by hand via Edit Material, then read it back through this same
        # reader) to tell whether the WRITE or the READ side is at fault.
        active_config = doc.ConfigurationManager.ActiveConfiguration
        config_name = active_config.Name if active_config is not None else ""

        try:
            part = doc  # IPartDoc
            part.SetMaterialPropertyName2(config_name, material, resolved_database)
        except Exception:
            try:
                part.SetMaterialPropertyName(material, resolved_database)
            except Exception:
                raise RuntimeError(
                    f"Failed to set material '{material}'. Check that the name matches "
                    "the SolidWorks material library exactly (case-sensitive)."
                )

        # Verify by DENSITY, which is the number the BOM uses and the only
        # witness that cannot be faked. The previous check compared the
        # material NAME read back from GetMaterialPropertyName2; that is
        # weaker, and on this build the name reads back as "" even when the
        # call reported no error, so the message blamed the wrong thing.
        expected_density = _library_density(material, resolved_database)
        actual_density = _document_density(doc)
        applied_name = _native_material_name(doc, config_name)

        result = {
            "material": material,
            "database": resolved_database,
            "expected_density_kg_m3": expected_density,
            "document_density_kg_m3": (round(actual_density, 3)
                                       if actual_density else None),
            "material_name_read_back": applied_name,
        }

        if expected_density and actual_density:
            if abs(actual_density - expected_density) <= max(1.0, expected_density * 0.01):
                result["verified"] = "density matches the material library"
                return result
            # Exactly water means SolidWorks never picked up a density at all,
            # which is the documented failure mode here -- distinguish it from
            # "some other material is applied" so the message is actionable.
            looks_like_water = abs(actual_density - 1000.0) < 1.0
            raise RuntimeError(
                f"set_material did not take effect. SolidWorks raised no error, "
                f"but the part still computes mass at "
                f"{actual_density:.1f} kg/m3"
                + (" (water, i.e. no material density at all)" if looks_like_water else "")
                + f", while '{material}' is {expected_density:.1f} kg/m3 in "
                f"{os.path.basename(resolved_database)}.\n\n"
                f"IPartDoc.SetMaterialPropertyName2 has been confirmed silently "
                f"ineffective on this SolidWorks build: five documented argument "
                f"orders were tried live and none changed the density. Until that "
                f"is resolved:\n"
                f"  - apply the material by hand in SolidWorks (right-click "
                f"Material in the feature tree, Edit Material), or\n"
                f"  - compute weight as volume x density, taking the density "
                f"from lookup_material_properties('{material}').\n"
                f"Do NOT trust measure_body's mass_kg on a part whose material "
                f"was set through this tool."
            )

        # Without a library density there is nothing to verify against, so say
        # so rather than implying success.
        if expected_density is None:
            result["verified"] = (
                f"could not find '{material}' in "
                f"{os.path.basename(resolved_database)} to check its density; "
                f"the assignment was not verified"
            )
        else:
            result["verified"] = (
                "could not read the document's density; the assignment was not "
                "verified"
            )
        result["warning"] = (
            "unverified material assignment -- check mass_kg against "
            "lookup_material_properties before using it in a BOM"
        )
        return result

    return await _run(_impl)


def _resolve_material_database_path(database: str) -> str:
    """Resolve a short material-database name (e.g. the default "SOLIDWORKS
    Materials") to the full .sldmat file path SetMaterialPropertyName2 needs
    to actually apply density data, not just accept the call without error.

    Confirmed live, 2026-10-04: passing the short display name "SOLIDWORKS
    Materials" raises nothing and set_material reports success, but the density
    used for mass/volume calculations stays at the SolidWorks default (water)
    regardless of which material was requested -- measure_body returned the
    identical mass_kg for ASTM A36 Steel (~7850 kg/m3) and 6061 Alloy (~2700
    kg/m3) on the same body. The real file lives at
    "<install dir>/lang/<language>/sldmaterials/<name>.sldmat" (filename
    lowercase, e.g. "solidworks materials.sldmat") -- the same per-language
    layout create_weldment_profile's _weldment_profile_roots already navigates
    for weldment profile files, just under sldmaterials/ instead.
    """
    if os.path.isfile(database):
        return database  # caller already passed a real path

    exe = _find_solidworks_exe()
    install_dir = os.path.dirname(exe) if exe else None
    if not install_dir:
        return database  # no install dir found -- let SetMaterialPropertyName2 fail on its own

    lang_root = os.path.join(install_dir, "lang")
    target_name = f"{database.lower()}.sldmat"
    if os.path.isdir(lang_root):
        for language in os.listdir(lang_root):
            candidate = os.path.join(lang_root, language, "sldmaterials", target_name)
            if os.path.isfile(candidate):
                return candidate
        # Fall back to a full walk in case a given install nests sldmaterials/
        # differently than lang/<language>/sldmaterials/.
        for root, _dirs, files in os.walk(lang_root):
            for name in files:
                if name.lower() == target_name:
                    return os.path.join(root, name)

    return database  # nothing found -- let SetMaterialPropertyName2 fail on its own


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_appearance(
    red: int = 255, green: int = 255, blue: int = 0,
    target: str = "body",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Set the color/appearance of the active part or a specific face.

    red/green/blue: color components (0–255). Default is yellow (typical for
                    structural steel).
    target: 'body' (whole part) or 'face' (single face at face_x/y/z).
    face_x/y/z: face location when target='face'."""

    def _impl():
        if not all(0 <= c <= 255 for c in (red, green, blue)):
            raise ValueError("RGB components must be between 0 and 255.")
        doc = _active_doc()

        if target.lower() == "face":
            doc.ClearSelection2(True)
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")

        # The first three values are normalized RGB.  The remaining values
        # describe ambient, diffuse, specular, shininess, transparency, and
        # emission respectively (IModelDocExtension/IFace2 contract).
        #
        # ambient=0.2/specular=0.5/shininess=0.3 (the values this used to
        # hardcode) render near-achromatic colors (gray, light silver --
        # R/G/B close to each other) as solid black in the viewport: confirmed
        # live, 2026-10-05, on create_automotive_piston's default body color
        # (185, 190, 200) and again with plain (128, 128, 128) and
        # (220, 225, 232). A saturated color like (255, 0, 0) or (210, 190,
        # 150) rendered correctly with the SAME low reflection values, and the
        # richer set_appearance_properties tool's proven-good preset (ambient
        # 0.45, specular 0.92, shininess 0.82 -- see RELATORIO_TESTES.md's
        # "light silver RGB 220/225/232" entry) fixed the identical color
        # instantly. Low ambient/specular starves a desaturated surface of
        # enough light response to stay visible; a saturated hue has enough
        # perceptual contrast to survive the same dim values, which is why
        # only grayish colors looked broken. Raised to match what is already
        # proven to work.
        material_values = [
            red / 255.0,
            green / 255.0,
            blue / 255.0,
            0.45,
            0.9,
            0.85,
            0.7,
            0.0,
            0.0,
        ]
        # SolidWorks requires a SAFEARRAY of doubles here. Passing a Python
        # list creates a SAFEARRAY of VARIANTs and silently corrupts the color
        # channels on this COM proxy.
        material_values = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, material_values,
        )
        this_configuration = 1  # swThisConfiguration

        applied = False
        try:
            if target.lower() == "face":
                sel = doc.SelectionManager.GetSelectedObject6(1, -1)
                face = win32com.client.Dispatch(
                    sel,
                    "IFace2",
                    "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
                )
                face.SetMaterialPropertyValues2(
                    material_values, this_configuration, None,
                )
                applied = True
            else:
                extension = win32com.client.Dispatch(
                    doc.Extension,
                    "IModelDocExtension",
                    "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}",
                )
                extension.SetMaterialPropertyValues(
                    material_values, this_configuration, None,
                )
                applied = True
        except Exception as exc:
            raise RuntimeError(f"Failed to set {target.lower()} appearance: {exc}") from exc

        try:
            redraw = doc.GraphicsRedraw2
            if callable(redraw):
                redraw()
        except Exception:
            pass

        if not applied:
            raise RuntimeError("Failed to set appearance/color.")

        return {"rgb": [red, green, blue], "target": target}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_appearance_properties() -> dict:
    """Read the active document's RGB, reflection, transparency, and emission.

    The result follows the SolidWorks material-property channel order used by
    ``IModelDocExtension.GetMaterialPropertyValues``.  It is useful to inspect
    an existing finish before changing it.
    """

    def _impl():
        doc = _active_doc()
        extension = doc.Extension
        has_values = getattr(extension, "HasMaterialPropertyValues", False)
        has_values = bool(has_values() if callable(has_values) else has_values)
        raw = extension.GetMaterialPropertyValues(1, None) if has_values else None
        values = list(raw) if raw else None
        if values is not None and len(values) != 9:
            raise RuntimeError(
                f"SolidWorks returned {len(values)} appearance channels; expected 9."
            )
        return {
            "has_appearance": has_values,
            "channels": None if values is None else {
                "rgb": [round(values[0] * 255), round(values[1] * 255), round(values[2] * 255)],
                "ambient": values[3],
                "diffuse": values[4],
                "specular": values[5],
                "shininess": values[6],
                "transparency": values[7],
                "emission": values[8],
            },
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_appearance_properties(
    red: int = 210, green: int = 215, blue: int = 220,
    ambient: float = 0.35, diffuse: float = 0.85,
    specular: float = 0.70, shininess: float = 0.70,
    transparency: float = 0.0, emission: float = 0.0,
    target: str = "body",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Set color plus all native SolidWorks reflection/opacity channels.

    ``specular`` and ``shininess`` control reflected highlights; ``diffuse``
    controls base-light response; ``transparency`` and ``emission`` range from
    0 to 1. Use target ``face`` with a point on a face for a local finish.
    """

    def _impl():
        if not all(isinstance(c, int) and 0 <= c <= 255 for c in (red, green, blue)):
            raise ValueError("RGB components must be integers between 0 and 255.")
        channels = (ambient, diffuse, specular, shininess, transparency, emission)
        if not all(0.0 <= float(value) <= 1.0 for value in channels):
            raise ValueError("ambient, diffuse, specular, shininess, transparency, and emission must be 0..1.")
        target_key = target.strip().lower()
        if target_key not in {"body", "face"}:
            raise ValueError("target must be 'body' or 'face'.")

        doc = _active_doc()
        values = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [red / 255.0, green / 255.0, blue / 255.0,
             float(ambient), float(diffuse), float(specular), float(shininess),
             float(transparency), float(emission)],
        )
        if target_key == "face":
            doc.ClearSelection2(True)
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")
            selected = doc.SelectionManager.GetSelectedObject6(1, -1)
            face = win32com.client.Dispatch(
                selected, "IFace2", "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
            )
            face.SetMaterialPropertyValues2(values, 1, None)
        else:
            extension = win32com.client.Dispatch(
                doc.Extension, "IModelDocExtension", "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}",
            )
            extension.SetMaterialPropertyValues(values, 1, None)
        _redraw_document(doc)
        return {
            "target": target_key,
            "rgb": [red, green, blue],
            "reflection": {"ambient": ambient, "diffuse": diffuse, "specular": specular, "shininess": shininess},
            "transparency": transparency,
            "emission": emission,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def apply_texture(
    texture_path: str,
    scale: float = 1.0,
    angle: float = 0.0,
    blend_with_color: bool = True,
    target: str = "model",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Apply an image texture to the active model or a selected face.

    ``texture_path`` must be a local image path supported by SolidWorks.
    ``scale`` is the texture granularity multiplier (0.001..1000000), and
    ``angle`` rotates it in degrees (0..360). Set target to ``face`` to apply
    only to the face at face_x/face_y/face_z.
    """

    def _impl():
        abs_path = os.path.abspath(texture_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Texture file not found: {abs_path}")
        if not 0.001 <= float(scale) <= 1_000_000:
            raise ValueError("scale must be between 0.001 and 1000000.")
        if not 0.0 <= float(angle) <= 360.0:
            raise ValueError("angle must be between 0 and 360 degrees.")
        target_key = target.strip().lower()
        if target_key not in {"model", "face"}:
            raise ValueError("target must be 'model' or 'face'.")

        doc = _active_doc()
        extension = doc.Extension
        texture = extension.CreateTexture(abs_path, float(scale), float(angle), bool(blend_with_color))
        if texture is None:
            raise RuntimeError("SolidWorks could not create a texture from the supplied file.")
        if target_key == "face":
            doc.ClearSelection2(True)
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")
            selected = doc.SelectionManager.GetSelectedObject6(1, -1)
            face = win32com.client.Dispatch(
                selected, "IFace2", "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
            )
            applied = bool(face.SetTexture("", texture))
        else:
            applied = bool(extension.SetTexture("", texture))
        if not applied:
            raise RuntimeError("SolidWorks rejected the texture assignment.")
        _redraw_document(doc)
        return {"target": target_key, "texture_path": abs_path, "scale": scale, "angle": angle, "blend_with_color": blend_with_color}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def remove_texture(
    target: str = "model",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Remove the native texture from the active model or a selected face."""

    def _impl():
        target_key = target.strip().lower()
        if target_key not in {"model", "face"}:
            raise ValueError("target must be 'model' or 'face'.")
        doc = _active_doc()
        extension = doc.Extension
        if target_key == "face":
            doc.ClearSelection2(True)
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")
            selected = doc.SelectionManager.GetSelectedObject6(1, -1)
            face = win32com.client.Dispatch(
                selected, "IFace2", "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
            )
            removed = bool(face.RemoveTexture(""))
        else:
            removed = bool(extension.RemoveTexture2(""))
        _redraw_document(doc)
        return {"target": target_key, "removed": removed}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def apply_metal_finish(
    finish: str = "chrome",
    target: str = "body",
    face_x: float = 0, face_y: float = 0, face_z: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Apply a native SolidWorks metallic visual finish to a body or face.

    finish: ``chrome``, ``polished_steel``, ``brushed_steel``, ``cast_iron``,
            or ``aluminum``. These presets are embedded material-property
            channels, not external bitmap texture files.
    target: ``body`` (whole part) or ``face`` (face at face_x/y/z).

    Use ``set_material`` as well when mass/BOM material data is required.
    """

    presets = {
        # A bright neutral gray keeps chrome recognizable in the default
        # SolidWorks scene, where a highly reflective dark preset otherwise
        # appears nearly black on faces that do not reflect the light source.
        "chrome": ((225, 230, 235), 0.70, 0.90, 0.95, 0.75),
        "polished_steel": ((140, 150, 160), 0.25, 0.55, 0.85, 0.75),
        "brushed_steel": ((115, 125, 135), 0.25, 0.65, 0.55, 0.45),
        "cast_iron": ((55, 60, 65), 0.25, 0.60, 0.35, 0.32),
        "aluminum": ((175, 180, 185), 0.30, 0.65, 0.70, 0.60),
    }
    finish_key = finish.strip().lower().replace(" ", "_").replace("-", "_")
    target_key = target.strip().lower()
    if finish_key not in presets:
        raise ValueError(f"Unsupported finish '{finish}'. Choose: {', '.join(presets)}.")
    if target_key not in {"body", "face"}:
        raise ValueError("target must be 'body' or 'face'.")

    def _impl():
        (red, green, blue), ambient, diffuse, specular, shininess = presets[finish_key]
        doc = _active_doc()
        if target_key == "face":
            doc.ClearSelection2(True)
            fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
                raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")

        # SolidWorks requires a SAFEARRAY of doubles. Native material-property
        # channels travel with the saved file and avoid external texture paths.
        values = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8,
            [red / 255.0, green / 255.0, blue / 255.0,
             ambient, diffuse, specular, shininess, 0.0, 0.0],
        )
        try:
            if target_key == "face":
                selected = doc.SelectionManager.GetSelectedObject6(1, -1)
                face = win32com.client.Dispatch(
                    selected, "IFace2", "{4A8BA4D8-DA25-4B75-8E2D-4922B74D81ED}",
                )
                face.SetMaterialPropertyValues2(values, 1, None)  # swThisConfiguration
            else:
                extension = win32com.client.Dispatch(
                    doc.Extension, "IModelDocExtension", "{99F4D4AF-F268-4EE1-8C55-041F7BECF879}",
                )
                extension.SetMaterialPropertyValues(values, 1, None)  # swThisConfiguration
        except Exception as exc:
            raise RuntimeError(f"Failed to apply {finish_key} finish to {target_key}: {exc}") from exc

        try:
            redraw = doc.GraphicsRedraw2
            if callable(redraw):
                redraw()
        except Exception:
            pass
        return {
            "finish": finish_key,
            "target": target_key,
            "rgb": [red, green, blue],
            "appearance": {
                "ambient": ambient, "diffuse": diffuse,
                "specular": specular, "shininess": shininess,
            },
        }

    return await _run(_impl)


# ===========================================================================
# Native P2M appearance tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_p2m_appearances(scope: str = "this") -> dict:
    """List native P2M/render appearances linked to the active display state.

    scope: ``this`` for the current display state or ``all`` for every display
    state in the active configuration.
    """

    def _impl():
        options = {"this": 1, "all": 2}
        option = options.get(scope.strip().lower())
        if option is None:
            raise ValueError("scope must be 'this' or 'all'.")
        doc = _active_doc()
        materials = doc.Extension.GetRenderMaterials2(option, None) or ()
        appearances = []
        for material in materials:
            appearances.append({
                "path": str(getattr(material, "FileName", "")),
                "ambient": float(getattr(material, "Ambient", 0.0)),
                "diffuse": float(getattr(material, "Diffuse", 0.0)),
                "specular": float(getattr(material, "Specular", 0.0)),
            })
        return {"scope": scope.strip().lower(), "count": len(appearances), "appearances": appearances}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def apply_p2m_appearance(
    appearance_path: str,
    scope: str = "this",
    display_state: str = "",
) -> dict:
    """Apply a native SolidWorks ``.p2m`` appearance to the active model.

    scope: ``this`` applies to the current display state, ``all`` to all
    display states, and ``specific`` requires ``display_state``. The applied
    render material is native SolidWorks data, unlike a bitmap texture.
    """

    def _impl():
        abs_path = os.path.abspath(appearance_path)
        if not os.path.isfile(abs_path) or os.path.splitext(abs_path)[1].lower() != ".p2m":
            raise ValueError("appearance_path must be an existing .p2m appearance file.")
        scope_key = scope.strip().lower()
        options = {"this": 1, "all": 2, "specific": 3}
        option = options.get(scope_key)
        if option is None:
            raise ValueError("scope must be 'this', 'all', or 'specific'.")
        if scope_key == "specific" and not display_state.strip():
            raise ValueError("display_state is required when scope is 'specific'.")

        doc = _active_doc()
        extension = doc.Extension
        names = None
        if scope_key == "specific":
            cfg = doc.ConfigurationManager.ActiveConfiguration
            available = getattr(cfg, "GetDisplayStates", ())
            available = available() if callable(available) else available
            if display_state.casefold() not in {str(item).casefold() for item in (available or ())}:
                raise ValueError(f"Display state '{display_state}' does not exist in '{cfg.Name}'.")
            names = win32com.client.VARIANT(
                pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [display_state],
            )
        material = extension.CreateRenderMaterial(abs_path)
        if material is None:
            raise RuntimeError("SolidWorks could not create the requested P2M render material.")
        if not bool(material.AddEntity(doc)):
            raise RuntimeError("SolidWorks could not attach the P2M appearance to the active model.")
        material_id_1 = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        material_id_2 = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        if not bool(extension.AddDisplayStateSpecificRenderMaterial(
            material, option, names, material_id_1, material_id_2,
        )):
            raise RuntimeError("SolidWorks rejected the P2M appearance for the requested display-state scope.")
        _redraw_document(doc)
        count = int(extension.GetRenderMaterialsCount2(option, names))
        if count < 1:
            raise RuntimeError("SolidWorks did not retain the applied P2M appearance.")
        return {
            "appearance_path": abs_path,
            "scope": scope_key,
            "display_state": display_state if scope_key == "specific" else None,
            "material_ids": [int(material_id_1.value), int(material_id_2.value)],
            "appearance_count": count,
        }

    return await _run(_impl)


# ===========================================================================
# Parametric example tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
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
        for ring_bottom in reversed(ring_bottoms):
            plane = await create_reference_plane("front", ring_bottom, unit=unit)
            await create_sketch(plane["plane"])
            await draw_circle(0, 0, radius + ring_depth, unit)
            await draw_circle(0, 0, radius - ring_depth, unit)
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
                "unit": unit or _default_unit,
            },
            "features": ["extruded skirt/crown", "three ring grooves", "hollow underside", "wrist-pin bore"],
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
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
        await insert_component(part_paths["piston"], unit=unit)
        # The piston itself begins at the Front plane.  Shift the rod group a
        # controlled positive distance toward the viewing side of that plane so its
        # beam is visible below the skirt instead of being occluded by it.
        # The long wrist pin still spans this offset and the piston body.
        await insert_component(part_paths["connecting_rod"], z=front_offset, unit=unit)
        await insert_component(part_paths["wrist_pin"], z=front_offset, unit=unit)
        await insert_component(part_paths["bearing"], z=front_offset, unit=unit)
        await insert_component(part_paths["rod_cap"], x=rod_width + 4, z=front_offset, unit=unit)
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
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
            "joint_centers": {
                "wrist_pin": {"x": 0.0, "y": small_end_y, "z": 0.0},
                "crank_pin": {"x": 0.0, "y": big_end_y, "z": 0.0},
                "unit": unit or _default_unit,
                "contract": "Both rod bores and the transverse wrist pin use these shared centers.",
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
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


# ===========================================================================
# Configuration tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_configuration(
    name: str,
    comment: str = "",
    parent: str = "",
) -> dict:
    """Create a new configuration (design variant) in the active document.

    Configurations let one file hold multiple variants — e.g. M6, M8, M10
    versions of the same bolt, or different lengths of the same beam.

    name: the new configuration's name.
    comment: optional description.
    parent: name of a parent configuration (empty = top-level)."""

    def _impl():
        doc = _active_doc()

        try:
            cfg = doc.ConfigurationManager.AddConfiguration2(
                name, comment, "", 0, parent, "",
            )
        except Exception:
            try:
                cfg = doc.AddConfiguration3(name, comment, "", 0)
            except Exception:
                cfg = None

        if cfg is None and not isinstance(cfg, bool):
            raise RuntimeError(f"Failed to create configuration '{name}'.")

        return {"configuration": name, "comment": comment, "parent": parent or "(top-level)"}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def switch_configuration(name: str) -> dict:
    """Switch the active document to a named configuration.

    name: the configuration to activate (see create_configuration, or the
          ConfigurationManager in SolidWorks)."""

    def _impl():
        doc = _active_doc()

        # Resolve the configuration name case-insensitively (the default config is
        # localized, e.g. 'Default' vs 'Predefinição').
        available = []
        try:
            names = doc.GetConfigurationNames
            available = list(names) if names else []
        except Exception:
            pass

        target = name
        for cfg in available:
            if cfg.lower() == name.lower():
                target = cfg
                break

        doc.ShowConfiguration2(target)
        active_name = ""
        try:
            active = doc.ConfigurationManager.ActiveConfiguration
            active_name = active.Name if not callable(getattr(active, "Name", None)) else active.Name()
        except Exception:
            pass
        if active_name.lower() != target.lower():
            raise RuntimeError(
                f"Could not switch to configuration '{name}'. "
                f"Available configurations: {', '.join(available) if available else '(none)'}."
            )
        return {"active_configuration": active_name, "available": available}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_features() -> dict:
    """List every feature in the active document's feature tree.

    Each entry also reports whether the feature is suppressed and its raw
    SolidWorks error code (0 = healthy), so a caller can tell which features
    a configuration has turned off, or which one is broken, without opening
    the FeatureManager tree in the UI.
    """

    def _impl():
        doc = _active_doc()
        features = []
        for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
            feat = win32com.client.Dispatch(raw_feature)
            try:
                entry = {"name": feat.Name, "type": feat.GetTypeName2}
            except Exception:
                continue
            try:
                suppressed = feat.IsSuppressed
                entry["suppressed"] = bool(suppressed() if callable(suppressed) else suppressed)
            except Exception:
                entry["suppressed"] = None
            try:
                error_code = feat.GetErrorCode
                entry["error_code"] = int(error_code() if callable(error_code) else error_code)
            except Exception:
                entry["error_code"] = None
            features.append(entry)
        return {"count": len(features), "features": features}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_feature(name: str, delete_children: bool = True) -> dict:
    """Delete a named feature from the active part/assembly's FeatureManager tree.

    Works for sketches (2D/3D), weldment members, reference geometry, and any
    other tree-level feature -- unlike components in an assembly
    (delete_component), SolidWorks exposes no separate part-feature-delete API,
    so this is the only way to remove a feature that isn't a mate,
    configuration, or equation.

    delete_children: also delete features that depend on this one (e.g. a weld
    member built from a 3D sketch) instead of leaving SolidWorks to prompt its
    confirm-delete dialog, which would otherwise block the COM call.
    """

    def _impl():
        doc = _active_doc()
        feature_name = name.strip()
        if not feature_name:
            raise ValueError("Feature name cannot be empty.")

        def _all_features():
            return list(doc.FeatureManager.GetFeatures(False) or ())

        def _feature_names():
            return {str(feat.Name) for feat in _all_features()}

        before = _feature_names()
        if feature_name not in before:
            raise ValueError(f"Feature '{feature_name}' does not exist.")

        # SelectByID2 needs a selection-type string that matches what kind of
        # feature this is -- "BODYFEATURE" (the only type this tool used to
        # try) only matches features that contribute a solid body (cuts,
        # bosses, fillets...). A sketch/3D-sketch is type "SKETCH" and a
        # reference plane/axis is "PLANE"/"AXIS"; asking for "BODYFEATURE" on
        # any of those always fails with "could not select feature", even
        # when the feature is perfectly healthy and unsuppressed (this was
        # BUG 4's second half in bug_report_solidworks_mcp.txt: delete_feature
        # could never remove the orphaned sketch "Esboço28").
        sel_type = "BODYFEATURE"
        for feat in _all_features():
            if str(feat.Name) != feature_name:
                continue
            feature_type = feat.GetTypeName2
            if callable(feature_type):
                feature_type = feature_type()
            sel_type = {
                "ProfileFeature": "SKETCH",
                "3DProfileFeature": "SKETCH",
                "RefPlane": "PLANE",
                "RefAxis": "AXIS",
            }.get(feature_type, "BODYFEATURE")
            break

        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        selected = doc.Extension.SelectByID2(feature_name, sel_type, 0, 0, 0, False, 0, empty, 0)
        if not selected and sel_type != "BODYFEATURE":
            # Fall back to the old behavior for any feature type not in the
            # map above, in case a given install/version files it differently.
            selected = doc.Extension.SelectByID2(feature_name, "BODYFEATURE", 0, 0, 0, False, 0, empty, 0)
        if not selected:
            raise RuntimeError(f"SolidWorks could not select feature '{feature_name}'.")

        options = 2 if delete_children else 0  # swDeleteSelectionOptions_Children
        if not bool(doc.Extension.DeleteSelection2(options)):
            raise RuntimeError(f"SolidWorks could not delete feature '{feature_name}'.")

        after = _feature_names()
        if feature_name in after:
            raise RuntimeError(f"Feature '{feature_name}' remains in the feature tree after deletion.")
        also_removed = sorted(before - after - {feature_name})
        _redraw_document(doc)
        return {
            "deleted": feature_name,
            "also_removed": also_removed,
            "remaining_feature_count": len(after),
        }

    return await _run(_impl)


# ===========================================================================
# Custom properties tools
# ===========================================================================

def _native_material_name(doc, config: str = "") -> Optional[str]:
    """Read the material assigned via set_material (IPartDoc.SetMaterialPropertyName2),
    which does NOT show up in CustomPropertyManager -- it's a separate native
    slot. The Database out-param needs a real by-reference VARIANT (confirmed
    live: a plain "" raises DISP_E_TYPEMISMATCH here, same family of binding
    quirk as _open_doc6/_activate_doc3/_save_doc3 above). Returns None for a
    document with no material assigned, or a non-part document.
    """
    if _doc_type(doc) != 1:  # only IPartDoc exposes a single material
        return None
    try:
        database_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
        name = doc.GetMaterialPropertyName2(config, database_out)
        return name or None
    except Exception:
        return None


def _read_custom_properties(cpm) -> dict:
    """Read every property off a CustomPropertyManager into {name: {value, resolved}}.

    Shared by get_custom_properties (active document) and extract_assembly_data
    (one CustomPropertyManager per assembly component) so both read properties
    the same way instead of drifting apart.
    """
    names = cpm.GetNames
    if not names:
        return {}

    props = {}
    for name in names:
        val_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
        resolved_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
        was_resolved = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
        try:
            cpm.Get5(name, False, val_out, resolved_out, was_resolved)
            props[name] = {"value": val_out.value, "resolved": resolved_out.value}
        except Exception:
            try:
                cpm.Get6(name, False, val_out, resolved_out, was_resolved, False)
                props[name] = {"value": val_out.value, "resolved": resolved_out.value}
            except Exception:
                props[name] = {"value": "(could not read)", "resolved": ""}
    return props


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_custom_properties(config: str = "") -> dict:
    """Read all custom properties from the active document.
    config: configuration name (empty string = document-level properties)."""

    def _impl():
        doc = _active_doc()
        cpm = doc.Extension.CustomPropertyManager(config)
        props = _read_custom_properties(cpm)
        return {"config": config or "(document)", "count": len(props), "properties": props}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_custom_property(name: str, value: str, config: str = "") -> dict:
    """Set a custom property on the active document. Creates it if it doesn't exist.
    name: property name (e.g. 'Material', 'Description', 'PartNumber').
    value: property value as text.
    config: configuration name (empty = document-level)."""

    def _impl():
        doc = _active_doc()
        cpm = doc.Extension.CustomPropertyManager(config)
        try:
            ret = cpm.Add3(name, 30, value, 1)
        except Exception:
            ret = -1
        if ret != 0:
            try:
                cpm.Set2(name, value)
            except Exception:
                try:
                    cpm.Set(name, value)
                except Exception:
                    raise RuntimeError(f"Failed to set property '{name}'.")
        return {"name": name, "value": value, "config": config or "(document)"}

    return await _run(_impl)


# ===========================================================================
# Measurement tools
# ===========================================================================

def _measure_model_doc(doc, factor: float) -> dict:
    """Mass/volume/surface/bounding-box of any IModelDoc2, in SI + ``factor``-scaled size.

    Shared by measure_body (active document) and extract_assembly_data (one
    component's model document at a time). Raises if nothing could be measured
    at all, same as measure_body always did -- callers that want a per-item
    soft failure (extract_assembly_data) catch that around each component.
    """
    result = {}

    # CreateMassProperty was effectively dead code: dynamic IDispatch exposes
    # it as an already-evaluated property, so `doc.Extension.CreateMassProperty()`
    # raised DISP_E_MEMBERNOTFOUND every time and the GetMassProperties2
    # fallback below did all the work -- confirmed live, 2026-10-04. Going
    # through _com_member makes the primary path work, which matters because
    # IMassProperty is the only one that reports Density.
    try:
        mp = _com_member(doc.Extension, "CreateMassProperty")
        if mp is not None:
            mp = win32com.client.Dispatch(mp)
            for member, key in (("Mass", "mass_kg"), ("Volume", "volume_m3"),
                                ("SurfaceArea", "surface_area_m2"),
                                ("Density", "density_kg_m3")):
                try:
                    result[key] = _com_member(mp, member)
                except Exception:
                    log.debug("IMassProperty.%s unavailable", member, exc_info=True)
            try:
                com = _com_member(mp, "CenterOfMass")
                if com:
                    result["center_of_mass_m"] = list(com)
            except Exception:
                log.debug("IMassProperty.CenterOfMass unavailable", exc_info=True)
    except Exception:
        log.debug("CreateMassProperty unavailable, using GetMassProperties2",
                  exc_info=True)

    if "mass_kg" not in result or "volume_m3" not in result:
        try:
            status = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            props = doc.Extension.GetMassProperties2(1, status, False)
            if props and len(props) > 5:
                result.setdefault("center_of_mass_m", [props[0], props[1], props[2]])
                result.setdefault("volume_m3", props[3])
                result.setdefault("surface_area_m2", props[4])
                result.setdefault("mass_kg", props[5])
        except Exception:
            log.debug("GetMassProperties2 also failed", exc_info=True)

    # A part with no material computes mass at water density, and a mass that
    # is wrong without an error is exactly what makes a BOM untrustworthy.
    # Say so in the result instead of leaving the caller to notice.
    mass, volume = result.get("mass_kg"), result.get("volume_m3")
    if mass is not None and volume:
        density = mass / volume
        result["density_kg_m3"] = round(density, 3)
        if abs(density - 1000.0) < 1.0:
            result["density_warning"] = (
                "mass is being computed at 1000 kg/m3 (water): this part has no "
                "effective material density, so mass_kg is NOT a real weight. "
                "Assign the material by hand in SolidWorks, or compute the "
                "weight as volume_m3 x density from lookup_material_properties."
            )

    # Union the boxes of EVERY body, not just bodies[0]. A weldment -- the
    # typical structural part -- is multibody by construction, so measuring
    # the first body alone reported the size of one member as the size of the
    # whole frame, and the error was invisible because the number looked
    # plausible.
    bodies = doc.GetBodies2(0, True) or ()
    boxes = []
    for raw_body in bodies:
        try:
            box = win32com.client.Dispatch(raw_body).GetBodyBox()
            if box and len(box) >= 6:
                boxes.append(tuple(float(c) for c in box[:6]))
        except Exception:
            log.debug("GetBodyBox failed for one body; it is left out of the "
                      "bounding box", exc_info=True)
    if boxes:
        low = [min(b[i] for b in boxes) for i in range(3)]
        high = [max(b[i + 3] for b in boxes) for i in range(3)]
        result["body_count"] = len(bodies)
        result["bounding_box"] = {
            "min_m": low,
            "max_m": high,
            "bodies_measured": len(boxes),
            "size": {
                "x": round(abs(high[0] - low[0]) * factor, 4),
                "y": round(abs(high[1] - low[1]) * factor, 4),
                "z": round(abs(high[2] - low[2]) * factor, 4),
                "unit": _default_unit,
            },
        }
        if len(boxes) != len(bodies):
            result["bounding_box"]["warning"] = (
                f"{len(bodies) - len(boxes)} of {len(bodies)} bodies did not "
                f"report a bounding box; the extents below exclude them."
            )

    if not result:
        raise RuntimeError(
            "Could not measure the body. Ensure the document has a solid body "
            "and a material assigned (right-click the material in the feature tree)."
        )
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def measure_body() -> dict:
    """Measure the active document's solid body: mass, volume, surface area,
    center of mass, and bounding box. Returns values in SI units (kg, m, m^2, m^3).
    Requires a part document with at least one solid body and a material assigned."""

    def _impl():
        doc = _active_doc()
        factor = 1.0 / UNIT_TO_METERS.get(_default_unit, 0.001)
        return _measure_model_doc(doc, factor)

    return await _run(_impl)


# ===========================================================================
# Inspection tools
# ===========================================================================
# These read what SolidWorks is showing instead of driving it, so a caller can
# look at the session and resolve real geometry before issuing a command.

# swSelectType_e. Only the values the inspection tools can encounter are named;
# anything else is reported by its raw id so nothing is silently mislabelled.
_SELECTION_TYPE_NAMES = {
    0: "nothing", 1: "edge", 2: "face", 3: "vertex", 4: "datum_plane",
    5: "datum_axis", 6: "datum_point", 7: "ole_item", 8: "attribute",
    9: "sketch", 10: "sketch_segment", 11: "sketch_point", 12: "drawing_view",
    13: "gtol", 14: "dimension", 15: "note", 16: "section_line",
    17: "detail_circle", 18: "section_text", 19: "sheet", 20: "component",
    21: "mate", 22: "body_feature", 23: "ref_curve",
    24: "external_sketch_segment", 25: "external_sketch_point", 26: "helix",
    27: "ref_surface", 28: "center_mark", 29: "in_context_feature",
    30: "mate_group", 31: "break_line", 34: "sketch_text",
    35: "surface_finish_symbol", 36: "datum_tag", 37: "component_pattern",
    38: "weld", 39: "cosmetic_thread", 40: "datum_target",
}

_SURFACE_KINDS = ("plane", "cylinder", "cone", "sphere", "torus")


def _classify_surface(surface) -> str:
    """Name a surface through the ISurface Is<Kind> predicates."""
    for kind in _SURFACE_KINDS:
        try:
            if bool(_com_member(surface, f"Is{kind.capitalize()}")):
                return kind
        except Exception:
            continue
    return "other"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def capture_viewport(output_path: Optional[str] = None) -> Image:
    """Return the SolidWorks viewport exactly as it currently appears on screen.

    Unlike capture_standard_views, this never moves the camera: the current
    orientation, zoom, and selection highlighting are preserved, so it shows
    what the user is actually looking at. Use it to inspect the model visually
    before deciding what to change.
    """

    if output_path and os.path.splitext(output_path)[1].lower() != ".png":
        raise ValueError("output_path must end in .png.")

    def _impl():
        doc = _active_doc()
        keep = bool(output_path)
        if keep:
            path = _resolve_write_path(output_path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        else:
            handle, path = tempfile.mkstemp(prefix="sw_viewport_", suffix=".png")
            os.close(handle)
        try:
            # SaveAs3 to a .png exports the live view and leaves the camera
            # alone. SaveBMP would also work but returns a format the caller
            # cannot view without adding an image-conversion dependency.
            doc.SaveAs3(path, 0, 0)
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                raise RuntimeError(f"SolidWorks did not capture the viewport to '{path}'.")
            # Read on the COM thread so the bytes cannot be replaced by a later
            # capture before they are handed back.
            with open(path, "rb") as image_file:
                return image_file.read()
        finally:
            if not keep:
                with contextlib.suppress(OSError):
                    os.remove(path)

    return Image(data=await _run(_impl), format="png")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_selection(unit: Optional[str] = None) -> dict:
    """Read what is currently selected in SolidWorks.

    Reports each selected entity's type, the point where it was picked, and the
    owning component in an assembly. Use this to act on what the user selected
    on screen instead of guessing coordinates.
    """

    active_unit = (unit or _default_unit).lower()
    if active_unit not in UNIT_TO_METERS:
        raise ValueError(f"Unknown unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")

    def _impl():
        doc = _active_doc()
        factor = 1.0 / UNIT_TO_METERS[active_unit]
        manager = doc.SelectionManager
        # Mark -1 returns every selection regardless of the mark used to make it.
        count = int(manager.GetSelectedObjectCount2(-1))
        selections = []
        for index in range(1, count + 1):
            entry = {"index": index}
            try:
                type_id = int(manager.GetSelectedObjectType3(index, -1))
                entry["type_id"] = type_id
                entry["type"] = _SELECTION_TYPE_NAMES.get(type_id, f"unnamed_type_{type_id}")
            except Exception:
                entry["type_id"] = None
                entry["type"] = "(unavailable)"
            try:
                point = tuple(manager.GetSelectionPoint2(index, -1) or ())
                if len(point) >= 3:
                    entry["pick_point"] = {
                        "x": point[0] * factor,
                        "y": point[1] * factor,
                        "z": point[2] * factor,
                        "unit": active_unit,
                    }
            except Exception:
                pass
            try:
                raw_component = manager.GetSelectedObjectsComponent4(index, -1)
                if raw_component is not None:
                    entry["component"] = str(win32com.client.Dispatch(raw_component).Name2)
            except Exception:
                pass
            selections.append(entry)
        return {"count": count, "unit": active_unit, "selections": selections}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_faces(
    surface_type: Optional[str] = None,
    min_area: float = 0.0,
    max_faces: int = 200,
    unit: Optional[str] = None,
) -> dict:
    """Inventory the solid faces of the active part, each with a usable pick point.

    Every entry carries a ``pick_point`` that provably lies on that face, so it
    can be passed straight to add_mate, add_advanced_mate, or
    create_sketch_on_face rather than guessing where a face sits. Planar faces
    also report their normal and cylindrical faces their radius and axis.

    Those coordinate-based tools resolve a point through SelectByID2, which
    picks from the current camera and therefore cannot reach a face hidden
    behind the model. Call set_view('isometric') first when acting on a
    pick_point: it is the one standard orientation that leaves every face of a
    convex part reachable. This inventory itself is view-independent and always
    reports the full geometry.

    surface_type: keep only 'plane', 'cylinder', 'cone', 'sphere', or 'torus'.
    min_area: drop faces smaller than this area, in ``unit`` squared.
    max_faces: stop after this many matches so large models stay readable.
    """

    def _impl():
        if max_faces < 1:
            raise ValueError("max_faces must be at least 1.")
        if min_area < 0:
            raise ValueError("min_area cannot be negative.")
        wanted = surface_type.strip().lower() if surface_type else None
        if wanted is not None and wanted not in _SURFACE_KINDS:
            raise ValueError(f"surface_type must be one of: {', '.join(_SURFACE_KINDS)}")

        doc = _active_doc()
        active_unit = (unit or _default_unit).lower()
        if active_unit not in UNIT_TO_METERS:
            raise ValueError(f"Unknown unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
        factor = 1.0 / UNIT_TO_METERS[active_unit]
        area_factor = factor ** 2

        try:
            raw_bodies = doc.GetBodies2(0, True) or ()  # 0 = swSolidBody
        except Exception as exc:
            raise RuntimeError(
                "Could not read solid bodies. list_faces inspects a part document; "
                "open the part itself to inventory its faces."
            ) from exc
        if not raw_bodies:
            raise RuntimeError("The active document has no solid body to inspect.")

        faces = []
        inspected = 0
        truncated = False
        for body_index, raw_body in enumerate(raw_bodies):
            if truncated:
                break
            body = win32com.client.Dispatch(raw_body)
            try:
                body_name = str(body.Name)
            except Exception:
                body_name = f"body_{body_index + 1}"
            for face_index, raw_face in enumerate(body.GetFaces() or ()):
                inspected += 1
                face = win32com.client.Dispatch(raw_face)
                try:
                    surface = win32com.client.Dispatch(face.GetSurface)
                    kind = _classify_surface(surface)
                except Exception:
                    surface, kind = None, "other"
                if wanted is not None and kind != wanted:
                    continue

                try:
                    area = float(_com_member(face, "GetArea")) * area_factor
                except Exception:
                    continue
                if area < min_area:
                    continue

                try:
                    box = tuple(_com_member(face, "GetBox"))
                    centre = (
                        (box[0] + box[3]) / 2.0,
                        (box[1] + box[4]) / 2.0,
                        (box[2] + box[5]) / 2.0,
                    )
                    # GetBox is approximate and its centre can sit off the face
                    # (or outside it entirely). GetClosestPointOn projects that
                    # centre back onto real geometry, which is what makes the
                    # returned point safe to feed to the point-based tools.
                    nearest = tuple(face.GetClosestPointOn(*centre))
                except Exception:
                    continue
                if len(nearest) < 3:
                    continue

                entry = {
                    "body": body_name,
                    "face_index": face_index,
                    "surface_type": kind,
                    "area": area,
                    "pick_point": {
                        "x": nearest[0] * factor,
                        "y": nearest[1] * factor,
                        "z": nearest[2] * factor,
                        "unit": active_unit,
                    },
                }
                if kind == "plane":
                    # Only planar faces have a single meaningful normal; on a
                    # cylinder IFace2.Normal returns zeros rather than failing,
                    # which would read as a real direction.
                    try:
                        normal = tuple(_com_member(face, "Normal"))
                        if len(normal) >= 3 and any(normal[:3]):
                            entry["normal"] = [normal[0], normal[1], normal[2]]
                    except Exception:
                        pass
                if kind == "cylinder" and surface is not None:
                    try:
                        # ISurface.CylinderParams: origin (0-2), axis (3-5), radius (6).
                        params = tuple(_com_member(surface, "CylinderParams"))
                        if len(params) >= 7:
                            entry["axis"] = [params[3], params[4], params[5]]
                            entry["radius"] = params[6] * factor
                    except Exception:
                        pass
                faces.append(entry)
                if len(faces) >= max_faces:
                    truncated = True
                    break

        return {
            "count": len(faces),
            "inspected_face_count": inspected,
            "truncated": truncated,
            "unit": active_unit,
            "filter": {"surface_type": wanted, "min_area": min_area},
            "faces": faces,
        }

    return await _run(_impl)


# ===========================================================================
# Utility tools
# ===========================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def rebuild_model(force: bool = True, top_only: bool = False) -> dict:
    """Rebuild the active document and report whether SolidWorks accepted it.

    Use this before saving, exporting, or measuring a model after feature,
    equation, or configuration edits. ``force`` rebuilds all features; setting
    it to false performs the lighter normal rebuild.
    """

    def _impl():
        doc = _active_doc()
        if force:
            result = doc.ForceRebuild3(bool(top_only))
        else:
            result = doc.EditRebuild3
            if callable(result):
                result = result()
        try:
            doc.FeatureManager.UpdateFeatureTree()
        except Exception:
            pass
        _redraw_document(doc)
        return {"force": force, "top_only": top_only, "rebuilt": bool(result)}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def validate_model() -> dict:
    """Rebuild and inspect the feature tree for SolidWorks errors and warnings.

    This is a production gate: it returns ``valid: false`` when a feature has a
    non-zero ``IFeature.GetErrorCode2`` result after rebuilding.
    """

    def _impl():
        doc = _active_doc()
        rebuild_result = doc.ForceRebuild3(False)
        issues = []
        raw_features = doc.FeatureManager.GetFeatures(False) or ()
        for raw_feature in raw_features:
            feature = win32com.client.Dispatch(raw_feature)
            warning = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
            try:
                result = feature.GetErrorCode2(warning)
                code, outputs = _split_com_result(result)
                if outputs:
                    is_warning = bool(outputs[0])
                else:
                    is_warning = bool(warning.value)
                code = int(code)
                if code != 0:
                    issues.append({
                        "feature": feature.Name,
                        "feature_type": feature.GetTypeName2,
                        "error_code": code,
                        "severity": "warning" if is_warning else "error",
                    })
            except Exception as exc:
                issues.append({
                    "feature": getattr(feature, "Name", "(unknown)"),
                    "feature_type": "(unavailable)",
                    "error_code": None,
                    "severity": "inspection_error",
                    "message": str(exc),
                })
        errors = [issue for issue in issues if issue["severity"] == "error"]
        return {
            "rebuilt": bool(rebuild_result),
            "feature_count": len(raw_features),
            "valid": not errors,
            "error_count": len(errors),
            "warning_count": len(issues) - len(errors),
            "issues": issues,
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_document_dependencies(
    traverse_all: bool = True,
    search_paths: bool = True,
    include_read_only: bool = True,
    include_broken: bool = True,
) -> dict:
    """List referenced models and identify broken/read-only document links."""

    def _impl():
        doc = _active_doc()
        # GetDependencies is the current API, but this installation's dynamic
        # COM proxy raises a server exception for a saved part with no links.
        # GetDependencies2 returns the same dependency rows for that case and
        # is retained as a compatibility fallback.
        try:
            raw = doc.Extension.GetDependencies(
                bool(traverse_all), bool(search_paths), bool(include_read_only),
                bool(include_broken), True,
            )
        except Exception as extension_error:
            try:
                raw = doc.GetDependencies2(
                    bool(traverse_all), bool(search_paths), bool(include_read_only),
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    "SolidWorks could not inspect document dependencies through either API path: "
                    f"{fallback_error}"
                ) from extension_error
        values = list(raw or ())
        stride = 3 if include_read_only else 2
        dependencies = []
        for index in range(0, len(values), stride):
            if index + 1 >= len(values):
                break
            path = str(values[index + 1])
            dependencies.append({
                "name": str(values[index]),
                "path": path,
                "read_only": str(values[index + 2]).lower() == "true" if include_read_only and index + 2 < len(values) else None,
                "exists": os.path.exists(path.split("|")[0]),
            })
        broken = [item for item in dependencies if not item["exists"]]
        return {"count": len(dependencies), "broken_count": len(broken), "dependencies": dependencies}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_persistent_reference(
    entity_type: str,
    x: float, y: float, z: float,
    unit: Optional[str] = None,
) -> dict:
    """Create a portable base64 persistent ID for a model face, edge, or vertex.

    Unlike coordinate-only selection, this ID can be resolved after a rebuild
    when the referenced entity remains topologically valid.
    """

    def _impl():
        supported = {"face": "FACE", "edge": "EDGE", "vertex": "VERTEX"}
        key = entity_type.strip().lower()
        if key not in supported:
            raise ValueError("entity_type must be face, edge, or vertex.")
        doc = _active_doc()
        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(
            "", supported[key], to_meters(x, unit), to_meters(y, unit), to_meters(z, unit),
            False, 0, empty, 0,
        ):
            raise RuntimeError(f"No {key} found at ({x}, {y}, {z}) {unit or _default_unit}.")
        entity = doc.SelectionManager.GetSelectedObject6(1, -1)
        raw_id = doc.Extension.GetPersistReference3(entity)
        if not raw_id:
            raise RuntimeError("SolidWorks could not create a persistent reference for the selected entity.")
        try:
            binary = bytes(raw_id)
        except TypeError:
            binary = bytes(list(raw_id))
        doc.ClearSelection2(True)
        return {
            "entity_type": key,
            "reference": base64.b64encode(binary).decode("ascii"),
            "byte_length": len(binary),
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def resolve_persistent_reference(reference: str, select: bool = True) -> dict:
    """Resolve a base64 persistent ID and optionally select its current entity."""

    def _impl():
        try:
            binary = base64.b64decode(reference.encode("ascii"), validate=True)
        except Exception as exc:
            raise ValueError("reference must be a valid base64 persistent ID.") from exc
        doc = _active_doc()
        data = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, list(binary))
        status = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        result = doc.Extension.GetObjectByPersistReference3(data, status)
        entity, outputs = _split_com_result(result)
        status_code = int(outputs[0]) if outputs else int(status.value)
        if entity is None:
            return {"resolved": False, "status_code": status_code, "selected": False}
        selected = False
        if select:
            doc.ClearSelection2(True)
            try:
                # Select2 accepts the resolved entity directly. The other
                # selection overloads require an ISelectData dispatch object
                # and reject None in this Python COM host.
                entity.Select2(False, 0)
                count = doc.SelectionManager.GetSelectedObjectCount2(-1)
                selected = int(count) > 0
            except Exception:
                try:
                    selection_data = doc.SelectionManager.CreateSelectData
                    entity.Select4(False, selection_data)
                    count = doc.SelectionManager.GetSelectedObjectCount2(-1)
                    selected = int(count) > 0
                except Exception:
                    selected = False
        return {"resolved": True, "status_code": status_code, "selected": selected}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_configurations() -> dict:
    """List all configurations, their descriptions, and the active configuration."""

    def _impl():
        doc = _active_doc()
        names_member = doc.GetConfigurationNames
        names = list(names_member() if callable(names_member) else names_member or ())
        active = doc.ConfigurationManager.ActiveConfiguration
        active_name = active.Name() if callable(getattr(active, "Name", None)) else active.Name
        configurations = []
        for name in names:
            cfg = doc.GetConfigurationByName(name)
            comment = ""
            try:
                comment = cfg.Comment() if callable(getattr(cfg, "Comment", None)) else cfg.Comment
            except Exception:
                pass
            configurations.append({"name": name, "comment": comment, "active": name == active_name})
        return {"count": len(configurations), "active_configuration": active_name, "configurations": configurations}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_configuration(name: str) -> dict:
    """Delete a non-active configuration from the active model."""

    def _impl():
        doc = _active_doc()
        active = doc.ConfigurationManager.ActiveConfiguration
        active_name = active.Name() if callable(getattr(active, "Name", None)) else active.Name
        if name.lower() == active_name.lower():
            raise RuntimeError("Cannot delete the active configuration. Switch configuration first.")
        names_member = doc.GetConfigurationNames
        names = list(names_member() if callable(names_member) else names_member or ())
        target = next((item for item in names if item.lower() == name.lower()), None)
        if target is None:
            raise ValueError(f"Configuration '{name}' does not exist.")
        deleted = doc.DeleteConfiguration2(target)
        if not deleted:
            raise RuntimeError(f"SolidWorks could not delete configuration '{target}'.")
        return {"deleted": target, "active_configuration": active_name}

    return await _run(_impl)


def _equation_manager(doc):
    # Dynamic pywin32 exposes the manager as a callable dispatch object; calling
    # it invokes a non-existent default member. The property itself is the API
    # object in both typed and dynamic hosts.
    return doc.GetEquationMgr


def _evaluate_equations(manager):
    """Invoke EvaluateAll across typed and dynamic SolidWorks proxies."""
    result = manager.EvaluateAll
    return result() if callable(result) else result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_equations() -> dict:
    """List equations and global variables in the active document."""

    def _impl():
        manager = _equation_manager(_active_doc())
        count_member = manager.GetCount
        count = int(count_member() if callable(count_member) else count_member)
        items = []
        for index in range(count):
            equation = manager.Equation(index)
            global_variable = manager.GlobalVariable(index)
            disabled = manager.Disabled(index)
            value = manager.Value(index)
            items.append({"index": index, "equation": equation, "value": value,
                          "global_variable": bool(global_variable), "disabled": bool(disabled)})
        return {"count": count, "equations": items}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_equation(equation: str, solve: bool = True) -> dict:
    """Add an equation or global variable, e.g. ``\"Length\" = 25mm``.

    solve=False adds the equation without evaluating it, so several can be
    added before one rebuild. Use list_equations or rebuild_model afterwards
    to apply them."""

    def _impl():
        manager = _equation_manager(_active_doc())
        index = int(manager.Add2(-1, equation, bool(solve)))
        if index < 0:
            raise RuntimeError(f"SolidWorks rejected equation: {equation}")
        # IEquationMgr.Add2 already honours its third argument, so the only
        # thing an extra EvaluateAll can do is evaluate when the caller asked
        # NOT to. The condition here used to be `if not solve`, i.e. exactly
        # backwards: solve=False added the equation and then evaluated it
        # anyway, while the return still reported "solved": False.
        evaluation_result = _evaluate_equations(manager) if solve else None
        return {"index": index, "equation": manager.Equation(index),
                "value": manager.Value(index), "solved": bool(solve),
                "evaluation_result": evaluation_result}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_equation(index: int, equation: str, solve: bool = True) -> dict:
    """Replace an equation by index and optionally evaluate it immediately."""

    def _impl():
        manager = _equation_manager(_active_doc())
        count_member = manager.GetCount
        count = int(count_member() if callable(count_member) else count_member)
        if not 0 <= index < count:
            raise ValueError(f"index must be between 0 and {count - 1}.")
        manager.Equation(index, equation)
        evaluation_result = None
        if solve:
            evaluation_result = _evaluate_equations(manager)
        stored_equation = manager.Equation(index)
        if stored_equation != equation:
            raise RuntimeError(f"SolidWorks did not retain equation at index {index}.")
        return {"index": index, "equation": stored_equation, "value": manager.Value(index),
                "solved": solve, "evaluation_result": evaluation_result}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_equation(index: int) -> dict:
    """Delete an equation or global variable by zero-based index."""

    def _impl():
        manager = _equation_manager(_active_doc())
        count_member = manager.GetCount
        count = int(count_member() if callable(count_member) else count_member)
        if not 0 <= index < count:
            raise ValueError(f"index must be between 0 and {count - 1}.")
        removed = manager.Equation(index)
        # Dynamic dispatch reports a false/empty return for Delete even though
        # the mutation succeeds. Verify through the post-operation count.
        manager.Delete(index)
        after_member = manager.GetCount
        after_count = int(after_member() if callable(after_member) else after_member)
        if after_count != count - 1:
            raise RuntimeError(f"SolidWorks could not delete equation at index {index}.")
        return {"deleted_index": index, "equation": removed}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def export_flat_pattern_dxf(
    filepath: str,
    include_hidden_edges: bool = False,
    include_bend_lines: bool = True,
    include_sketches: bool = False,
    include_bounding_box: bool = False,
) -> dict:
    """Export a sheet-metal flat pattern to DXF/DWG using ``IPartDoc.ExportToDWG2``."""

    def _impl():
        doc = _active_doc()
        if _doc_type(doc) != 1:
            raise RuntimeError("Flat-pattern DXF export requires an active part document.")
        abs_path = _resolve_write_path(filepath)
        if os.path.splitext(abs_path)[1].lower() not in {".dxf", ".dwg"}:
            raise ValueError("filepath must end in .dxf or .dwg.")
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        options = 1
        if include_hidden_edges:
            options |= 2
        if include_bend_lines:
            options |= 4
        if include_sketches:
            options |= 8
        if include_bounding_box:
            options |= 2048
        alignment = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0] * 12)
        ok = bool(doc.ExportToDWG2(abs_path, _doc_path(doc), 1, True, alignment, False, False, options, None))
        if not ok or not os.path.exists(abs_path):
            raise RuntimeError("SolidWorks could not export the sheet-metal flat pattern.")
        return {"path": abs_path, "options": {"hidden_edges": include_hidden_edges,
                "bend_lines": include_bend_lines, "sketches": include_sketches,
                "bounding_box": include_bounding_box}}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def pack_and_go(
    destination: str,
    include_drawings: bool = True,
    include_suppressed: bool = True,
    include_toolbox_components: bool = False,
    flatten_to_single_folder: bool = True,
    prefix: str = "",
    suffix: str = "",
) -> dict:
    """Create a portable Pack and Go copy of the active saved SolidWorks document.

    ``destination`` is a folder, or a ``.zip`` file.  Pack and Go copies the
    model and every discovered reference without altering the active document.
    The result is deliberately written only to a caller-selected location, so
    automated jobs cannot overwrite the source design by accident.
    """

    def _impl():
        doc = _active_doc()
        source_path = _doc_path(doc)
        if not source_path or not os.path.isfile(source_path):
            raise RuntimeError("Save the active document before running Pack and Go.")
        destination_path = _resolve_write_path(destination)
        is_zip = destination_path.lower().endswith(".zip")
        if is_zip:
            os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)
        else:
            os.makedirs(destination_path, exist_ok=True)

        native_error = None
        try:
            package = doc.Extension.GetPackAndGo()
            if package is None:
                raise RuntimeError("SolidWorks could not initialize a Pack and Go session.")
            package.IncludeDrawings = bool(include_drawings)
            package.IncludeSuppressed = bool(include_suppressed)
            package.IncludeToolboxComponents = bool(include_toolbox_components)
            package.FlattenToSingleFolder = bool(flatten_to_single_folder)
            if prefix:
                package.AddPrefix = str(prefix)
            if suffix:
                package.AddSuffix = str(suffix)

            source_count = int(package.GetDocumentNamesCount())
            if source_count < 1:
                raise RuntimeError("Pack and Go did not find the active document to copy.")
            if not bool(package.SetSaveToName2(True, destination_path)):
                raise RuntimeError("SolidWorks rejected the Pack and Go destination.")
            statuses = list(doc.Extension.SavePackAndGo(package) or ())
            failures = [int(status) for status in statuses if int(status) != 0]
            if failures:
                raise RuntimeError(f"Pack and Go reported save status codes: {failures}.")
            created = os.path.isfile(destination_path) if is_zip else any(os.scandir(destination_path))
            if not created:
                raise RuntimeError("Pack and Go returned success but did not create output files.")
            return {
                "destination": destination_path,
                "format": "zip" if is_zip else "folder",
                "source_document_count": source_count,
                "status_codes": [int(status) for status in statuses],
                "backend": "solidworks_pack_and_go",
            }
        except Exception as exc:
            # Some dynamic pywin32 builds cannot marshal GetPackAndGo's
            # interface return value ("Parameter not optional") even though
            # it succeeds from VBA/.NET. Do not abandon portability in that
            # environment: create a deterministic package from the resolved
            # dependency graph and explicitly disclose the backend used.
            native_error = str(exc)

        try:
            raw_dependencies = list(doc.GetDependencies2(True, True, True) or ())
        except Exception:
            raw_dependencies = []
        dependency_paths = [source_path]
        for index in range(1, len(raw_dependencies), 3):
            candidate = str(raw_dependencies[index]).split("|")[0]
            if candidate and os.path.isfile(candidate):
                dependency_paths.append(candidate)
        dependency_paths = list(dict.fromkeys(dependency_paths))

        staging_dir = destination_path if not is_zip else os.path.join(
            os.path.dirname(destination_path) or ".",
            f".{os.path.splitext(os.path.basename(destination_path))[0]}_staging",
        )
        # This fallback copies files with shutil; it does NOT rewrite the
        # references inside an assembly. Renaming a part while the assembly
        # still points at the old name produces a copy that opens with
        # missing references -- worse than no copy at all, and the process
        # rule is "always make a working copy". So refuse the rename here
        # rather than produce a broken package.
        if prefix or suffix:
            raise RuntimeError(
                "Native Pack and Go failed and the fallback cannot apply a "
                f"prefix/suffix: renaming files without rewriting the "
                f"assembly's internal references produces a copy with missing "
                f"references. Retry without prefix/suffix, or fix the native "
                f"Pack and Go error first: {native_error}"
            )

        os.makedirs(staging_dir, exist_ok=True)
        copied = []
        collisions = []
        used_names: dict = {}
        for original in dependency_paths:
            filename = os.path.basename(original)
            stem, extension = os.path.splitext(filename)
            target_name = f"{stem}{extension}"
            # Two parts of the same name from different folders used to
            # overwrite each other silently, in both the flattened and the
            # models/ layout, because the destination name was just the
            # basename. Disambiguate instead.
            key = target_name.lower()
            if key in used_names:
                index = 2
                while f"{stem}_{index}{extension}".lower() in used_names:
                    index += 1
                target_name = f"{stem}_{index}{extension}"
                collisions.append({
                    "original": original,
                    "clashed_with": used_names[key],
                    "renamed_to": target_name,
                })
            used_names[target_name.lower()] = original

            relative_target = target_name if flatten_to_single_folder else os.path.join("models", target_name)
            target = os.path.join(staging_dir, relative_target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target):
                raise RuntimeError(
                    f"Refusing to overwrite an existing file in the package: "
                    f"{target}. Choose an empty destination."
                )
            shutil.copy2(original, target)
            copied.append(relative_target)
        if is_zip:
            with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative_target in copied:
                    archive.write(os.path.join(staging_dir, relative_target), relative_target)
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not copied or (is_zip and not os.path.isfile(destination_path)):
            raise RuntimeError("The managed packaging fallback did not create output files.")
        result = {
            "destination": destination_path,
            "format": "zip" if is_zip else "folder",
            "source_document_count": len(dependency_paths),
            "copied_file_count": len(copied),
            "copied_files": copied,
            "backend": "managed_dependency_fallback",
            "native_pack_and_go_error": native_error,
        }
        if len(copied) != len(dependency_paths):
            raise RuntimeError(
                f"Packaged {len(copied)} of {len(dependency_paths)} dependencies; "
                f"the package is incomplete."
            )
        if collisions:
            result["name_collisions"] = collisions
            result["warning"] = (
                f"{len(collisions)} file(s) shared a name with another and were "
                f"renamed to avoid overwriting. The copied assembly will report "
                f"those as missing references, because this fallback does not "
                f"rewrite internal references. Open it and repair the paths, or "
                f"re-run once native Pack and Go works."
            )
        return result

    return await _run(_impl)


def _configuration_by_name(doc, name: str):
    """Return a named configuration, or the active configuration for an empty name."""
    if not name.strip():
        return doc.ConfigurationManager.ActiveConfiguration
    configuration = doc.GetConfigurationByName(name)
    if configuration is None:
        raise ValueError(f"Configuration '{name}' does not exist.")
    return configuration


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_display_states(configuration: str = "") -> dict:
    """List display states for a configuration (or the active configuration)."""

    def _impl():
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        states = getattr(cfg, "GetDisplayStates", ())
        states = states() if callable(states) else states
        return {"configuration": str(cfg.Name), "count": len(states or ()), "display_states": list(states or ())}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_display_state(name: str, configuration: str = "", activate: bool = False) -> dict:
    """Create a named display state, optionally applying it immediately."""

    def _impl():
        display_name = name.strip()
        if not display_name:
            raise ValueError("Display-state name cannot be empty.")
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        states = getattr(cfg, "GetDisplayStates", ())
        states = states() if callable(states) else states
        if display_name.casefold() in {str(value).casefold() for value in (states or ())}:
            raise ValueError(f"Display state '{display_name}' already exists.")
        if not bool(cfg.CreateDisplayState(display_name)):
            raise RuntimeError(f"SolidWorks failed to create display state '{display_name}'.")
        applied = bool(cfg.ApplyDisplayState(display_name)) if activate else False
        _redraw_document(doc)
        return {"configuration": str(cfg.Name), "display_state": display_name, "active": applied}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def apply_display_state(name: str, configuration: str = "") -> dict:
    """Apply an existing display state in the requested configuration."""

    def _impl():
        display_name = name.strip()
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        states = getattr(cfg, "GetDisplayStates", ())
        states = states() if callable(states) else states
        if display_name.casefold() not in {str(value).casefold() for value in (states or ())}:
            raise ValueError(f"Display state '{display_name}' does not exist in '{cfg.Name}'.")
        if not bool(cfg.ApplyDisplayState(display_name)):
            raise RuntimeError(f"SolidWorks failed to apply display state '{display_name}'.")
        _redraw_document(doc)
        return {"configuration": str(cfg.Name), "display_state": display_name, "applied": True}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def rename_display_state(old_name: str, new_name: str, configuration: str = "") -> dict:
    """Rename one display state without changing its appearance data."""

    def _impl():
        old_value, new_value = old_name.strip(), new_name.strip()
        if not old_value or not new_value:
            raise ValueError("Both display-state names are required.")
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        if not bool(cfg.RenameDisplayState(old_value, new_value)):
            raise RuntimeError(f"SolidWorks could not rename display state '{old_value}'.")
        return {"configuration": str(cfg.Name), "old_name": old_value, "new_name": new_value}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_display_state(name: str, configuration: str = "") -> dict:
    """Delete a display state after confirming it belongs to the configuration."""

    def _impl():
        display_name = name.strip()
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        states = getattr(cfg, "GetDisplayStates", ())
        states = states() if callable(states) else states
        if display_name.casefold() not in {str(value).casefold() for value in (states or ())}:
            raise ValueError(f"Display state '{display_name}' does not exist in '{cfg.Name}'.")
        if not bool(cfg.DeleteDisplayState(display_name)):
            raise RuntimeError(f"SolidWorks could not delete display state '{display_name}'.")
        _redraw_document(doc)
        return {"configuration": str(cfg.Name), "deleted": display_name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_units(unit: str) -> dict:
    """Set the default unit (mm, cm, m, in, or ft) used by tools when 'unit' is omitted."""
    global _default_unit
    u = unit.lower()
    if u not in UNIT_TO_METERS:
        raise ValueError(f"Invalid unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
    _default_unit = u
    return {"default_unit": _default_unit}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_view(view_name: str = "isometric") -> dict:
    """Set the camera view orientation.
    Options: front, back, left, right, top, bottom, isometric, trimetric, dimetric."""

    def _impl():
        doc = _active_doc()
        views = {
            "front": ("*Front", 1), "back": ("*Back", 2),
            "left": ("*Left", 3), "right": ("*Right", 4),
            "top": ("*Top", 5), "bottom": ("*Bottom", 6),
            "isometric": ("*Isometric", 7), "trimetric": ("*Trimetric", 8),
            "dimetric": ("*Dimetric", 9),
        }
        entry = views.get(view_name.lower())
        if entry is None:
            raise ValueError(f"Unknown view '{view_name}'. Use: {', '.join(views)}")
        name, vid = entry
        doc.ShowNamedView2(name, vid)
        doc.ViewZoomtofit2()
        return {"view": view_name}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def zoom_to_fit() -> dict:
    """Zoom the view to fit the entire model in the viewport."""

    def _impl():
        doc = _active_doc()
        doc.ViewZoomtofit2()
        return {"zoomed": True}

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def zoom_to_area(x1: float, y1: float, x2: float, y2: float, unit: Optional[str] = None) -> dict:
    """Zoom into a rectangular area defined by two corner points (screen-mapped to model)."""

    def _impl():
        doc = _active_doc()
        doc.ViewZoomTo2(
            to_meters(x1, unit), to_meters(y1, unit), 0,
            to_meters(x2, unit), to_meters(y2, unit), 0,
        )
        return {"area": [[x1, y1], [x2, y2]], "unit": unit or _default_unit}

    return await _run(_impl)


# 3x3 rotation matrices (row-major, flattened) for each named view, captured
# live from IModelView.Orientation via set_view so get_view_state can match a
# live camera back to a human-readable name instead of only raw numbers.
_NAMED_VIEW_ORIENTATIONS = {
    "front": (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    "back": (-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, -1.0),
    "left": (0.0, 0.0, -1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0),
    "right": (0.0, 0.0, 1.0, 0.0, 1.0, 0.0, -1.0, 0.0, 0.0),
    "top": (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, -1.0, 0.0),
    "bottom": (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0),
    "isometric": (0.707107, -0.408204, 0.577382, 0.0, 0.816541, 0.577288, -0.707107, -0.408204, 0.577382),
    "trimetric": (0.884418, -0.240366, 0.400036, 0.0, 0.857167, 0.515038, -0.466695, -0.455509, 0.758094),
    "dimetric": (0.935414, -0.117851, 0.333333, 0.0, 0.942809, 0.333333, -0.353553, -0.311805, 0.881917),
}
# Sum-of-squared-differences below this counts as "the same view"; a model
# tumbled even slightly by the user will exceed it and correctly report None.
_NAMED_VIEW_MATCH_TOLERANCE = 1e-4


def _closest_named_view(matrix: list) -> Optional[str]:
    if len(matrix) != 9:
        return None
    best_name, best_distance = None, None
    for name, reference in _NAMED_VIEW_ORIENTATIONS.items():
        distance = sum((a - b) ** 2 for a, b in zip(matrix, reference))
        if best_distance is None or distance < best_distance:
            best_name, best_distance = name, distance
    return best_name if best_distance is not None and best_distance <= _NAMED_VIEW_MATCH_TOLERANCE else None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def get_view_state() -> dict:
    """Read the camera state the user is actually looking at right now.

    Reports the zoom scale, the raw SolidWorks display-mode code (wireframe /
    shaded / etc. -- shown as-is since SolidWorks does not expose a name for
    it through this API), the 3x3 camera rotation matrix, and -- when the
    camera exactly matches one of set_view's named orientations -- that name
    under closest_named_view (None if the user has freely rotated the model).
    Also reports the active configuration, since that changes what geometry
    is visible without changing the camera at all.

    Unlike capture_viewport, this returns structured numbers instead of an
    image, so a caller can reason about "is this still isometric?" or
    "did the zoom change?" without decoding pixels.
    """

    def _impl():
        doc = _active_doc()
        view = doc.ActiveView

        scale = view.Scale2
        scale = scale() if callable(scale) else scale

        display_mode = view.DisplayMode
        display_mode = display_mode() if callable(display_mode) else display_mode

        orientation = view.Orientation
        orientation = orientation() if callable(orientation) else orientation
        matrix = [round(float(v), 6) for v in (orientation or ())]

        active = doc.ConfigurationManager.ActiveConfiguration
        active_name = active.Name() if callable(getattr(active, "Name", None)) else active.Name

        return {
            "zoom_scale": float(scale),
            "display_mode_code": int(display_mode),
            "orientation_matrix": matrix,
            "closest_named_view": _closest_named_view(matrix),
            "active_configuration": active_name,
        }

    return await _run(_impl)


# swMacroMethods_e filters, confirmed live against real .swp files rather than
# taken from the enum names: 1 lists the no-argument procedures -- the only
# ones RunMacro2 can actually call -- and 2 lists procedures that take
# arguments. 0 reads like an "all" flag but returns nothing at all.
_MACRO_METHODS_WITHOUT_ARGS = 1
_MACRO_METHODS_WITH_ARGS = 2
_MACRO_SUFFIXES = {".swp", ".swb", ".dll"}

# Opt-in, and deliberately a SEPARATE allowlist from PROJECT_ROOTS_ENV: being
# allowed to write a BOM export into a project folder says nothing about
# whether code dropped in that same folder should be allowed to RUN.
# run_macro executes a VBA/.NET macro body with the same access as the
# SolidWorks session itself (server.py never runs a macro's body, by
# design -- see list_macro_methods' docstring); unset, behaviour is
# unchanged from before this existed (any .swp/.swb/.dll by path).
MACRO_ROOTS_ENV = "SOLIDWORKS_MCP_MACRO_ROOTS"


def _resolve_macro_path(path: str):
    macro_path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isfile(macro_path):
        raise FileNotFoundError(f"Macro file not found: {macro_path}")
    suffix = os.path.splitext(macro_path)[1].lower()
    if suffix not in _MACRO_SUFFIXES:
        raise ValueError(
            f"'{suffix or macro_path}' is not a SolidWorks macro. "
            f"Expected one of: {', '.join(sorted(_MACRO_SUFFIXES))}"
        )
    roots = [os.path.abspath(p) for p in
             os.environ.get(MACRO_ROOTS_ENV, "").split(os.pathsep) if p.strip()]
    if roots and not any(
        macro_path == root or macro_path.startswith(root + os.sep) for root in roots
    ):
        raise PermissionError(
            f"Refusing to run macro '{macro_path}': it is outside the "
            f"configured macro root(s) ({MACRO_ROOTS_ENV}="
            f"{os.environ.get(MACRO_ROOTS_ENV)!r}). Move it into one of "
            f"those folders, or reconfigure {MACRO_ROOTS_ENV}."
        )
    return macro_path


def _macro_methods(app, macro_path: str, method_filter: int) -> list:
    try:
        methods = app.GetMacroMethods(macro_path, method_filter)
    except Exception:
        return []
    return [str(entry) for entry in (methods or ())]


# swRunMacroError_e, from the generated type library -- names the codes
# RunMacro2 hands back so a failure says *why* instead of just a bare number.
_RUN_MACRO_ERROR_NAMES = {
    1: "InvalidArg", 2: "MacrosAreDisabled", 3: "NotInDesignMode",
    4: "OnlyCodeModules", 5: "OutOfMemory", 6: "InvalidProcname",
    7: "InvalidPropertyType", 8: "SuborfuncExpected", 9: "BadParmCount",
    10: "BadVarType", 11: "UserInterrupt", 12: "Exception", 13: "Overflow",
    14: "TypeMismatch", 15: "ParmNotOptional", 16: "UnknownLcid", 17: "Busy",
    18: "ConnectionTerminated", 19: "CallRejected", 20: "CallFailed",
    21: "Zombied", 22: "Invalidindex", 23: "NoPermission", 24: "Reverted",
    25: "TooManyOpenFiles", 26: "DiskError", 27: "CantSave",
    28: "OpenFileFailed",
}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
async def list_macro_methods(path: str) -> dict:
    """List the entry points inside a SolidWorks macro WITHOUT executing it.

    Returns ``runnable`` (procedures taking no arguments, which is what
    run_macro can invoke) and ``needs_arguments`` (procedures that take
    parameters and therefore cannot be launched directly). Both are reported
    as "Module.Procedure" strings, ready to be split into run_macro's
    ``module`` and ``procedure``.

    This only inspects the file, so it is safe to point at a macro whose
    contents you have not reviewed yet.
    """

    def _impl():
        app = _connect()
        macro_path = _resolve_macro_path(path)
        runnable = _macro_methods(app, macro_path, _MACRO_METHODS_WITHOUT_ARGS)
        with_args = _macro_methods(app, macro_path, _MACRO_METHODS_WITH_ARGS)
        return {
            "path": macro_path,
            "runnable": runnable,
            "needs_arguments": with_args,
            "runnable_count": len(runnable),
        }

    return await _run(_impl)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def run_macro(
    path: str,
    module: str = "",
    procedure: str = "",
    unload_after: bool = True,
) -> dict:
    """Run a procedure from an existing SolidWorks macro file (.swp/.swb/.dll).

    SECURITY: this executes whatever VBA the macro contains, with the same
    privileges as the SolidWorks session -- it is not sandboxed, and a macro
    can touch the file system and the rest of the machine. Only run macro
    files you trust. Use list_macro_methods first to see what is inside one.

    Leave ``module`` and ``procedure`` empty to auto-select when the macro
    exposes exactly one runnable entry point; if there are several, the error
    lists them so you can pick one. ``unload_after`` unloads the VBA project
    when the run finishes, which avoids the macro holding state between calls.
    """

    def _impl():
        app = _connect()
        macro_path = _resolve_macro_path(path)

        target_module, target_procedure = module.strip(), procedure.strip()
        if not target_module or not target_procedure:
            runnable = _macro_methods(app, macro_path, _MACRO_METHODS_WITHOUT_ARGS)
            if len(runnable) != 1:
                raise ValueError(
                    "Specify module and procedure. "
                    f"This macro exposes {len(runnable)} runnable entry points: "
                    f"{', '.join(runnable) if runnable else '(none found)'}"
                )
            target_module, _, target_procedure = runnable[0].partition(".")

        # swRunMacroOption_e: 0 keeps the VBA project loaded, 1 unloads it
        # once the procedure returns.
        options = 1 if unload_after else 0
        result = app.RunMacro2(macro_path, target_module, target_procedure, options)
        # RunMacro2 has a by-ref error out-parameter, so the generated proxy
        # hands back (succeeded, error_code) while dynamic dispatch returns
        # just the boolean.
        if isinstance(result, tuple):
            succeeded, error_code = bool(result[0]), int(result[1])
        else:
            succeeded, error_code = bool(result), 0

        if not succeeded:
            error_name = _RUN_MACRO_ERROR_NAMES.get(error_code, "Unknown")
            raise RuntimeError(
                f"SolidWorks refused to run '{target_module}.{target_procedure}' from "
                f"{macro_path}: {error_name} (error code {error_code}). Confirm the "
                "module and procedure names with list_macro_methods."
            )
        return {
            "path": macro_path,
            "module": target_module,
            "procedure": target_procedure,
            "unloaded_after": unload_after,
            "error_code": error_code,
            "ran": True,
        }

    return await _run(_impl)


_BLOCKED_BUILTINS = frozenset({
    "open", "__import__", "exec", "eval", "compile",
    "exit", "quit", "input", "breakpoint",
})
_SAFE_BUILTINS = {k: v for k, v in vars(builtins).items() if k not in _BLOCKED_BUILTINS}
EXECUTE_PYTHON_ENV = "SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON"


def _execute_python_enabled() -> bool:
    return os.environ.get(EXECUTE_PYTHON_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# Registered only when explicitly enabled, not registered-then-rejecting.
# Left always-registered, it would sit in the catalog handed to every
# client/model regardless of whether this install ever turns it on --
# costing context tokens for a tool that only ever answers "disabled" is
# worse than not advertising it at all, and the previous behaviour (always
# registered, raising PermissionError when called) did exactly that.
if _execute_python_enabled():
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    async def execute_python(code: str) -> dict:
        """Run privileged Python with 'sw' and 'doc' in scope.

        Only registered when SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1 was set
        before the server started -- it does not appear in the tool list at
        all otherwise. Enable it only for trusted local debugging sessions:
        the COM objects in scope can change the active SolidWorks session.
        """

        def _impl():
            app = _connect()
            try:
                doc = app.ActiveDoc
            except Exception:
                doc = None
            buf = io.StringIO()
            exec_globals = {
                "__builtins__": _SAFE_BUILTINS,
                "sw": app, "doc": doc,
                "win32com": win32com, "pythoncom": pythoncom, "math": math,
            }
            with contextlib.redirect_stdout(buf):
                exec(code, exec_globals)
            return {"stdout": buf.getvalue()}

        return await _run(_impl)


# ---------------------------------------------------------------------------
# Design-sandbox guidance: resource + prompt
# ---------------------------------------------------------------------------
# These do not touch SolidWorks. They exist so the calling AI has a
# methodology layer on top of the raw tool catalog: which tools are actually
# reliable in this installation, and how to sequence plan/build/verify steps
# instead of improvising feature-by-feature. See RELATORIO_TESTES.md for the
# live-tested workflow this codifies.

README_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")


@mcp.resource(
    "solidworks://tool-status",
    name="tool-reliability-status",
    description=(
        "Quais ferramentas estao confirmadas OK vs. experimentais (EXP) "
        "nesta versao/instalacao do SolidWorks, com as ressalvas conhecidas "
        "de cada uma. Consulte antes de depender de uma ferramenta EXP."
    ),
    mime_type="text/markdown",
)
def tool_reliability_status() -> str:
    """Return the live-verification status table straight from README.md."""
    text = _read_readme_or_fallback()
    heading = re.search(r"^## Status de verifica.*$", text, re.MULTILINE)
    if not heading:
        return text
    body_start = heading.end()
    next_heading = re.search(r"^## ", text[body_start:], re.MULTILINE)
    body_end = body_start + next_heading.start() if next_heading else len(text)
    return text[heading.start():body_end].strip()


def _read_readme_or_fallback() -> str:
    try:
        with open(README_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "README.md nao encontrado ao lado de server.py."


# ---------------------------------------------------------------------------
# Engineering knowledge base (.claude/) exposed as MCP resources
# ---------------------------------------------------------------------------
# .claude/CLAUDE.md and .claude/knowledge/*.md are Claude Code's own
# auto-load convention -- invisible to a client that only talks MCP (Claude
# Desktop with this server as an extension, which is how this project is
# actually used day to day). Resources are the MCP-native way to make the
# same files readable on demand there too, so both clients read the exact
# same source of truth instead of two copies drifting apart.

CLAUDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude")
KNOWLEDGE_DIR = os.path.join(CLAUDE_DIR, "knowledge")


def _read_claude_file(relative_path: str) -> str:
    path = os.path.join(CLAUDE_DIR, relative_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return f"{relative_path} nao encontrado em .claude/ ao lado de server.py."


@mcp.resource(
    "solidworks://knowledge/index",
    name="indice-de-conhecimento-de-engenharia",
    description=(
        "Papel esperado (projetista/engenheiro mecanico), fluxo obrigatorio, "
        "e qual resource solidworks://knowledge/* ler para cada tipo de "
        "pedido. Leia isto primeiro, antes de qualquer resource especifico."
    ),
    mime_type="text/markdown",
)
def knowledge_index() -> str:
    return _read_claude_file("CLAUDE.md")


_KNOWLEDGE_RESOURCES = [
    ("roteiro-projetista", "roteiro_projetista.md", "roteiro-projetista-mecanico",
     "Fluxo completo de ponta a ponta para projetar qualquer peca a partir de "
     "um pedido ou referencia -- amarra todos os outros resources de "
     "conhecimento em ordem de decisao."),
    ("materiais", "materiais.md", "materiais-de-engenharia",
     "Acos, inox, aluminios e plasticos de engenharia: densidade, "
     "escoamento, ruptura, e regra pratica de selecao por "
     "exposicao/peso/carga. Consulte antes de qualquer set_material."),
    ("tolerancias-e-ajustes", "tolerancias_e_ajustes.md", "tolerancias-e-ajustes-iso",
     "Tolerancia geral ISO 2768, sistema de ajustes ISO (H7/g6 ... H7/s6), "
     "furos de folga padrao para parafuso, acabamento superficial tipico "
     "por processo."),
    ("gdt", "gdt.md", "gdt-tolerancia-geometrica",
     "As 14 caracteristicas de GD&T, quando cada uma se aplica de verdade, "
     "e como montar o datum reference frame."),
    ("elementos-de-maquina", "elementos_de_maquina.md", "elementos-de-maquina-padronizados",
     "Parafusos metricos ISO (furo/broca/chave), rolamentos rigidos de "
     "esferas (serie 6000/6200/6300), chavetas DIN 6885, molas -- nunca "
     "invente essas dimensoes."),
    ("chapa-metalica", "chapa_metalica.md", "chapa-metalica-regras-de-projeto",
     "Raio minimo de dobra por espessura, K-factor, flange minimo, "
     "distancia furo-dobra, sequencia pratica de ferramentas para chapa "
     "dobrada."),
    ("soldas-e-perfis-estruturais", "soldas_e_perfis_estruturais.md", "weldments-e-estruturas-soldadas",
     "Escolha de perfil estrutural por tipo de carga, dimensionamento "
     "rapido de tubo, simbolos de solda, sequencia pratica para weldments."),
    ("processos-de-fabricacao", "processos_de_fabricacao.md", "dfm-por-processo-de-fabricacao",
     "Design for manufacturing por processo: usinagem CNC, corte a laser + "
     "dobra, solda, injecao plastica, fundicao."),
    ("verificacao-e-qa", "verificacao_e_qa.md", "verificacao-e-qa-antes-de-entregar",
     "Checklist real antes de dizer que uma peca esta pronta: reconstrucao "
     "sem erro, massa bate, inspecao visual, interferencia, fabricabilidade, "
     "e o que fazer quando pedem confirmacao de resistencia sem FEA "
     "disponivel."),
    ("montagens-mecanicas-reais", "montagens_mecanicas_reais.md", "montagens-mecanicas-encaixe-real",
     "Encaixe real entre pecas numa montagem: por que validate_model e uma "
     "isometrica solta nao bastam, mate concentric so trava 2 graus de "
     "liberdade, boss so funde com a parede acima de um raio minimo, "
     "convencao linha-vs-coluna da rotation_matrix, e bugs confirmados "
     "(create_reference_plane flip/offset negativo no plano right, "
     "create_sketch falhando em silencio, close_document no documento "
     "ativo errado). Leia antes de montar qualquer par de pecas que se "
     "encaixam (pino, eixo, rosca, mancal)."),
]


def _register_knowledge_resource(uri_slug: str, filename: str, name: str, description: str) -> None:
    def _reader() -> str:
        return _read_claude_file(os.path.join("knowledge", filename))

    mcp.resource(
        f"solidworks://knowledge/{uri_slug}",
        name=name,
        description=description,
        mime_type="text/markdown",
    )(_reader)


for _uri_slug, _filename, _name, _description in _KNOWLEDGE_RESOURCES:
    _register_knowledge_resource(_uri_slug, _filename, _name, _description)


@mcp.prompt(
    name="design_from_reference",
    description=(
        "Roteiro passo a passo para projetar uma peca nova a partir de uma "
        "referencia real (foto, desenho tecnico, catalogo), seguindo o "
        "metodo validado nas replicas de engenharia deste projeto."
    ),
)
def design_from_reference(
    part_description: str,
    key_dimensions: str = "",
    reference_source: str = "",
) -> str:
    """Scaffold the plan/build/verify/inspect loop for a new part."""
    dims_line = (
        f"Dimensoes criticas conhecidas: {key_dimensions}"
        if key_dimensions
        else "Dimensoes criticas: ainda nao levantadas -- extraia-as da "
        "referencia antes de modelar."
    )
    source_line = f"\nReferencia: {reference_source}" if reference_source else ""

    return f"""Projete a seguinte peca usando o metodo de design-sandbox deste servidor:

Peca: {part_description}
{dims_line}{source_line}

Siga esta sequencia, sem pular etapas:

1. PLANEJAR -- antes de chamar qualquer tool, escreva em texto a arvore de
   features pretendida (ex.: sketch base -> extrude 40mm -> corte circular
   passante 10mm -> arredondamento 2mm nas arestas X) e as dimensoes
   criticas que precisam bater com a referencia.
2. ISOLAR -- crie a peca com create_new_part num documento novo e
   descartavel. Nao reaproveite nem edite um documento que o usuario ja
   tinha aberto.
3. CONSTRUIR INCREMENTAL -- execute o plano feature por feature. Prefira
   ferramentas marcadas OK no resource solidworks://tool-status; para
   ferramentas EXP, releia as ressalvas la descritas antes de tentar, e
   tenha um fallback manual pronto.
4. VALIDAR A CADA PASSO -- depois de cada feature estrutural (extrude,
   corte, padrao, chanfro, casca...), chame measure_body e/ou
   validate_model. Nao acumule mais de 2-3 features sem validar; erros
   silenciosos se acumulam e ficam mais dificeis de rastrear depois.
5. INSPECIONAR VISUALMENTE -- chame capture_standard_views (ou
   zoom_to_fit + set_view) e observe a imagem retornada. Compare
   proporcao e silhueta com a referencia/descricao antes de seguir para a
   proxima etapa do plano.
6. COMPARAR E ITERAR -- se a geometria nao bater com a referencia
   (proporcao, posicao de furos, simetria), corrija a feature responsavel
   antes de avancar; nao tente compensar num passo posterior.
7. FINALIZAR -- so depois de validado sem erros/avisos e com a inspecao
   visual aprovada, salve o resultado e reporte ao usuario as dimensoes
   finais medidas (measure_body) comparadas com as dimensoes-alvo.
"""


if __name__ == "__main__":
    mcp.run()
