from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_config(client: str, root: Path = ROOT, python: str = sys.executable) -> dict:
    root = root.resolve()
    server = {
        "command": str(Path(python).resolve()),
        "args": ["-m", "backend.mcp.server"],
        "cwd": str(root),
    }
    if client == "vscode":
        vscode_server = {
            "type": "stdio",
            "command": server["command"],
            "args": server["args"],
            "env": {"PYTHONPATH": str(root)},
        }
        return {"servers": {"ai-finance-assistant": vscode_server}}
    return {"mcpServers": {"ai-finance-assistant": server}}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="根据当前项目路径和 Python 解释器生成 MCP 客户端配置。"
    )
    parser.add_argument(
        "--client", choices=("claude", "cursor", "generic", "vscode"),
        default="generic", help="目标客户端配置格式。",
    )
    args = parser.parse_args()
    venv_python = (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    current_python = Path(sys.executable).resolve()
    if current_python != venv_python:
        print(
            r"警告：当前 Python 不在项目 .venv 中。建议使用 .venv\Scripts\python.exe "
            "重新运行，以确保 MCP 依赖可用。",
            file=sys.stderr,
        )
    print(json.dumps(build_config(args.client), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
