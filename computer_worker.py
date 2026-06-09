#!/usr/bin/env python3
"""Example external program invoked by Gemini Live tool calls.

Replace or extend the command handlers in this file with the real computer or
drone actions you want the model to be able to request.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def handle_command(command: str, payload: str) -> dict[str, Any]:
    """Run one allowlisted command and return JSON-serializable output."""
    if command == "status":
        return {
            "status": "ok",
            "message": "computer_worker.py is reachable",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    if command == "echo":
        return {
            "status": "ok",
            "message": payload,
        }

    if command == "list_repo_files":
        repo_root = Path(__file__).resolve().parent
        files = sorted(
            path.name
            for path in repo_root.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
        return {
            "status": "ok",
            "files": files,
        }

    return {
        "status": "error",
        "message": f"Unsupported command: {command}",
        "supported_commands": ["status", "echo", "list_repo_files"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--payload", default="")
    args = parser.parse_args()

    print(json.dumps(handle_command(args.command, args.payload)))


if __name__ == "__main__":
    main()
