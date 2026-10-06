"""
Fake COM layer: lets the test suite collect and run on any OS.

``server.py`` needs ``win32com.client`` and ``pythoncom`` to *import* --
``pythoncom.CoInitialize()`` runs unconditionally at module load time to set
up the single-threaded COM executor (server.py, "Single-threaded COM
executor" section). pywin32 only installs on Windows, so without this, the
whole suite could only ever run on a Windows box -- which is also the one
place a packaging mistake is most expensive to discover (only after a user
hits it).

None of the tests in this suite call into a tool's actual implementation
(the part that talks to a running SolidWorks): they introspect signatures,
docstrings and source via ``inspect`` (see tests/test_contract.py and
tests/test_bugfixes.py). So the fake modules below only need to exist and be
importable, not behave like real COM -- a bare ``MagicMock`` tree answers any
attribute access and is enough to satisfy every test that exists today. If a
future test ever awaits a tool's real implementation, it needs a live
SolidWorks and belongs in tests/run_*_live_test.py instead, not here.

On an actual Windows machine with pywin32 installed, this file changes
nothing: the ``try`` below succeeds and the real modules are used untouched.
"""
import os
import sys
from unittest.mock import MagicMock

# execute_python is registered only when this is set (see server.py's
# _execute_python_enabled) -- unset, it simply isn't in the tool catalog at
# all, which is the whole point of R3.24 (don't even advertise the riskiest
# tool unless an install opted in). The test suite exercises the maximal
# surface -- tests/tool_names.json's 150-tool snapshot and
# test_run_macro_and_execute_python_are_the_only_open_world_tools both
# assume it exists -- so set this before `server` is ever imported, exactly
# as "a trusted local debugging session" (the tool's own docstring) would.
# setdefault: a real Windows CI run or a developer who explicitly set this
# to "0" to test the disabled path is left alone.
os.environ.setdefault("SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON", "1")

try:
    import pythoncom  # noqa: F401
    import win32com.client  # noqa: F401
except ImportError:
    _fake_pythoncom = MagicMock(name="pythoncom")
    # Referenced as bit-flags inside VARIANT() calls deep in server.py, e.g.
    # ``pythoncom.VT_BYREF | pythoncom.VT_I4``. MagicMock doesn't implement
    # ``__or__`` by default, so these need to be real ints for any code path
    # that happens to combine them to not raise -- even though no test here
    # exercises that path today, a fake constant that behaves like the real
    # one costs nothing and avoids a confusing failure for whoever adds one.
    for _name, _value in (
        ("VT_BYREF", 0x4000), ("VT_I4", 3), ("VT_R4", 4), ("VT_R8", 5),
        ("VT_BSTR", 8), ("VT_DISPATCH", 9), ("VT_BOOL", 11),
        ("VT_VARIANT", 12), ("VT_UNKNOWN", 13), ("VT_ARRAY", 0x2000),
    ):
        setattr(_fake_pythoncom, _name, _value)
    _fake_pythoncom.CoInitialize = lambda: None
    _fake_pythoncom.CoUninitialize = lambda: None

    _fake_win32com_client = MagicMock(name="win32com.client")
    _fake_win32com = MagicMock(name="win32com")
    _fake_win32com.client = _fake_win32com_client

    # _connect_running_instance() tries GetActiveObject/GetObject and treats
    # any exception as "nothing is running" -- which is exactly what real
    # pywin32 raises on a machine with no SolidWorks COM server registered
    # (a bare CI runner, same as this one). Left as a plain MagicMock, these
    # calls would instead "succeed" with a fake Application/ActiveDoc that
    # answers every attribute access, so server code that expects a REAL
    # document (flatten_sheet_metal's state-polling loop, for one) runs
    # straight into application logic that was never meant to execute
    # without one -- and can spin forever comparing real values against
    # mock objects that never equal them. Failing here, the same way real
    # pywin32 does without SolidWorks installed, is what lets
    # tests/test_bugfixes.py's "no SolidWorks: any exception is fine, this
    # isn't what the test is about" branches actually trigger instead of
    # hanging the single COM worker thread (and, with it, every later test
    # that queues behind it).
    _no_solidworks = OSError("GetActiveObject: no SolidWorks COM server is registered")
    _fake_win32com_client.GetActiveObject.side_effect = _no_solidworks
    _fake_win32com_client.GetObject.side_effect = _no_solidworks

    sys.modules.setdefault("pythoncom", _fake_pythoncom)
    sys.modules.setdefault("win32com", _fake_win32com)
    sys.modules.setdefault("win32com.client", _fake_win32com_client)
