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
import base64
import asyncio
import shutil
import zipfile
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
    """Open a document through OpenDoc6 and return ``(document, errors, warnings)``."""
    result = app.OpenDoc6(filepath, doc_type, options, "", 0, 0)
    doc, outputs = _split_com_result(result)
    errors = int(outputs[0]) if len(outputs) > 0 else 0
    warnings = int(outputs[1]) if len(outputs) > 1 else 0
    return doc, errors, warnings


def _activate_doc3(app, title: str, make_visible: bool = True):
    """Activate a document while tolerating typed and dynamic COM dispatch."""
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
    app = _connect()
    ext = os.path.splitext(filepath)[1].lower()
    doc_type = {".sldprt": 1, ".sldasm": 2}.get(ext, 1)
    loaded_doc, errors, _warnings = _open_doc6(app, filepath, doc_type, 1)
    if loaded_doc is None:
        raise RuntimeError(f"Failed to load component '{filepath}' (error code {errors}).")

    active_assy, reactivate_errors = _activate_doc3(app, assy_title, False)
    if active_assy is None:
        raise RuntimeError(f"Failed to re-activate assembly '{assy_title}' (error code {reactivate_errors}).")
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
        version = app.RevisionNumber
        if callable(version):
            version = version()
        return {"connected": True, "version": str(version), "launched": _last_launch_happened}

    return await _run(_impl)


@mcp.tool()
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
            saved, errs, _wrns = _save_doc3(doc)
            if not saved or errs != 0:
                raise RuntimeError(f"Failed to save '{title}' before closing (error code {errs}).")
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
            saved, errors, _warnings = _save_doc3(doc)
            if not saved or errors != 0:
                raise RuntimeError(f"Save failed (error code {errors}).")
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
async def get_component_transform(name: str, unit: Optional[str] = None) -> dict:
    """Read an assembly component's exact position and orientation.

    Returns translation in ``unit`` (mm by default), the 3x3 rotation matrix,
    and whether the component is fixed. Use this before changing an assembly
    pose so a placement can be checked or restored deterministically.
    """

    def _impl():
        assy = _active_assembly()
        component = _find_component(assy, name)
        if component is None:
            raise RuntimeError(f"Component '{name}' not found in the assembly.")
        transform = component.Transform2
        data = transform.ArrayData
        if callable(data):
            data = data()
        values = list(data or ())
        if len(values) < 12:
            raise RuntimeError(f"Component '{name}' returned an invalid transform.")
        active_unit = (unit or _default_unit).lower()
        if active_unit not in UNIT_TO_METERS:
            raise ValueError(f"Unknown unit '{unit}'. Use one of: {', '.join(UNIT_TO_METERS)}")
        factor = 1.0 / UNIT_TO_METERS[active_unit]
        return {
            "name": name,
            "translation": {
                "x": values[9] * factor,
                "y": values[10] * factor,
                "z": values[11] * factor,
                "unit": active_unit,
            },
            "rotation_matrix": [values[0:3], values[3:6], values[6:9]],
            "fixed": bool(component.IsFixed),
        }

    return await _run(_impl)


