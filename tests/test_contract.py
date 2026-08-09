"""Static contract checks for the SolidWorks MCP server."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
MANIFEST = ROOT / "manifest.json"


def source_tool_names() -> list[str]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        is_tool = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
        if is_tool:
            names.append(node.name)
    return names


class McpContractTests(unittest.TestCase):
    def test_server_syntax_is_valid(self) -> None:
        ast.parse(SERVER.read_text(encoding="utf-8"))

    def test_manifest_and_server_expose_the_same_tools(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest_names = [tool["name"] for tool in manifest["tools"]]
        source_names = source_tool_names()
        self.assertGreater(len(source_names), 0)
        self.assertGreater(len(manifest_names), 0)
        self.assertEqual(len(source_names), len(set(source_names)))
        self.assertEqual(len(manifest_names), len(set(manifest_names)))
        self.assertEqual(set(source_names), set(manifest_names))

    def test_readme_version_and_tool_count_match_manifest(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(manifest["version"], readme)
        self.assertIn(f"**{len(manifest['tools'])} ferramentas**", readme)

    def test_mcpb_and_test_output_are_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.mcpb", gitignore)
        self.assertIn("tests/output/", gitignore)

    def test_source_files_have_no_common_credential_patterns(self) -> None:
        credential_pattern = re.compile(
            r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
            r"(?:xox[baprs]-|gh[pousr]_)[A-Za-z0-9_-]{20,}|"
            r"\bsk-[A-Za-z0-9]{20,}\b",
            re.IGNORECASE,
        )
        for path in (SERVER, MANIFEST, ROOT / "README.md", ROOT / "requirements.txt"):
            with self.subTest(path=path.name):
                self.assertIsNone(credential_pattern.search(path.read_text(encoding="utf-8")))

    def test_public_manifest_does_not_expose_local_author_name(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["author"]["name"], "SolidWorks MCP Contributors")

    def test_execute_python_requires_explicit_opt_in(self) -> None:
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn("SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON", source)
        self.assertIn("execute_python is disabled by default", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
