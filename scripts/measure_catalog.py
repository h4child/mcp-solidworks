"""Measure how much context the MCP tool catalog costs a client.

Every tool definition (name, description, input schema, annotations) is sent
to the model on every conversation, before the user types anything. At v5.21.5
this catalog was ~200k characters (~58k tokens). This script prints the total
and the heaviest tools, and with ``--max-tokens`` exits non-zero above a
budget, so CI can stop the catalog from silently growing back.

The token figure is an estimate (characters / 3.5 for this mixed
English/JSON text); use it to compare versions, not as an exact bill.

Runs on any OS: when pywin32 is not importable it installs the same fake COM
modules the test suite uses (tests/conftest.py). Importing the server never
needs a running SolidWorks.

Usage:
    python scripts/measure_catalog.py
    python scripts/measure_catalog.py --max-tokens 60000 --top 15
"""

import argparse
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARS_PER_TOKEN = 3.5


def _import_server():
    sys.path.insert(0, ROOT)
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError:
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import conftest  # noqa: F401  -- installs fake pythoncom/win32com
    import server
    return server


def measure() -> dict:
    server = _import_server()
    tools = asyncio.run(server.mcp.list_tools())
    entries = [tool.model_dump(exclude_none=True) for tool in tools]
    sizes = sorted(
        ((len(json.dumps(entry, ensure_ascii=False)), entry["name"]) for entry in entries),
        reverse=True,
    )
    total_chars = len(json.dumps(entries, ensure_ascii=False))
    return {
        "tools": len(entries),
        "chars": total_chars,
        "tokens_estimate": int(total_chars / CHARS_PER_TOKEN),
        "heaviest": [{"tool": name, "chars": chars} for chars, name in sizes],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="fail (exit 1) when the estimate exceeds this budget")
    parser.add_argument("--top", type=int, default=10, help="how many heavy tools to list")
    args = parser.parse_args(argv)

    report = measure()
    print(f"tools: {report['tools']}  chars: {report['chars']}  "
          f"~tokens: {report['tokens_estimate']}")
    for entry in report["heaviest"][:args.top]:
        print(f"  {entry['chars']:>6}  {entry['tool']}")
    if args.max_tokens is not None and report["tokens_estimate"] > args.max_tokens:
        print(f"FAIL: catalog estimate {report['tokens_estimate']} tokens exceeds "
              f"the budget of {args.max_tokens}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