@mcp.tool()
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
) -> dict:
    """Set a component's absolute assembly position and Euler rotation.

    Translations use ``unit`` (mm by default); rotations are degrees. The
    rotation is composed X then Y then Z, and is applied as an absolute pose,
    not an incremental drag. Fixed components are floated automatically before
    the transform is applied. Set ``fix_after`` to lock the verified pose.
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

        return {
            "name": name,
            "translation": {"x": x, "y": y, "z": z, "unit": unit or _default_unit},
            "rotation_degrees": {"x": rotation_x, "y": rotation_y, "z": rotation_z},
            "fixed": bool(fix_after),
            "previously_fixed": was_fixed,
        }

    return await _run(_impl)


@mcp.tool()
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
        target_dir = os.path.abspath(output_dir)
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
        spline = doc.SketchManager.CreateSpline3(point_data, None, None, natural_ends, status)
        if spline is None:
            raise RuntimeError("SolidWorks could not create the spline in the active sketch.")
        return {
            "point_count": len(points),
            "natural_ends": natural_ends,
            "unit": unit or _default_unit,
        }

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
        _ = doc.EditRebuild3
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
        equal_spacing = abs(angle - 360) < 0.01

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
    thread_standard: 'ISO' or 'ANSI' metric. Hole Wizard uses metric M-size
    families; tapped holes use the matching ISO coarse pitch automatically."""

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
    profile: 'triangular' (straight hypotenuse) or 'flat' (polygonal infill)."""

    def _impl():
        if thickness <= 0:
            raise ValueError(f"Thickness must be positive, got {thickness}.")
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
        d1 = to_meters(50, unit)  # default gusset leg 1
        d2 = to_meters(50, unit)  # default gusset leg 2
        # A polygonal gusset needs a third distance plus either an angle or a
        # fourth distance. Use the documented d3 + 45 degree form; setting d3
        # to zero produces no profile in SolidWorks.
        d3 = to_meters(25, unit) if is_polygon else 0.0
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
            math.radians(45),  # ProfileAngle
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

        fx, fy, fz = to_meters(face_x, unit), to_meters(face_y, unit), to_meters(face_z, unit)
        requested_point = (fx, fy, fz)

        # A hollow member's end face shares its edges with the side faces.
        # SelectByID2 at a coordinate can therefore select a side face even
        # when the coordinate is on the end. Locate the closest planar end
        # face and ray-select it from outside the member instead.
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
                end_face_candidates.append((distance_sq, axis, mins[axis]))

        if not end_face_candidates:
            raise RuntimeError(
                f"No planar structural-member end face was found near ({face_x}, {face_y}, {face_z})."
            )

        _, axis, face_coordinate = min(end_face_candidates, key=lambda item: item[0])
        ray_radius = 1e-4
        selected_end_face = False
        for outward_sign in (1.0, -1.0):
            ray_start = list(requested_point)
            ray_start[axis] = face_coordinate + outward_sign * 0.01
            ray_vector = [0.0, 0.0, 0.0]
            ray_vector[axis] = -outward_sign
            doc.ClearSelection2(True)
            if not doc.Extension.SelectByRay(
                *ray_start,
                *ray_vector,
                ray_radius,
                2,      # swSelFACES
                False,
                0,
                0,
            ):
                continue
            selected_face = doc.SelectionManager.GetSelectedObject6(1, 0)
            if selected_face is None:
                continue
            selected_box = tuple(win32com.client.Dispatch(selected_face).GetBox)
            selected_spans = [
                selected_box[index + 3] - selected_box[index] for index in range(3)
            ]
            if (
                selected_spans[axis] <= 1e-7
                and abs(selected_box[axis] - face_coordinate) <= 1e-6
            ):
                selected_end_face = True
                break

        if not selected_end_face:
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
                # These zero-argument ModelDoc2 commands are exposed as
                # already-invoked Boolean properties by late-bound pywin32.
                # Adding parentheses tries to call that Boolean and raises
                # TypeError after SolidWorks has already changed the state.
                changed = doc.EditUnsuppress2
                if not changed:
                    raise RuntimeError("SolidWorks could not unsuppress the flat pattern.")
                doc.ClearSelection2(True)
                return {"state": "flattened", "action": "unsuppressed flat-pattern"}
            else:
                flat_pattern.Select2(False, 0)
                changed = doc.EditSuppress2
                if not changed:
                    raise RuntimeError("SolidWorks could not suppress the flat pattern.")
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


@mcp.tool()
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
    """Return the currently active/selected drawing view, or the first view."""
    def _noarg_member(obj, name):
        value = getattr(obj, name)
        if callable(value):
            try:
                return value()
            except Exception:
                # Dynamic COM dispatch can expose an already-evaluated object
                # as callable; in that case invoking it raises "member not
                # found", while the original value is the desired object.
                return value
        return value

    try:
        view = _noarg_member(doc, "ActiveDrawingView")
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        pass
    try:
        views = _noarg_member(doc, "GetFirstView")  # sheet is first "view"
        view = _noarg_member(win32com.client.Dispatch(views), "GetNextView")
        if view is not None:
            return win32com.client.Dispatch(view)
    except Exception:
        pass
    return None


_DRAWING_EDGE_IID = "{83A33D42-27C5-11CE-BFD4-00400513BB57}"


def _select_drawing_edge_near(doc, x_m: float, y_m: float, tolerance_m: float = 0.002):
    """Select the visible drawing edge closest to a sheet-space point.

    ``SelectByID2`` does not reliably hit projected model edges in drawings.
    This uses the documented IView entity enumeration and selection APIs while
    preserving the public MCP contract of accepting sheet coordinates.
    """
    source_view = _get_selected_view(doc)
    if source_view is None:
        raise RuntimeError("No model view is available to select a drawing edge.")

    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    try:
        entities = source_view.GetVisibleEntities2(empty, 1) or ()  # swViewEntityType_Edge
        xform = tuple(source_view.GetViewXform)
        if len(xform) != 13:
            raise RuntimeError("SolidWorks returned an invalid drawing-view transform.")
    except Exception as exc:
        raise RuntimeError("Could not enumerate visible edges in the selected drawing view.") from exc

    def project(point):
        x, y, z = point
        # IView.GetViewXform: rotation [0:9], translation [9:12], and scale
        # [12]. SolidWorks stores the rotation matrix by columns.
        scale = xform[12]
        return (
            (xform[0] * x + xform[3] * y + xform[6] * z) * scale + xform[9],
            (xform[1] * x + xform[4] * y + xform[7] * z) * scale + xform[10],
        )

    def distance_to_segment_sq(start, end):
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return (x_m - sx) ** 2 + (y_m - sy) ** 2
        fraction = max(0.0, min(1.0, ((x_m - sx) * dx + (y_m - sy) * dy) / length_sq))
        cx, cy = sx + fraction * dx, sy + fraction * dy
        return (x_m - cx) ** 2 + (y_m - cy) ** 2

    closest_edge = None
    closest_distance_sq = float("inf")
    for raw_entity in entities:
        try:
            edge = win32com.client.Dispatch(raw_entity, "IEdge", _DRAWING_EDGE_IID)
            params = tuple(edge.GetCurveParams())
            if len(params) < 6:
                continue
            distance_sq = distance_to_segment_sq(project(params[:3]), project(params[3:6]))
            if distance_sq < closest_distance_sq:
                closest_edge = raw_entity
                closest_distance_sq = distance_sq
        except Exception:
            continue

    if closest_edge is None or closest_distance_sq > tolerance_m ** 2:
        raise RuntimeError(
            f"No edge found near ({x_m:.6f}, {y_m:.6f}) m on the sheet."
        )

    doc.ClearSelection2(True)
    if not source_view.SelectEntity(closest_edge, False):
        raise RuntimeError("SolidWorks could not select the requested reference edge.")
    return source_view


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

        source_view = _select_drawing_edge_near(doc, x_m, y_m)
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


@mcp.tool()
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


@mcp.tool()
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

        if mate is None:
            raise RuntimeError(
                f"Advanced mate '{mate_type}' failed (error {mate_err.value}). "
                "Confirm both entities support this mate type."
            )

        return {"mate_type": mate_key, "value": value if code in (5, 6) else None,
                "gear_ratio": [gear_ratio_numerator, gear_ratio_denominator] if mate_key == "gear" else None,
                "reverse": bool(reverse) if mate_key == "gear" else None,
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
            count = int(manager.GetInterferenceCount())
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

        # The first three values are normalized RGB.  The remaining values
        # describe ambient, diffuse, specular, shininess, transparency, and
        # emission respectively (IModelDocExtension/IFace2 contract).
        material_values = [
            red / 255.0,
            green / 255.0,
            blue / 255.0,
            0.2,
            0.8,
            0.5,
            0.3,
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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
    """Create a complete automotive piston-and-connecting-rod assembly.

    This is the moving assembly represented in engine cutaway drawings: the
    piston with three ring grooves and a hollow skirt, a transverse wrist pin,
    an I-style connecting rod, a plain big-end bearing, and a separate rod cap.
    Each component is saved as an editable SolidWorks part, then inserted into
    a final ``.SLDASM`` assembly at the correct shared pin/crank centerlines.

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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def add_equation(equation: str, solve: bool = True) -> dict:
    """Add an equation or global variable, e.g. ``\"Length\" = 25mm``."""

    def _impl():
        manager = _equation_manager(_active_doc())
        index = int(manager.Add2(-1, equation, bool(solve)))
        if index < 0:
            raise RuntimeError(f"SolidWorks rejected equation: {equation}")
        if not solve:
            _evaluate_equations(manager)
        return {"index": index, "equation": manager.Equation(index), "solved": solve}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        abs_path = os.path.abspath(filepath)
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


