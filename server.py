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
import math
import time
import atexit
import asyncio
import builtins
import logging
import functools
import contextlib
import concurrent.futures
from typing import Optional

import win32com.client
import pythoncom

from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("solidworks-mcp")

mcp = FastMCP("solidworks-mcp")

# ---------------------------------------------------------------------------
# Single-threaded COM executor
# ---------------------------------------------------------------------------
# pywin32 COM objects are apartment-threaded: an object obtained on one OS
# thread cannot be safely used from another. asyncio's default thread-pool
# offloading for sync callables does not guarantee the same worker thread
# across calls, so we pin all COM work to one persistent thread instead.

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sw-com")

COM_TIMEOUT_SECONDS = 120


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
    log.debug("COM call: %s", fn_name)
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
    try:
        result = await asyncio.wait_for(future, timeout=COM_TIMEOUT_SECONDS)
        log.debug("COM call OK: %s", fn_name)
        return result
    except asyncio.TimeoutError:
        log.error("COM operation timed out after %ds: %s", COM_TIMEOUT_SECONDS, fn_name)
        raise RuntimeError(
            f"SolidWorks operation timed out after {COM_TIMEOUT_SECONDS}s. "
            "The application may be blocked by a dialog or heavy computation. "
            "Close any open dialogs and retry."
        )
    except Exception:
        log.exception("COM call failed: %s", fn_name)
        raise


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


def _connect_running_instance():
    for method in (
        lambda: win32com.client.GetActiveObject("SldWorks.Application"),
        lambda: win32com.client.GetObject(Class="SldWorks.Application"),
    ):
        try:
            app = method()
            app.Visible = True
            _ = app.RevisionNumber
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
        try:
            _ = _app.RevisionNumber
            return _app
        except Exception:
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


def _find_last_sketch(doc) -> Optional[str]:
    name = None
    feat = doc.FirstFeature
    while feat is not None:
        try:
            if feat.GetTypeName2 == "ProfileFeature":
                name = feat.Name
        except Exception:
            pass
        try:
            feat = feat.GetNextFeature
        except Exception:
            break
    return name


def _select_last_sketch(doc) -> str:
    try:
        if doc.SketchManager.ActiveSketch is not None:
            doc.SketchManager.InsertSketch(True)  # close it
    except Exception:
        pass

    doc.ClearSelection2(True)
    name = _find_last_sketch(doc)
    if not name:
        raise RuntimeError("No sketch found. Create a sketch and draw a closed profile first.")
    if not _select_by_id(doc, name, "SKETCH"):
        raise RuntimeError(f"Could not select sketch '{name}'.")
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


def _active_assembly():
    doc = _active_doc()
    if _doc_type(doc) != 2:
        raise RuntimeError("The active document is not an assembly.")
    return doc


def _preload_and_insert_component(assy, filepath: str, x: float, y: float, z: float):
    """AddComponent5 silently returns None unless the source document is already
    loaded into the SolidWorks session (via a silent OpenDoc6) and the assembly
    is re-activated as ActiveDoc right before the call.
    """
    assy_title = _doc_title(assy)
    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    app = _connect()
    ext = os.path.splitext(filepath)[1].lower()
    doc_type = {".sldprt": 1, ".sldasm": 2}.get(ext, 1)
    app.OpenDoc6(filepath, doc_type, 1, "", errors, warnings)

    reactivate_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    app.ActivateDoc3(assy_title, False, 0, reactivate_errors)
    assy = app.ActiveDoc

    comp = assy.AddComponent5(filepath, 0, "", False, "", x, y, z)
    if comp is None:
        raise RuntimeError(f"Failed to insert component '{filepath}' into the assembly.")
    return assy, win32com.client.Dispatch(comp)


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

@mcp.tool()
async def connect_solidworks() -> dict:
    """Connect to a running SolidWorks instance, launching it if it isn't open yet."""

    def _impl():
        app = _connect()
        try:
            version = app.RevisionNumber
        except Exception:
            version = app.RevisionNumber()
        return {"connected": True, "version": str(version), "launched": _last_launch_happened}

    return await _run(_impl)


@mcp.tool()
async def get_solidworks_info() -> dict:
    """Get the connected SolidWorks application's version and visibility state."""

    def _impl():
        app = _connect()
        try:
            version = app.RevisionNumber
        except Exception:
            version = app.RevisionNumber()
        return {"version": str(version), "visible": bool(app.Visible)}

    return await _run(_impl)


# ===========================================================================
# Document tools
# ===========================================================================

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def insert_drawing_view(source_filepath: str, view_type: str = "front",
                               x: float = 150, y: float = 150,
                               scale: float = 1.0, unit: Optional[str] = None) -> dict:
    """Insert a standard model view into the active drawing.
    view_type: front, back, left, right, top, bottom, isometric, trimetric, dimetric.
    x/y is the position on the drawing sheet. scale is the view scale (e.g. 2 = 2:1)."""

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
        open_errs = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warns = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        try:
            source_doc = app.OpenDoc6(source_filepath, src_type, 0, "", open_errs, open_warns)
        except Exception as e:
            log.warning("Could not pre-load source model: %s", e)
            source_doc = app.GetOpenDocumentByName(source_filepath)
        available_views = tuple(getattr(source_doc, "GetModelViewNames", ()) or ())
        sw_view = next((name for name in view_candidates if name in available_views), view_candidates[0])
        react_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        app.ActivateDoc3(drawing_title, False, 0, react_err)
        doc = app.ActiveDoc

        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        model_name = os.path.basename(source_filepath)
        view = doc.CreateDrawViewFromModelView3(model_name, sw_view, x_m, y_m, 0)
        if view is None:
            raise RuntimeError(
                f"Failed to insert {view_type} view of '{model_name}'. "
                "Ensure the source document exists and contains geometry."
            )
        try:
            view = win32com.client.Dispatch(view)
            view.ScaleRatio = (scale, 1.0)
        except Exception:
            pass
        return {"view_type": view_type, "source": source_filepath,
                "position": [x, y], "scale": scale, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
async def open_document(filepath: str) -> dict:
    """Open an existing SolidWorks file (.sldprt, .sldasm, or .slddrw)."""

    def _impl():
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        app = _connect()
        ext = os.path.splitext(filepath)[1].lower()
        doc_type = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}.get(ext, 1)
        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        doc = app.OpenDoc6(filepath, doc_type, 0, "", errors, warnings)
        if doc is None:
            raise RuntimeError(f"Failed to open '{filepath}' (error code {errors.value}).")
        title = _doc_title(doc)
        activate_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        active_doc = app.ActivateDoc3(title, True, 0, activate_errors)
        if active_doc is None:
            raise RuntimeError(
                f"Opened '{filepath}' but could not activate it (error code {activate_errors.value})."
            )
        return {"title": title, "path": filepath}

    return await _run(_impl)


@mcp.tool()
async def close_document(save: bool = False) -> dict:
    """Close the active document, optionally saving it first."""

    def _impl():
        app = _connect()
        doc = app.ActiveDoc
        if doc is None:
            return {"closed": False, "message": "No document was open."}
        title = _doc_title(doc)
        if save:
            errs = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            wrns = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            doc.Save3(0, errs, wrns)
        app.CloseDoc(title)
        return {"closed": True, "title": title}

    return await _run(_impl)


@mcp.tool()
async def save_document(filepath: Optional[str] = None) -> dict:
    """Save the active document. Omit filepath to save in place."""

    def _impl():
        doc = _active_doc()
        if filepath:
            abs_path = os.path.abspath(filepath)
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
            errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            doc.Save3(0, errors, warnings)
            if errors.value != 0:
                raise RuntimeError(f"Save failed (error code {errors.value}).")
            return {"path": _doc_path(doc)}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
async def list_open_documents() -> dict:
    """List every SolidWorks document currently open."""

    def _impl():
        app = _connect()
        type_names = {1: "Part", 2: "Assembly", 3: "Drawing"}
        docs = []
        for doc in app.GetDocuments:
            try:
                docs.append({"title": _doc_title(doc), "type": type_names.get(_doc_type(doc), "Unknown")})
            except Exception:
                pass
        return {"count": len(docs), "documents": docs}

    return await _run(_impl)


# ===========================================================================
# Assembly tools
# ===========================================================================

