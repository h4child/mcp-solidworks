"""Build the ChatGPT plugin distribution from the main source tree.

Until v5.22.0 the plugin lived as a hand-maintained copy of the whole server
in ``chatgpt-solidworks-mcp-plugin/``. It drifted: by v5.21.5 its server.py
differed from the real one by ~900 lines and still carried bugs already fixed
here. The plugin is now *generated* -- the only plugin-specific files are the
two manifests in ``packaging/chatgpt/``; everything else is copied from the
repository root, and the version is taken from pyproject.toml so it cannot
lag behind.

Usage:
    python scripts/build_chatgpt_plugin.py            # -> dist/chatgpt-plugin/
    python scripts/build_chatgpt_plugin.py --zip      # also writes the .zip
    python scripts/build_chatgpt_plugin.py --out DIR  # custom output folder
"""

import argparse
import json
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(ROOT, "packaging", "chatgpt")

# Everything the server needs at runtime: server.py imports the two geometry
# modules, reads README.md for the solidworks://tool-status resource and
# exposes .claude/knowledge/* as resources. pyproject/uv.lock let `uv run`
# resolve dependencies on the user's machine.
RUNTIME_FILES = (
    "server.py",
    "alfa_drawing.py",
    "gear_geometry.py",
    "README.md",
    "pyproject.toml",
    "uv.lock",
)
RUNTIME_DIRS = (".claude",)
EXCLUDED_NAMES = {"settings.local.json", "__pycache__"}


def project_version() -> str:
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.MULTILINE)
    if not match:
        raise SystemExit("pyproject.toml has no [project] version")
    return match.group(1)


def _ignore(_directory, names):
    return [name for name in names if name in EXCLUDED_NAMES]


def build(out_dir: str) -> str:
    version = project_version()
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    for name in RUNTIME_FILES:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(out_dir, name))
    for name in RUNTIME_DIRS:
        shutil.copytree(os.path.join(ROOT, name), os.path.join(out_dir, name),
                        ignore=_ignore)

    with open(os.path.join(PACKAGING, "plugin.json"), encoding="utf-8") as handle:
        plugin = json.load(handle)
    plugin["version"] = version
    with open(os.path.join(out_dir, "plugin.json"), "w", encoding="utf-8") as handle:
        json.dump(plugin, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    shutil.copy2(os.path.join(PACKAGING, "mcp.json"), os.path.join(out_dir, "mcp.json"))
    return version


def make_zip(out_dir: str, version: str) -> str:
    zip_path = os.path.join(os.path.dirname(out_dir),
                            f"solidworks-mcp-chatgpt-plugin-{version}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, _dirs, files in os.walk(out_dir):
            for name in files:
                full = os.path.join(folder, name)
                archive.write(full, os.path.relpath(full, out_dir))
    return zip_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "chatgpt-plugin"))
    parser.add_argument("--zip", action="store_true", help="also write a .zip next to --out")
    args = parser.parse_args(argv)
    out_dir = os.path.abspath(args.out)
    version = build(out_dir)
    print(f"built ChatGPT plugin v{version} in {out_dir}")
    if args.zip:
        print(f"wrote {make_zip(out_dir, version)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