@mcp.tool()
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
        destination_path = os.path.abspath(destination)
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
        os.makedirs(staging_dir, exist_ok=True)
        copied = []
        for original in dependency_paths:
            filename = os.path.basename(original)
            target_name = f"{prefix}{os.path.splitext(filename)[0]}{suffix}{os.path.splitext(filename)[1]}"
            relative_target = target_name if flatten_to_single_folder else os.path.join("models", target_name)
            target = os.path.join(staging_dir, relative_target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(original, target)
            copied.append(relative_target)
        if is_zip:
            with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative_target in copied:
                    archive.write(os.path.join(staging_dir, relative_target), relative_target)
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not copied or (is_zip and not os.path.isfile(destination_path)):
            raise RuntimeError("The managed packaging fallback did not create output files.")
        return {
            "destination": destination_path,
            "format": "zip" if is_zip else "folder",
            "source_document_count": len(dependency_paths),
            "copied_files": copied,
            "backend": "managed_dependency_fallback",
            "native_pack_and_go_error": native_error,
        }

    return await _run(_impl)


def _configuration_by_name(doc, name: str):
    """Return a named configuration, or the active configuration for an empty name."""
    if not name.strip():
        return doc.ConfigurationManager.ActiveConfiguration
    configuration = doc.GetConfigurationByName(name)
    if configuration is None:
        raise ValueError(f"Configuration '{name}' does not exist.")
    return configuration


@mcp.tool()
async def list_display_states(configuration: str = "") -> dict:
    """List display states for a configuration (or the active configuration)."""

    def _impl():
        doc = _active_doc()
        cfg = _configuration_by_name(doc, configuration)
        states = getattr(cfg, "GetDisplayStates", ())
        states = states() if callable(states) else states
        return {"configuration": str(cfg.Name), "count": len(states or ()), "display_states": list(states or ())}

    return await _run(_impl)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