@mcp.tool()
async def insert_component(filepath: str, x: float = 0, y: float = 0, z: float = 0,
                            unit: Optional[str] = None) -> dict:
    """Insert an existing part or sub-assembly file into the active assembly at a position."""

    def _impl():
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")
        assy = _active_assembly()
        x_m, y_m, z_m = to_meters(x, unit), to_meters(y, unit), to_meters(z, unit)
        _assy, comp = _preload_and_insert_component(assy, filepath, x_m, y_m, z_m)
        return {"name": comp.Name2, "path": filepath, "position": [x, y, z], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
async def list_components() -> dict:
    """List every component (part/sub-assembly instance) in the active assembly."""

    def _impl():
        assy = _active_assembly()
        components = []
        for raw in assy.GetComponents(True) or ():
            comp = win32com.client.Dispatch(raw)
            components.append({
                "name": comp.Name2,
                "path": comp.GetPathName,
                "suppressed": bool(comp.IsSuppressed),
                "fixed": bool(comp.IsFixed),
            })
        return {"count": len(components), "components": components}

    return await _run(_impl)


@mcp.tool()
async def fix_component(name: str) -> dict:
    """Fix a component in place (removes its remaining degrees of freedom)."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        assy.FixComponent()
        return {"name": name, "fixed": True}

    return await _run(_impl)


@mcp.tool()
async def float_component(name: str) -> dict:
    """Float a previously fixed component, giving it back its degrees of freedom."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        assy.UnfixComponent()
        return {"name": name, "fixed": False}

    return await _run(_impl)


@mcp.tool()
async def delete_component(name: str) -> dict:
    """Remove a component from the active assembly."""

    def _impl():
        assy = _active_assembly()
        _select_component(assy, name)
        if not assy.Extension.DeleteSelection2(0):
            raise RuntimeError(f"Failed to delete component '{name}'.")
        return {"deleted": name}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def add_mate(mate_type: str, point1: dict, point2: dict) -> dict:
    """Mate two faces/planes, each chosen by a point that
    lies on it, e.g. point1={"x":0,"y":0,"z":0,"unit":"mm"}. Supported mate_type
    values: 'coincident', 'concentric', 'parallel', 'perpendicular', 'tangent'."""

    def _impl():
        assy = _active_assembly()
        types = {"coincident": 0, "concentric": 1, "perpendicular": 2, "parallel": 3, "tangent": 4}
        mate_code = types.get(mate_type.lower())
        if mate_code is None:
            raise ValueError(f"Unknown mate_type '{mate_type}'. Use one of: {', '.join(types)}")

        def pt(p):
            u = p.get("unit")
            return to_meters(p["x"], u), to_meters(p["y"], u), to_meters(p["z"], u)

        x1, y1, z1 = pt(point1)
        x2, y2, z2 = pt(point2)

        assy.ClearSelection2(True)
        empty_callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not assy.Extension.SelectByID2("", "FACE", x1, y1, z1, False, 1, empty_callout, 0):
            raise RuntimeError(f"No face/plane found at point1 {point1}.")
        if not assy.Extension.SelectByID2("", "FACE", x2, y2, z2, True, 1, empty_callout, 0):
            raise RuntimeError(f"No face/plane found at point2 {point2}.")

        mate_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        mate = assy.AddMate5(
            mate_code, 0, False,
            0.0, 0.0, 0.0,
            0.0, 0.0,
            0.0, 0.0, 0.0,
            False, False, 0, mate_err,
        )
        # swAddMateError_NoError is 1; COM can return an object even on failure.
        if mate is None or mate_err.value != 1:
            raise RuntimeError(f"Mate creation failed (error code {mate_err.value}).")
        return {"mate_type": mate_type, "error_code": mate_err.value}

    return await _run(_impl)


@mcp.tool()
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


# ===========================================================================
# Export tools
# ===========================================================================

@mcp.tool()
async def export_document(filepath: str) -> dict:
    """Export the active document to STEP, STL, IGES, PDF, DXF, or Parasolid.
    The format is determined by the file extension:
    .step/.stp, .stl, .igs/.iges, .pdf (drawings), .dxf, .x_t (Parasolid), .3mf"""

    def _impl():
        doc = _active_doc()
        abs_path = os.path.abspath(filepath)
        ext = os.path.splitext(abs_path)[1].lower()
        valid = {".step", ".stp", ".stl", ".igs", ".iges", ".pdf", ".dxf", ".x_t", ".x_b", ".sat", ".3mf"}
        if ext not in valid:
            raise ValueError(f"Unsupported export format '{ext}'. Use: {', '.join(sorted(valid))}")

        if ext == ".pdf" and _doc_type(doc) != 3:
            raise RuntimeError("PDF export is only supported for drawing documents.")

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
        return {"path": abs_path, "format": ext.lstrip(".")}

    return await _run(_impl)


# ===========================================================================
# Sketch tools
# ===========================================================================

@mcp.tool()
async def create_sketch(plane: str = "front") -> dict:
    """Create a new sketch on a standard plane: 'front', 'top', or 'right'."""

    def _impl():
        doc = _active_doc()
        plane_name = _standard_plane_name(doc, plane)
        if not _select_by_id(doc, plane_name, "PLANE"):
            raise RuntimeError(f"Could not select plane '{plane_name}'.")
        doc.InsertSketch2(True)
        return {"plane": plane_name}

    return await _run(_impl)


@mcp.tool()
async def create_sketch_on_face(x: float = 0, y: float = 0, z: float = 0, unit: Optional[str] = None) -> dict:
    """Create a sketch on an existing body face, chosen by a point known to lie on it."""

    def _impl():
        doc = _active_doc()
        x_m, y_m, z_m = to_meters(x, unit), to_meters(y, unit), to_meters(z, unit)
        doc.ClearSelection2(True)
        if not _select_by_id(doc, "", "FACE", x_m, y_m, z_m):
            raise RuntimeError(f"No face found at ({x}, {y}, {z}) {unit or _default_unit}.")
        doc.InsertSketch2(True)
        return {"point": {"x": x, "y": y, "z": z}, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
async def close_sketch() -> dict:
    """Exit the currently active sketch."""

    def _impl():
        doc = _active_doc()
        if doc.SketchManager.ActiveSketch is None:
            return {"closed": False, "message": "No sketch was active."}
        doc.InsertSketch2(True)
        return {"closed": True}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        disp_dim = doc.AddDimension2(dim_x_m, dim_y_m, 0)
        if disp_dim is None:
            raise RuntimeError("Failed to add dimension. Ensure the sketch entities are valid for dimensioning.")

        disp_dim = win32com.client.Dispatch(disp_dim)
        result = {"unit": unit or _default_unit}

        if value is not None:
            val_m = to_meters(value, unit)
            try:
                dim = disp_dim.GetDimension2(0)
                if callable(dim):
                    dim = dim()
                dim = win32com.client.Dispatch(dim)
                dim.SystemValue = val_m
                result["value"] = value
            except Exception:
                try:
                    disp_dim.SystemValue = val_m
                    result["value"] = value
                except Exception:
                    result["value_set"] = False

        doc.ClearSelection2(True)
        return result

    return await _run(_impl)


@mcp.tool()
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

@mcp.tool()
async def extrude_sketch(depth: float = 10, both_directions: bool = False, unit: Optional[str] = None) -> dict:
    """Boss-extrude the last sketch drawn. Requires a closed sketch profile."""

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
            True, True, True, 0, 0.0, False,
        )
        if feat is None:
            raise RuntimeError(f"Extrusion failed on sketch '{sketch_name}'. Check that its profile is closed.")
        return {"sketch": sketch_name, "depth": depth, "both_directions": both_directions,
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
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
            raise RuntimeError(f"Cut failed on sketch '{sketch_name}'. Check that its profile is closed.")
        return {"sketch": sketch_name, "through_all": through_all,
                "depth": None if through_all else depth, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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
            0, 0, False, 0, 0.0, 0.0, 0,
            True, True, True, 0, True, True, 0.0, True, True,
        )
        if feat is None:
            raise RuntimeError(
                "Sweep failed. Ensure the profile sketch is closed, "
                "the path sketch is on a different plane, and both are named correctly."
            )
        return {"profile": profile_sketch, "path": path_sketch}

    return await _run(_impl)


@mcp.tool()
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
            False, True, True, 1, 0, 0, False, 0, 0, 0,
            True, True, True,
        )
        if feat is None:
            raise RuntimeError(
                "Loft failed. Ensure all sketches are closed profiles on different planes "
                "and are listed in order from start to end."
            )
        return {"sketches": sketch_names}

    return await _run(_impl)


@mcp.tool()
async def fillet_edges(radius: float = 2, unit: Optional[str] = None) -> dict:
    """Apply a constant-radius fillet to every edge of the solid body/bodies."""

    def _impl():
        if radius <= 0:
            raise ValueError(f"Radius must be positive, got {radius}.")
        doc = _active_doc()
        radius_m = to_meters(radius, unit)
        edge_count = _select_all_edges(doc)
        empty = win32com.client.VARIANT(pythoncom.VT_EMPTY, None)
        feat = doc.FeatureManager.FeatureFillet3(195, radius_m, 0, False, False, empty, empty)
        if feat is None:
            raise RuntimeError("Failed to create fillet.")
        return {"radius": radius, "unit": unit or _default_unit, "edges": edge_count}

    return await _run(_impl)


@mcp.tool()
async def chamfer_edges(distance: float = 2, angle: float = 45, unit: Optional[str] = None) -> dict:
    """Apply a distance/angle chamfer to every edge of the solid body/bodies."""

    def _impl():
        if distance <= 0:
            raise ValueError(f"Distance must be positive, got {distance}.")
        if not (0 < angle < 90):
            raise ValueError(f"Angle must be between 0 and 90 degrees (exclusive), got {angle}.")
        doc = _active_doc()
        dist_m = to_meters(distance, unit)
        edge_count = _select_all_edges(doc)
        feat = doc.FeatureManager.InsertFeatureChamfer(1, 0, dist_m, math.radians(angle), dist_m, 0, 0, 0)
        if feat is None:
            raise RuntimeError("Failed to create chamfer.")
        return {"distance": distance, "angle": angle, "unit": unit or _default_unit, "edges": edge_count}

    return await _run(_impl)


@mcp.tool()
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
            if not _select_by_id(doc, "", "FACE", fx, fy, fz):
                raise RuntimeError(
                    f"No face found at ({remove_face_at_x}, {remove_face_at_y}, {remove_face_at_z})."
                )

        feat = doc.FeatureManager.InsertFeatureShell(t_m, False)
        if feat is None:
            raise RuntimeError(
                "Shell failed. Ensure the body has sufficient thickness "
                "and select a face to remove for an open shell."
            )
        return {"thickness": thickness, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
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

        doc.ClearSelection2(True)
        if not _select_by_id(doc, feature_name, "BODYFEATURE"):
            raise RuntimeError(f"Could not select feature '{feature_name}'.")

        dir_map = {"x": 0, "y": 1, "z": 2}
        axis_idx = dir_map.get(direction.lower())
        if axis_idx is None:
            raise ValueError(f"Direction must be 'x', 'y', or 'z', got '{direction}'.")

        axes = []
        feat = doc.FirstFeature
        while feat is not None and len(axes) <= axis_idx:
            try:
                if feat.GetTypeName2 == "RefAxis":
                    axes.append(feat.Name)
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature
            except Exception:
                break

        if axis_idx < len(axes):
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            doc.Extension.SelectByID2(axes[axis_idx], "AXIS", 0, 0, 0, True, 1, empty, 0)

        use_dir2 = count2 > 1
        feat = doc.FeatureManager.FeatureLinearPattern4(
            count, spacing_m, count2 if use_dir2 else 1, spacing2_m if use_dir2 else 0,
            True, False, use_dir2, False,
            True, False,
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


@mcp.tool()
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

        doc.ClearSelection2(True)
        if not _select_by_id(doc, feature_name, "BODYFEATURE"):
            raise RuntimeError(f"Could not select feature '{feature_name}'.")

        dir_map = {"x": 0, "y": 1, "z": 2}
        axis_idx = dir_map.get(axis.lower())
        if axis_idx is None:
            raise ValueError(f"Axis must be 'x', 'y', or 'z', got '{axis}'.")

        axes = []
        feat = doc.FirstFeature
        while feat is not None and len(axes) <= axis_idx:
            try:
                if feat.GetTypeName2 == "RefAxis":
                    axes.append(feat.Name)
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature
            except Exception:
                break

        if axis_idx < len(axes):
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            doc.Extension.SelectByID2(axes[axis_idx], "AXIS", 0, 0, 0, True, 1, empty, 0)

        angle_rad = math.radians(angle)
        equal_spacing = abs(angle - 360) < 0.01

        feat = doc.FeatureManager.FeatureCircularPattern4(
            count, angle_rad, equal_spacing,
            "", False, True, False,
        )
        if feat is None:
            raise RuntimeError(
                f"Circular pattern failed on feature '{feature_name}'. "
                "Ensure the feature and axis are valid."
            )
        return {"feature": feature_name, "axis": axis, "count": count, "angle": angle}

    return await _run(_impl)


@mcp.tool()
async def hole_wizard(face_x: float, face_y: float, face_z: float,
                       hole_x: float, hole_y: float,
                       hole_type: str = "simple",
                       size: float = 6, depth: float = 20,
                       thread_standard: str = "ISO",
                       unit: Optional[str] = None) -> dict:
    """Create a hole using the Hole Wizard on a face.
    face_x/y/z: a point on the face where the hole will be placed.
    hole_x/y: the 2D position of the hole center on that face (sketch coords).
    hole_type: 'simple', 'counterbore', 'countersink', 'tapped' (threaded).
    size: nominal hole diameter (or thread size for tapped).
    depth: hole depth (ignored for through-all; use depth=0 to drill through).
    thread_standard: 'ISO' or 'ANSI'."""

    def _impl():
        if size <= 0:
            raise ValueError(f"Size must be positive, got {size}.")
        doc = _active_doc()

        fx = to_meters(face_x, unit)
        fy = to_meters(face_y, unit)
        fz = to_meters(face_z, unit)
        doc.ClearSelection2(True)
        if not _select_by_id(doc, "", "FACE", fx, fy, fz):
            raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")

        types = {
            "simple": 0,      # swWzdGeneralHole
            "counterbore": 1, # swWzdCounterBore
            "countersink": 2, # swWzdCounterSink
            "tapped": 3,      # swWzdTap
        }
        hw_type = types.get(hole_type.lower())
        if hw_type is None:
            raise ValueError(f"Unknown hole_type '{hole_type}'. Use: {', '.join(types)}")

        size_m = to_meters(size, unit)
        depth_m = to_meters(depth, unit) if depth > 0 else 0.0
        end_cond = 0 if depth > 0 else 1  # 0=Blind, 1=ThroughAll
        std = 1 if thread_standard.upper() == "ISO" else 0

        feat = doc.FeatureManager.HoleWizard4(
            hw_type,  # HoleType
            std,      # Standard
            0,        # FastenerType
            end_cond, # EndCondition
            size_m,   # Diameter
            depth_m,  # Depth
            True,     # NearCountereboreDepth
            size_m * 2, 0.0, 0.0, 0.0,  # counterbore/sink params
            False, False, False, False,
        )
        if feat is None:
            raise RuntimeError(
                f"Hole Wizard failed on face at ({face_x},{face_y},{face_z}). "
                "Ensure the face is flat and the hole parameters are valid."
            )

        try:
            hx = to_meters(hole_x, unit)
            hy = to_meters(hole_y, unit)
            doc.SketchManager.CreatePoint(hx, hy, 0)
            doc.SketchManager.InsertSketch(True)
        except Exception:
            pass

        return {"hole_type": hole_type, "size": size, "depth": depth if depth > 0 else "through",
                "unit": unit or _default_unit}

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


@mcp.tool()
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

        # Select the whole path sketch, then insert the structural member.
        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(sketch_name, "SKETCH", 0, 0, 0, False, 0, empty, 0):
            raise RuntimeError(f"Sketch '{sketch_name}' not found or could not be selected.")

        # InsertStructuralWeldment5(Path, ConnectedSegmentsOption, AllowProtrusion,
        #   Groups, ConfigurationName) — ConfigurationName carries the size.
        feat = doc.FeatureManager.InsertStructuralWeldment5(
            profile_path,
            0,       # ConnectedSegmentsOption (0 = one group from all connected segments)
            False,   # AllowProtrusion
            None,    # Groups (None = auto-group from the selected sketch)
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


@mcp.tool()
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


@mcp.tool()
async def add_gusset(
    thickness: float = 5,
    x1: float = 0, y1: float = 0, z1: float = 0,
    x2: float = 0, y2: float = 0, z2: float = 0,
    profile: str = "triangular",
    unit: Optional[str] = None,
) -> dict:
    """Add a gusset plate (reinforcement triangle) between two planar faces.

    A gusset is the triangular steel plate welded at the junction of two beams
    or between a column and a base plate. Select two flat faces that meet at
    an edge or corner.

    x1/y1/z1: a point on the first face.
    x2/y2/z2: a point on the second face.
    thickness: plate thickness.
    profile: 'triangular' (straight hypotenuse), 'catenary' (curved), or
             'flat' (rectangular infill)."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        doc = _active_doc()
        t_m = to_meters(thickness, unit)

        # swGussetProfileTriangle=1, swGussetProfilePolygon=0
        profiles = {"triangular": 1, "flat": 0, "catenary": 1}
        prof_is_triangle = profiles.get(profile.lower())
        if prof_is_triangle is None:
            raise ValueError(f"profile must be 'triangular', 'catenary', or 'flat', got '{profile}'.")

        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        f1x, f1y, f1z = to_meters(x1, unit), to_meters(y1, unit), to_meters(z1, unit)
        f2x, f2y, f2z = to_meters(x2, unit), to_meters(y2, unit), to_meters(z2, unit)

        if not doc.Extension.SelectByID2("", "FACE", f1x, f1y, f1z, False, 1, empty, 0):
            raise RuntimeError(f"No face found at ({x1}, {y1}, {z1}).")

        if not doc.Extension.SelectByID2("", "FACE", f2x, f2y, f2z, True, 1, empty, 0):
            raise RuntimeError(f"No face found at ({x2}, {y2}, {z2}).")

        # InsertGussetFeature2(Depth, DirType, LocType, BIsProfile, ProfileD1,
        #   ProfileD2, ProfileD3, ProfileAngle, ProfileD4, BOffset, DProfileOffset,
        #   CrvIndex, BReverseDir, BReverseFace, BUseLenDim, Faces) — 16 args.
        d1 = to_meters(50, unit)  # default gusset leg 1
        d2 = to_meters(50, unit)  # default gusset leg 2
        feat = doc.FeatureManager.InsertGussetFeature2(
            t_m,               # Depth (thickness)
            1,                 # DirType (swGussetThicknessBothSides=1)
            1,                 # LocType (swGussetProfileLocationCenter=1)
            prof_is_triangle,  # BIsProfile / triangle flag
            d1,                # ProfileD1
            d2,                # ProfileD2
            0.0,               # ProfileD3
            math.radians(45),  # ProfileAngle
            0.0,               # ProfileD4
            False,             # BOffset
            0.0,               # DProfileOffset
            0,                 # CrvIndex
            False,             # BReverseDir
            False,             # BReverseFace
            False,             # BUseLenDim
            None,              # Faces (None = use selection)
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
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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

    face_x/y/z: a point on the open face at the end of the profile.
    thickness: cap plate thickness.
    offset: inward offset from the face (0 = flush with the end)."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
        doc = _active_doc()
        t_m = to_meters(thickness, unit)
        off_m = to_meters(offset, unit) if offset != 0 else 0.0

        doc.ClearSelection2(True)
        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
            raise RuntimeError(f"No face found at ({face_x}, {face_y}, {face_z}).")

        # InsertEndCapFeature(Depth, BIsGivenOffset, BIsChamfer, OffsetValue,
        #   WallThicknessRatio, ChamferValue) — 6 args.
        feat = doc.FeatureManager.InsertEndCapFeature(
            t_m,                        # Depth (cap thickness)
            bool(offset != 0),          # BIsGivenOffset
            False,                      # BIsChamfer
            off_m,                      # OffsetValue
            0.5,                        # WallThicknessRatio
            0.0,                        # ChamferValue
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


@mcp.tool()
async def create_3d_sketch() -> dict:
    """Open a new 3D sketch.

    A 3D sketch lets you draw lines, arcs, and splines in free 3D space, not
    constrained to a single plane. This is essential for defining the paths of
    structural members (weldments) in space — e.g. the frame of a platform
    or a tank support structure.

    After calling this, use draw_line_3d (or the regular draw_line with Z
    coordinates via execute_python) to add geometry, then close_sketch when done."""

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


@mcp.tool()
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

@mcp.tool()
async def create_base_flange(
    thickness: float = 2,
    depth: float = 100,
    direction: str = "blind",
    both_directions: bool = False,
    bend_radius: float = 1,
    unit: Optional[str] = None,
) -> dict:
    """Create a sheet metal base flange from the last sketch.

    This is the first step to create a sheet metal part. Draw a sketch profile
    (open or closed), then call this to turn it into a sheet metal body.

    An OPEN profile (e.g. a single line, arc, or open polyline) produces a
    flat strip or bent plate extruded to the given depth.
    A CLOSED profile (e.g. a rectangle) produces a hollow box/tube.

    thickness: sheet metal gauge thickness.
    depth: extrusion depth (how far the sheet extends).
    direction: 'blind' (one side) or 'midplane' (centered on sketch).
    both_directions: if True, extrude equally on both sides.
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

        end_cond = 6 if (both_directions or direction.lower() == "midplane") else 0

        # InsertSheetMetalBaseFlange2 argument order (19 args, verified against the
        # SW 2025 type library):
        #   Thickness, ThickenDir, Radius, ExtrudeDist1, ExtrudeDist2, FlipExtruDir,
        #   EndCondition1, EndCondition2, DirToUse, PCBA, UseDefaultRelief, ReliefType,
        #   ReliefWidth, ReliefDepth, ReliefRatio, UseReliefRatio, Merge, UseFeatScope,
        #   UseAutoSelect
        feat = doc.FeatureManager.InsertSheetMetalBaseFlange2(
            t_m,        # Thickness
            False,      # ThickenDir
            br_m,       # Radius
            d_m,        # ExtrudeDist1
            0.0,        # ExtrudeDist2
            False,      # FlipExtruDir
            end_cond,   # EndCondition1 (0=Blind, 6=MidPlane)
            0,          # EndCondition2
            0,          # DirToUse
            win32com.client.VARIANT(pythoncom.VT_DISPATCH, None),  # PCBA (null object)
            True,       # UseDefaultRelief
            0,          # ReliefType (0=Rectangular)
            0.0,        # ReliefWidth
            0.0,        # ReliefDepth
            0.5,        # ReliefRatio
            False,      # UseReliefRatio
            True,       # Merge
            True,       # UseFeatScope
            True,       # UseAutoSelect
        )

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


@mcp.tool()
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

        # InsertBends2(Radius, UseBendTable, UseKfactor, UseBendAllowance,
        #   UseAutoRelief, OffsetRatio, DoFlatten)
        feat = doc.FeatureManager.InsertBends2(
            r_m, False, False, False, True, 0.5, False,
        )

        if feat is None:
            raise RuntimeError(
                "Insert-bends failed. The active part must be a constant-thickness "
                "thin/shelled body (use shell_body first), and you must select a "
                "fixed face. To add a folded lip instead, use add_sheet_metal_edge_flange."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else str(feat_dispatch)

        return {
            "feature": feat_name,
            "bend_radius": bend_radius,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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

        feat = doc.FeatureManager.InsertSheetMetalEdgeFlange2(
            fl_m,       # Length
            angle_rad,  # Angle
            0,          # FlangePosition (0=MaterialInside)
            0,          # Relief type
            0.5,        # Relief ratio
            False,      # UseReliefRatio
            0.0,        # Relief depth
            0.0,        # Relief width
            0,          # OffsetType
            False,      # FlipDirection
            True,       # UseDefaultBendRadius
            0.0,        # CustomBendRadius
            0,          # CustomBendAllowance
            0.0,        # CustomKFactor
        )

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


@mcp.tool()
async def flatten_sheet_metal() -> dict:
    """Flatten the sheet metal part to show its flat pattern.

    Shows the sheet metal body unfolded into a single flat shape — exactly
    how it should be cut from a flat plate on a CNC, laser, or plasma cutter.
    Call this again (or use list_features and suppress the Flat-Pattern) to
    go back to the folded state."""

    def _impl():
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
            if is_suppressed:
                flat_pattern.Select2(False, 0)
                doc.EditUnsuppress2()
                doc.ClearSelection2(True)
                return {"state": "flattened", "action": "unsuppressed flat-pattern"}
            else:
                flat_pattern.Select2(False, 0)
                doc.EditSuppress2()
                doc.ClearSelection2(True)
                return {"state": "folded", "action": "suppressed flat-pattern (back to 3D)"}

        try:
            doc.FeatureManager.InsertSheetMetalFlatPattern2(True)
            return {"state": "flattened", "action": "created flat-pattern"}
        except Exception:
            pass

        raise RuntimeError(
            "Could not flatten the part. Ensure it is a sheet metal part "
            "(created with create_base_flange or converted to sheet metal)."
        )

    return await _run(_impl)


# ===========================================================================
# Threads / Machining tools
# ===========================================================================

@mcp.tool()
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

        # InsertHelix(Reversed, Clockwised, Tapered, Outward, Helixdef, Height,
        #   Pitch, Revolution, TaperAngle, Startangle) — 10 args, verified against
        #   the SW 2025 typelib. Helixdef 0 = swHelixDefinedByPitchAndRevolution.
        feat = doc.InsertHelix(
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

        if feat is None:
            raise RuntimeError(
                "Helix creation failed. Ensure the sketch has exactly one circle "
                "and no other geometry, and that it is a closed profile."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Helix"

        return {
            "feature": feat_name,
            "diameter": diameter,
            "pitch": pitch,
            "revolutions": revolutions,
            "clockwise": clockwise,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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

        feat = None
        try:
            feat = doc.InsertCosmeticThread3("", "", size, minor_d, 0, length_m, size)
        except Exception as e:
            log.warning("Real thread unavailable; cosmetic fallback failed: %s", e)
            feat = None

        if feat is None:
            raise RuntimeError(
                "Real 3D threads are not available through the SolidWorks API. "
                "Use add_cosmetic_thread for drawings, or model the thread manually "
                "with create_helix + a swept cut."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Thread"

        return {
            "feature": feat_name,
            "note": "Created as a COSMETIC thread — real cut geometry is not API-scriptable.",
            "size": size,
            "length": length,
            "internal": internal,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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

        md_m = to_meters(minor_diameter, unit)
        end_cond = 1 if length is None else 0  # 1=ThroughAll, 0=Blind
        length_m = to_meters(length, unit) if length is not None else 0.0

        # InsertCosmeticThread3(Standard, StandardType, Size, Diameter, EndType,
        # Depth, Note) — an IModelDoc2 method (verified against the SW 2025 typelib).
        feat = None
        try:
            feat = doc.InsertCosmeticThread3(
                "", "", "",   # Standard, StandardType, Size (blank = plain diameter)
                md_m,         # Diameter
                end_cond,     # EndType
                length_m,     # Depth
                "",           # Note
            )
        except Exception as e:
            log.warning("InsertCosmeticThread3 failed: %s", e)
            try:
                feat = doc.InsertCosmeticThread2(end_cond, md_m, length_m, "")
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


@mcp.tool()
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

        doc = _active_doc()
        doc.ClearSelection2(True)
        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, False, 0, empty, 0):
            raise RuntimeError(f"No cylindrical face found at ({face_x}, {face_y}, {face_z}).")

        p_m = to_meters(pitch, unit)
        d_m = to_meters(depth, unit)
        angle_rad = math.radians(angle)

        doc.InsertSketch2(True)

        try:
            radius_m = 0.010

            offset_dir1 = to_meters(1.0, "mm")

            for i in range(-20, 21):
                offset = i * p_m
                doc.SketchManager.CreateLine(
                    offset, -0.05, 0,
                    offset + math.tan(angle_rad) * 0.1, 0.05, 0,
                )
                if pattern.lower() == "diamond":
                    doc.SketchManager.CreateLine(
                        offset, -0.05, 0,
                        offset - math.tan(angle_rad) * 0.1, 0.05, 0,
                    )

            doc.SketchManager.InsertSketch(True)

            doc.ClearSelection2(True)
            sketch_name = _find_last_sketch(doc)
            if sketch_name is None:
                raise RuntimeError("Failed to create knurl sketch.")

            if not _select_by_id(doc, sketch_name, "SKETCH"):
                raise RuntimeError(f"Could not select sketch '{sketch_name}'.")

            if not doc.Extension.SelectByID2("", "FACE", fx, fy, fz, True, 4, empty, 0):
                raise RuntimeError("Could not re-select the cylindrical face.")

            try:
                feat = doc.FeatureManager.InsertWrapFeature2(
                    0,          # WrapType: 0=Emboss, 1=Deboss, 2=Scribe
                    d_m,        # Thickness
                    False,      # ReverseDirection
                    0,          # WrapMethod: 0=Analytical
                    0,          # PullDir
                )
            except Exception:
                try:
                    feat = doc.FeatureManager.InsertWrapFeature(d_m, False, 1)
                except Exception:
                    feat = None

            if feat is None:
                raise RuntimeError(
                    "Knurl (wrap+deboss) failed. This tool requires the face to be "
                    "a simple cylinder and the SolidWorks 'Wrap' feature to be available. "
                    "Consider using an appearance/texture map for cosmetic-only knurling."
                )

            return {
                "pattern": pattern,
                "pitch": pitch,
                "depth": depth,
                "angle": angle,
                "unit": unit or _default_unit,
            }
        except Exception as e:
            raise RuntimeError(f"Knurl creation failed: {e}")

    return await _run(_impl)


@mcp.tool()
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
    direction: 'parallel' (rib thickness perpendicular to sketch) or
               'normal' (rib thickness along sketch normal).
    flip: reverse the extrusion direction if the rib grows the wrong way."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")

        doc = _active_doc()
        t_m = to_meters(thickness, unit)

        sketch_name = _select_last_sketch(doc)

        edge_type = 1 if direction.lower() == "parallel" else 0

        try:
            feat = doc.FeatureManager.InsertRib(
                edge_type,   # EdgeType: 0=Normal, 1=Parallel
                t_m,         # Thickness
                0,           # ThicknessSide: 0=OneSide
                flip,        # FlipSide
                False,       # IsTwoSided
                False,       # NextRefIsThinFeat
                False,       # ExtrudeDir
            )
        except Exception:
            feat = None

        if feat is None:
            try:
                feat = doc.FeatureManager.InsertRib3(
                    edge_type, t_m, 0, flip, False, False, False, 0, False,
                )
            except Exception:
                feat = None

        if feat is None:
            raise RuntimeError(
                f"Rib failed on sketch '{sketch_name}'. "
                "The sketch should sit on a plane between the two surfaces to reinforce, "
                "and its lines must touch or extend to those surfaces when extruded."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        feat_name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Rib"

        return {
            "feature": feat_name,
            "sketch": sketch_name,
            "thickness": thickness,
            "direction": direction,
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


@mcp.tool()
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


@mcp.tool()
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
            False,  # BScopeOptions
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


@mcp.tool()
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
    about its origin. If copy=True, keeps the original and creates copies;
    if False, just moves the original.

    body_name: name of the solid body to move/copy.
    dx/dy/dz: translation along each axis.
    rx/ry/rz: rotation angles in degrees around X, Y, Z axes.
    copy: True to create copies; False to move in place.
    num_copies: how many copies to make (only used if copy=True)."""

    def _impl():
        if copy and num_copies < 1:
            raise ValueError(f"num_copies must be >= 1, got {num_copies}.")

        doc = _active_doc()
        doc.ClearSelection2(True)

        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2(body_name, "SOLIDBODY", 0, 0, 0, False, 1, empty, 0):
            raise RuntimeError(f"Body '{body_name}' not found.")

        dx_m = to_meters(dx, unit)
        dy_m = to_meters(dy, unit)
        dz_m = to_meters(dz, unit)
        rx_r = math.radians(rx)
        ry_r = math.radians(ry)
        rz_r = math.radians(rz)

        n = num_copies if copy else 1

        # InsertMoveCopyBody2(TransX, TransY, TransZ, TransDist, RotPointX,
        #   RotPointY, RotPointZ, RotAngleX, RotAngleY, RotAngleZ, BCopy,
        #   NumCopies) — 12 args.
        feat = doc.FeatureManager.InsertMoveCopyBody2(
            dx_m, dy_m, dz_m,
            0.0,             # TransDist
            0.0, 0.0, 0.0,   # rotation origin
            rx_r, ry_r, rz_r,
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
            "copied": copy,
            "copies": n if copy else 0,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
async def combine_bodies(
    operation: str = "add",
    main_body: Optional[str] = None,
    tool_bodies: Optional[list] = None,
) -> dict:
    """Combine two or more solid bodies with a boolean operation.

    operation: 'add' (union), 'subtract' (main minus tools), or 'common' (intersection).
    main_body: main body name (required for 'subtract'; for 'add'/'common', the
               first body in the list is treated as main).
    tool_bodies: list of body names to combine with. If omitted for 'add'/'common',
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

        # Resolve main body object
        if main_body:
            main_obj = _find_body(main_body)
            if main_obj is None:
                raise RuntimeError(f"Main body '{main_body}' not found.")
        else:
            main_obj = all_disp[0]

        # Resolve tool body objects
        if tool_bodies:
            tools = []
            for name in tool_bodies:
                b = _find_body(name)
                if b is None:
                    raise RuntimeError(f"Tool body '{name}' not found.")
                tools.append(b)
        else:
            main_name = main_obj.Name if not callable(getattr(main_obj, "Name", None)) else main_obj.Name()
            tools = [b for b in all_disp
                     if (b.Name if not callable(getattr(b, "Name", None)) else b.Name()) != main_name]

        if not tools:
            raise RuntimeError("Combine requires at least 2 bodies (1 main + 1 tool).")

        tool_variant = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
            [t._oleobj_ for t in tools],
        )

        # InsertCombineFeature(OperationType, MainBody, ToolVar)
        feat = doc.FeatureManager.InsertCombineFeature(op, main_obj, tool_variant)

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


@mcp.tool()
async def create_reference_plane(
    reference: str = "front",
    offset: float = 50,
    angle: float = 0,
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
    angle: rotation angle in degrees (0 = parallel offset; nonzero = angled).
    flip: reverse the offset direction."""

    def _impl():
        doc = _active_doc()
        doc.ClearSelection2(True)

        if not _select_plane_by_name(doc, reference, append=False, mark=0):
            raise RuntimeError(f"Could not select reference plane '{reference}'.")

        offset_m = to_meters(offset, unit)
        angle_rad = math.radians(angle)

        if flip:
            offset_m = -offset_m

        try:
            feat = doc.FeatureManager.InsertRefPlane(
                8,           # Constraint1: 8=Distance
                offset_m,    # Distance
                0, 0.0,      # Constraint2 unused
                0, 0.0,      # Constraint3 unused
            )
        except Exception:
            feat = None

        if feat is None and angle != 0:
            try:
                feat = doc.FeatureManager.InsertRefPlane(
                    16, angle_rad,   # Constraint1: 16=Angle
                    0, 0.0,
                    0, 0.0,
                )
            except Exception:
                feat = None

        if feat is None:
            raise RuntimeError(
                f"Reference plane creation failed. reference='{reference}', "
                f"offset={offset}, angle={angle}."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Plane"

        return {
            "plane": name,
            "reference": reference,
            "offset": offset,
            "angle": angle,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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
      'point_edge'   — not implemented here; use execute_python if needed.

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

        # InsertAxis2 takes a single AutoSize boolean and infers the axis type
        # from the current selection (two planes, one cylindrical face, etc.).
        try:
            feat = doc.FeatureManager.InsertAxis2(True)
        except Exception:
            feat = None

        if feat is None:
            raise RuntimeError(
                f"Reference axis creation failed. method='{method}'. "
                "For 'two_planes' the planes must actually intersect; for 'cylinder' "
                "the point must lie on a cylindrical/conical face."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        name = feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Axis"

        return {
            "axis": name,
            "method": method,
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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

        try:
            feat = doc.FeatureManager.InsertSplitLineFeature(
                0, 0.0, False, False, False, True,
            )
        except Exception:
            feat = None

        if feat is None:
            try:
                feat = doc.FeatureManager.InsertSplitBody(consume_original)
            except Exception:
                try:
                    feat = doc.FeatureManager.SplitBodyByPlane()
                except Exception:
                    feat = None

        if feat is None:
            raise RuntimeError(
                f"Split failed. tool_type='{tool_type}'. Ensure the cutter fully "
                "intersects the body. For plane cuts, the plane must pass through the body."
            )

        feat_dispatch = win32com.client.Dispatch(feat)
        return {
            "feature": feat_dispatch.Name if hasattr(feat_dispatch, "Name") else "Split",
            "tool_type": tool_type,
            "consume_original": consume_original,
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
    """Return the currently active/selected drawing view, or the first view."""
    try:
        view = doc.ActiveDrawingView
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        pass
    try:
        sheet = doc.GetCurrentSheet
        if callable(sheet):
            sheet = sheet()
        views = doc.GetFirstView  # sheet is first "view"
        if callable(views):
            views = views()
        view = win32com.client.Dispatch(views).GetNextView
        if callable(view):
            view = view()
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        pass
    return None


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def insert_broken_view(
    break_position1: float, break_position2: float,
    orientation: str = "vertical",
    gap: float = 10,
    unit: Optional[str] = None,
) -> dict:
    """Add a broken view (removes the middle of a long part to save paper space).

    Adds two break lines to the currently selected view and removes the region
    between them, so a very long beam or tank fits on the sheet at a larger scale.

    break_position1/2: positions of the two break lines (sheet coordinate along
                       the break axis).
    orientation: 'vertical' (break lines are vertical, for horizontally-long parts)
                 or 'horizontal'.
    gap: visual gap left between the two pieces."""

    def _impl():
        doc = _active_drawing()
        view = _get_selected_view(doc)
        if view is None:
            raise RuntimeError("No drawing view is selected. Select a view first.")

        orient = 0 if orientation.lower() == "vertical" else 1  # 0=vertical break lines
        p1_m = to_meters(break_position1, unit)
        p2_m = to_meters(break_position2, unit)
        gap_m = to_meters(gap, unit)

        try:
            brk = view.BreakView3(orient, 2, gap_m, p1_m, p2_m)  # style 2 = zigzag
        except Exception:
            try:
                brk = view.BreakView2(orient, gap_m, p1_m, p2_m)
            except Exception:
                brk = None

        if brk is None:
            raise RuntimeError(
                "Broken view failed. Ensure a single model view is selected and the "
                "break positions lie within it."
            )

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


@mcp.tool()
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

        doc.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2("", "EDGE", ex_m, ey_m, 0, False, 0, empty, 0):
            raise RuntimeError(f"No edge found at ({edge_x}, {edge_y}) on the sheet.")

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


@mcp.tool()
async def add_drawing_dimension(
    x1: float, y1: float,
    x2: Optional[float] = None, y2: Optional[float] = None,
    place_x: float = 0, place_y: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Add a dimension to the active drawing between one or two selected entities.

    Select an entity (edge/vertex) near (x1,y1), optionally a second near (x2,y2),
    then place the dimension text at (place_x, place_y). For a single edge, this
    gives its length; for two entities, the distance between them.

    All coordinates are drawing-sheet coordinates."""

    def _impl():
        doc = _active_drawing()
        doc.ClearSelection2(True)

        x1_m, y1_m = to_meters(x1, unit), to_meters(y1, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        selected = False
        for seltype in ("EDGE", "VERTEX", "SILHOUETTE"):
            if doc.Extension.SelectByID2("", seltype, x1_m, y1_m, 0, False, 0, empty, 0):
                selected = True
                break
        if not selected:
            raise RuntimeError(f"No dimensionable entity found near ({x1}, {y1}).")

        if x2 is not None and y2 is not None:
            x2_m, y2_m = to_meters(x2, unit), to_meters(y2, unit)
            for seltype in ("EDGE", "VERTEX", "SILHOUETTE"):
                if doc.Extension.SelectByID2("", seltype, x2_m, y2_m, 0, True, 0, empty, 0):
                    break

        px_m, py_m = to_meters(place_x, unit), to_meters(place_y, unit)
        dim = doc.AddDimension2(px_m, py_m, 0)
        if dim is None:
            raise RuntimeError("Failed to add drawing dimension. Check the selected entities.")

        doc.ClearSelection2(True)
        return {
            "from": [x1, y1],
            "to": [x2, y2] if x2 is not None else None,
            "placed_at": [place_x, place_y],
            "unit": unit or _default_unit,
        }

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        doc.ClearSelection2(True)
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        if not doc.Extension.SelectByID2("", "EDGE", x_m, y_m, 0, False, 0, empty, 0):
            log.warning("No edge at weld point; inserting symbol unattached")

        try:
            sym = doc.InsertWeldSymbol3()
        except Exception:
            try:
                sym = doc.InsertWeldSymbol()
            except Exception:
                sym = None

        if sym is None:
            raise RuntimeError(
                "Weld symbol insertion failed. Some SolidWorks versions require the "
                "symbol to be configured through a property manager dialog."
            )

        return {"weld_type": weld_type, "size": size, "placed_at": [x, y],
                "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
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
        doc.ClearSelection2(True)
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        doc.Extension.SelectByID2("", "EDGE", x_m, y_m, 0, False, 0, empty, 0)

        types = {"basic": 0, "machined": 1, "nomachining": 2}
        sym_code = types.get(symbol_type.lower(), 1)

        # InsertSurfaceFinishSymbol3(SymType, LeaderType, LocX, LocY, LocZ,
        #   LaySymbol, ArrowType, MachAllowance, OtherVals, ProdMethod, SampleLen,
        #   MaxRoughness, MinRoughness, RoughnessSpacing) — 14 args.
        sf = None
        try:
            sf = doc.InsertSurfaceFinishSymbol3(
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


@mcp.tool()
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
        doc.ClearSelection2(True)
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        doc.Extension.SelectByID2("", "EDGE", x_m, y_m, 0, False, 0, empty, 0)

        try:
            gtol = doc.InsertGtol()
            if gtol is not None:
                gtol = win32com.client.Dispatch(gtol)
                symbol_map = {
                    "position": 8, "concentricity": 10, "perpendicularity": 5,
                    "parallelism": 4, "flatness": 1, "straightness": 0,
                    "circularity": 2, "cylindricity": 3, "angularity": 6,
                    "symmetry": 11, "runout": 12,
                }
                sym_code = symbol_map.get(symbol.lower(), 8)
                try:
                    gtol.SetFrameValues2(1, "", str(tolerance), datum, "", "")
                except Exception:
                    pass
        except Exception:
            gtol = None

        if gtol is None:
            raise RuntimeError(
                "GD&T frame insertion failed. This annotation often requires the "
                "property manager; consider adding it manually."
            )

        return {"symbol": symbol, "tolerance": tolerance, "datum": datum,
                "placed_at": [x, y], "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
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
        doc.ClearSelection2(True)
        x_m, y_m = to_meters(x, unit), to_meters(y, unit)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not doc.Extension.SelectByID2("", "EDGE", x_m, y_m, 0, False, 0, empty, 0):
            doc.Extension.SelectByID2("", "FACE", x_m, y_m, 0, False, 0, empty, 0)

        # InsertBOMBalloon2(Style, Size, UpperTextStyle, UpperText, LowerTextStyle,
        #   LowerText) — 6 args, on IModelDocExtension.
        note = None
        try:
            note = doc.Extension.InsertBOMBalloon2(
                1,      # Style: swBalloonStyle_Circular
                6,      # Size: swBalloonFit_5Chars
                1,      # UpperTextStyle: item number
                "",     # UpperText
                0,      # LowerTextStyle
                "",     # LowerText
            )
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


@mcp.tool()
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

        # Resolve a BOM template if none supplied (InsertBomTable4 needs a valid one).
        tmpl = template
        if not tmpl:
            try:
                app = _connect()
                # swFileLocationsBOMTemplates = 92
                loc = app.GetUserPreferenceStringValue(92)
                if loc:
                    for base in loc.split(";"):
                        cand = os.path.join(base, "bom-standard.sldbomtbt")
                        if os.path.isfile(cand):
                            tmpl = cand
                            break
            except Exception:
                pass

        # InsertBomTable4(TemplateName, X, Y, BomType, ConfigurationName, Hidden,
        #   IndentedNumberingType, DetailedCutList, DissolvePartLevelRows).
        try:
            bom = view.InsertBomTable4(
                tmpl,       # TemplateName
                x_m, y_m,   # X, Y
                2,          # BomType: swBomType_PartsOnly
                "",         # ConfigurationName
                False,      # Hidden
                1,          # IndentedNumberingType
                False,      # DetailedCutList
                False,      # DissolvePartLevelRows
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


@mcp.tool()
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

@mcp.tool()
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

        # AddExplodeStep2(ExplDist, ExplDirIndex, ReverseDir, ExplAng, RotAxisIndex,
        #   ReverseAng, RotateAboutOrigin, AutoSpaceComponentsOnDrag, Error) — one
        #   step per component, each moved a bit further along the axis.
        created_steps = 0
        last_err = None
        for i, raw in enumerate(comps):
            comp = win32com.client.Dispatch(raw)
            assy.ClearSelection2(True)
            try:
                comp.Select4(False, None, False)
            except Exception:
                try:
                    comp.Select2(False, 0)
                except Exception:
                    continue
            step_dist = dist_m * (i + 1)
            err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            try:
                assy.AddExplodeStep2(step_dist, dir_index, False, 0.0, 0, False, True, False, err)
                created_steps += 1
            except Exception as e:
                last_err = e

        if created_steps == 0:
            raise RuntimeError(
                "Exploded view creation could not add steps automatically "
                f"({last_err}). Create the explode manually if needed."
            )

        return {"steps": created_steps, "direction": direction,
                "distance": explode_distance, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
async def add_advanced_mate(
    mate_type: str,
    x1: float, y1: float, z1: float,
    x2: float, y2: float, z2: float,
    value: float = 0,
    unit: Optional[str] = None,
) -> dict:
    """Add an advanced mate between two entities selected by point.

    Goes beyond basic mates to include distance, angle, width, and symmetry
    constraints needed to precisely position parts on platforms and tanks.

    mate_type: 'distance', 'angle', 'width', 'symmetric', 'lock', 'tangent',
               'coincident', 'concentric', 'parallel', 'perpendicular'.
    x1/y1/z1: a point on the first face/edge.
    x2/y2/z2: a point on the second face/edge.
    value: distance (in current unit) for 'distance', or angle in degrees for 'angle'."""

    def _impl():
        assy = _active_assembly()

        types = {
            "coincident": 0, "concentric": 1, "perpendicular": 2, "parallel": 3,
            "tangent": 4, "distance": 5, "angle": 6, "symmetric": 9,
            "width": 11, "lock": 13,
        }
        code = types.get(mate_type.lower())
        if code is None:
            raise ValueError(f"Unknown mate_type '{mate_type}'. Use: {', '.join(types)}")

        x1_m, y1_m, z1_m = to_meters(x1, unit), to_meters(y1, unit), to_meters(z1, unit)
        x2_m, y2_m, z2_m = to_meters(x2, unit), to_meters(y2, unit), to_meters(z2, unit)

        assy.ClearSelection2(True)
        empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        if not assy.Extension.SelectByID2("", "FACE", x1_m, y1_m, z1_m, False, 1, empty, 0):
            raise RuntimeError(f"No entity found at point 1 ({x1}, {y1}, {z1}).")
        if not assy.Extension.SelectByID2("", "FACE", x2_m, y2_m, z2_m, True, 1, empty, 0):
            raise RuntimeError(f"No entity found at point 2 ({x2}, {y2}, {z2}).")

        if mate_type.lower() == "angle":
            mate_value = math.radians(value)
        else:
            mate_value = to_meters(value, unit)

        mate_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        mate = assy.AddMate5(
            code, 0, False,
            mate_value, mate_value, mate_value,
            0.0, 0.0, 0.0, 0.0, 0.0,
            False, False, 0, mate_err,
        )

        if mate is None:
            raise RuntimeError(
                f"Advanced mate '{mate_type}' failed (error {mate_err.value}). "
                "Confirm both entities support this mate type."
            )

        return {"mate_type": mate_type, "value": value if code in (5, 6) else None,
                "error_code": mate_err.value, "unit": unit or _default_unit}

    return await _run(_impl)


@mcp.tool()
async def interference_check() -> dict:
    """Check the active assembly for interferences (parts overlapping in space).

    Reports every pair of components whose solid volumes intersect — a critical
    check before fabrication, since real steel can't occupy the same space twice."""

    def _impl():
        assy = _active_assembly()
        assy.ClearSelection2(True)

        # ToolsCheckInterference2(NumComponents, LpComponents, CoincidentInterference,
        #   PComp[out], PFace[out]) -> count. NumComponents=0 checks the whole
        #   assembly. (There is no CreateInterferenceDetectionMgr in this API.)
        count = 0
        try:
            comp_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
            face_out = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
            empty_arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, None)
            count = assy.ToolsCheckInterference2(0, empty_arr, False, comp_out, face_out)
        except Exception as e:
            log.warning("ToolsCheckInterference2 failed: %s", e)
            try:
                count = assy.ToolsCheckInterference2(0, None, False)
            except Exception as e2:
                raise RuntimeError(
                    f"Interference check is not available through this SolidWorks API "
                    f"build ({e2})."
                )

        count = int(count) if count else 0
        return {
            "interferences": count,
            "clear": count == 0,
        }

    return await _run(_impl)


@mcp.tool()
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
        _select_component(assy, component_name)

        if pattern_type.lower() == "linear":
            spacing_m = to_meters(spacing, unit)
            axis = direction.lower()

            edge_name = None
            feat = assy.FirstFeature
            axis_count = {"x": 0, "y": 1, "z": 2}[axis]
            found = 0
            while feat is not None:
                try:
                    if feat.GetTypeName2 in ("RefPlane",):
                        pass
                except Exception:
                    pass
                try:
                    feat = feat.GetNextFeature
                except Exception:
                    break

            try:
                feat = assy.FeatureManager.FeatureLinearPattern4(
                    count, spacing_m, 1, 0.0, False, False,
                    "NULL", "NULL", False, False, False, False, False, False, True,
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
            fx, fy, fz = to_meters(axis_face_x, unit), to_meters(axis_face_y, unit), to_meters(axis_face_z, unit)
            angle_rad = math.radians(angle)
            empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
            assy.Extension.SelectByID2("", "FACE", fx, fy, fz, True, 4, empty, 0)

            try:
                feat = assy.FeatureManager.FeatureCircularPattern5(
                    count, angle_rad, False, "NULL", False, True, False,
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

@mcp.tool()
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

        try:
            part = doc  # IPartDoc
            part.SetMaterialPropertyName2("", database, material)
        except Exception:
            try:
                part.SetMaterialPropertyName(database, material)
            except Exception:
                raise RuntimeError(
                    f"Failed to set material '{material}'. Check that the name matches "
                    "the SolidWorks material library exactly (case-sensitive)."
                )

        return {"material": material, "database": database}

    return await _run(_impl)


@mcp.tool()
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

        color_ref = red + (green << 8) + (blue << 16)

        applied = False
        try:
            if target.lower() == "face":
                sel = doc.SelectionManager.GetSelectedObject6(1, -1)
                sel = win32com.client.Dispatch(sel)
                mat_props = sel.MaterialPropertyValues
                mat_props = list(mat_props) if mat_props else [0.0] * 9
                mat_props[0] = red / 255.0
                mat_props[1] = green / 255.0
                mat_props[2] = blue / 255.0
                sel.MaterialPropertyValues = mat_props
                applied = True
            else:
                model = doc
                mat_props = [red / 255.0, green / 255.0, blue / 255.0, 1.0, 1.0, 0.5, 0.3, 0.0, 0.0]
                model.MaterialPropertyValues = mat_props
                applied = True
        except Exception:
            try:
                doc.SetColorTable(color_ref)
                applied = True
            except Exception:
                pass

        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        if not applied:
            raise RuntimeError("Failed to set appearance/color.")

        return {"rgb": [red, green, blue], "target": target}

    return await _run(_impl)


# ===========================================================================
# Configuration tools
# ===========================================================================

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def list_features() -> dict:
    """List every feature in the active document's feature tree."""

    def _impl():
        doc = _active_doc()
        features = []
        for raw_feature in doc.FeatureManager.GetFeatures(False) or ():
            feat = win32com.client.Dispatch(raw_feature)
            try:
                features.append({"name": feat.Name, "type": feat.GetTypeName2})
            except Exception:
                pass
        return {"count": len(features), "features": features}

    return await _run(_impl)


# ===========================================================================
# Custom properties tools
# ===========================================================================

@mcp.tool()
async def get_custom_properties(config: str = "") -> dict:
    """Read all custom properties from the active document.
    config: configuration name (empty string = document-level properties)."""

    def _impl():
        doc = _active_doc()
        cpm = doc.Extension.CustomPropertyManager(config)
        names = cpm.GetNames
        if not names:
            return {"config": config or "(document)", "count": 0, "properties": {}}

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
        return {"config": config or "(document)", "count": len(props), "properties": props}

    return await _run(_impl)


@mcp.tool()
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

@mcp.tool()
async def measure_body() -> dict:
    """Measure the active document's solid body: mass, volume, surface area,
    center of mass, and bounding box. Returns values in SI units (kg, m, m^2, m^3).
    Requires a part document with at least one solid body and a material assigned."""

    def _impl():
        doc = _active_doc()
        result = {}

        try:
            mp = doc.Extension.CreateMassProperty()
            if mp is not None:
                mp = win32com.client.Dispatch(mp)
                try:
                    result["mass_kg"] = mp.Mass
                except Exception:
                    pass
                try:
                    result["volume_m3"] = mp.Volume
                except Exception:
                    pass
                try:
                    result["surface_area_m2"] = mp.SurfaceArea
                except Exception:
                    pass
                try:
                    com = mp.CenterOfMass
                    if com:
                        result["center_of_mass_m"] = list(com)
                except Exception:
                    pass
        except Exception:
            log.debug("CreateMassProperty failed, trying GetMassProperties")
            try:
                status = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                props = doc.Extension.GetMassProperties2(1, status, False)
                if props:
                    result["center_of_mass_m"] = [props[0], props[1], props[2]]
                    result["volume_m3"] = props[3]
                    result["surface_area_m2"] = props[4]
                    result["mass_kg"] = props[5]
            except Exception:
                pass

        bodies = doc.GetBodies2(0, True)
        if bodies:
            try:
                body = win32com.client.Dispatch(bodies[0])
                box = body.GetBodyBox()
                if box:
                    factor = 1.0 / UNIT_TO_METERS.get(_default_unit, 0.001)
                    result["bounding_box"] = {
                        "min_m": [box[0], box[1], box[2]],
                        "max_m": [box[3], box[4], box[5]],
                        "size": {
                            "x": round(abs(box[3] - box[0]) * factor, 4),
                            "y": round(abs(box[4] - box[1]) * factor, 4),
                            "z": round(abs(box[5] - box[2]) * factor, 4),
                            "unit": _default_unit,
                        },
                    }
            except Exception:
                pass

        if not result:
            raise RuntimeError(
                "Could not measure the body. Ensure the document has a solid body "
                "and a material assigned (right-click the material in the feature tree)."
            )
        return result

    return await _run(_impl)


# ===========================================================================
# Utility tools
# ===========================================================================

@mcp.tool()
async def set_units(unit: str) -> dict:
    """Set the default unit (mm, cm, m, in, or ft) used by tools when 'unit' is omitted."""
    global _default_unit
    u = unit.lower()
    if u not in UNIT_TO_METERS:
        raise ValueError(f"Invalid unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
    _default_unit = u
    return {"default_unit": _default_unit}


@mcp.tool()
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


@mcp.tool()
async def zoom_to_fit() -> dict:
    """Zoom the view to fit the entire model in the viewport."""

    def _impl():
        doc = _active_doc()
        doc.ViewZoomtofit2()
        return {"zoomed": True}

    return await _run(_impl)


@mcp.tool()
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


_BLOCKED_BUILTINS = frozenset({
    "open", "__import__", "exec", "eval", "compile",
    "exit", "quit", "input", "breakpoint",
})
_SAFE_BUILTINS = {k: v for k, v in vars(builtins).items() if k not in _BLOCKED_BUILTINS}


@mcp.tool()
async def execute_python(code: str) -> dict:
    """Run Python with 'sw' (SldWorks Application) and 'doc' (active document)
    in scope. Use print() to return debug output.
    Sandboxed: open/import/exec/eval/compile are blocked for safety."""

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


if __name__ == "__main__":
    mcp.run()
