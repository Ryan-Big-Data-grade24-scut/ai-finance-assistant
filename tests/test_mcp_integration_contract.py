from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_tool_registry_matches_expected_tool_contract():
    source = (ROOT / "backend/mcp/server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    decorated = set()
    expected = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if any(
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "tool"
                for dec in node.decorator_list
            ):
                decorated.add(node.name)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EXPECTED_TOOLS"
            for target in node.targets
        ):
            expected = set(ast.literal_eval(node.value))
    assert expected is not None
    assert len(decorated) == 27
    assert decorated == expected


def test_mcp_config_generator_emits_client_specific_shapes(tmp_path):
    script = ROOT / "scripts/print_mcp_config.py"
    spec = importlib.util.spec_from_file_location("print_mcp_config", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    claude = module.build_config("claude", root=tmp_path, python=str(python))
    assert claude["mcpServers"]["ai-finance-assistant"]["command"] == str(python.resolve())
    assert claude["mcpServers"]["ai-finance-assistant"]["cwd"] == str(tmp_path.resolve())
    assert claude["mcpServers"]["ai-finance-assistant"]["args"] == ["-m", "backend.mcp.server"]

    vscode = module.build_config("vscode", root=tmp_path, python=str(python))
    assert vscode["servers"]["ai-finance-assistant"]["type"] == "stdio"
    assert vscode["servers"]["ai-finance-assistant"]["env"]["PYTHONPATH"] == str(tmp_path.resolve())
    assert "cwd" not in vscode["servers"]["ai-finance-assistant"]
    assert "mcpServers" not in vscode
