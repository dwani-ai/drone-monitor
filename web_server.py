#!/usr/bin/env python3
"""Dependency-free web API for the React drone monitor dashboard."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_EVENT_LOG_PATH = REPO_ROOT / ".gemini_live_events.jsonl"
EVENT_LOG_PATH = Path(
    os.getenv("GEMINI_LIVE_EVENT_LOG", str(DEFAULT_EVENT_LOG_PATH))
).expanduser()
DRONE_SERVICE_HOST = os.getenv("DRONE_SERVICE_HOST", "127.0.0.1")
DRONE_SERVICE_PORT = int(os.getenv("DRONE_SERVICE_PORT", "8765"))


def call_drone_service(command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = json.dumps({"command": command, "payload": payload or {}}) + "\n"
    try:
        with socket.create_connection(
            (DRONE_SERVICE_HOST, DRONE_SERVICE_PORT),
            timeout=10,
        ) as sock:
            sock.sendall(request.encode("utf-8"))
            response = sock.makefile("r", encoding="utf-8").readline()
    except OSError as exc:
        return {
            "status": "error",
            "message": (
                f"Drone service is not reachable at "
                f"{DRONE_SERVICE_HOST}:{DRONE_SERVICE_PORT}: {exc}"
            ),
        }

    if not response:
        return {"status": "error", "message": "Drone service returned no response."}

    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "message": f"Drone service returned invalid JSON: {exc}",
            "raw_response": response,
        }

    return parsed if isinstance(parsed, dict) else {"status": "error", "message": response}


def read_recent_events(limit: int = 50) -> list[dict[str, Any]]:
    if not EVENT_LOG_PATH.exists():
        return []

    lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)

    return events


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DroneDashboard/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "drone_service": call_drone_service("ping"),
                    "event_log": str(EVENT_LOG_PATH),
                }
            )
            return

        if path == "/api/drone/status":
            self.send_json(call_drone_service("status"))
            return

        if path == "/api/drone/frame":
            self.send_frame()
            return

        if path == "/api/drone/stream":
            self.send_mjpeg_stream()
            return

        if path == "/api/events/recent":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["50"])[0])
            self.send_json({"status": "ok", "events": read_recent_events(limit)})
            return

        if path == "/events":
            self.send_event_stream()
            return

        self.send_json(
            {"status": "error", "message": f"Unknown route: {path}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        prefix = "/api/drone/"
        if not path.startswith(prefix):
            self.send_json(
                {"status": "error", "message": f"Unknown route: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        command = path.removeprefix(prefix)
        if command not in {"connect", "status", "takeoff", "land", "shutdown", "snapshot"}:
            self.send_json(
                {"status": "error", "message": f"Unsupported drone command: {command}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        self.send_json(call_drone_service(command, self.read_json_body()))

    def read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}

        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        return parsed if isinstance(parsed, dict) else {}

    def send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def send_frame(self) -> None:
        result = call_drone_service("frame", {"quality": 80})
        if result.get("status") != "ok":
            self.send_json(result, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return

        frame_bytes = base64.b64decode(str(result["image_base64"]))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", str(result.get("mime_type", "image/jpeg")))
        self.send_header("Content-Length", str(len(frame_bytes)))
        self.end_headers()
        self.wfile.write(frame_bytes)

    def send_mjpeg_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        while True:
            result = call_drone_service(
                "frame",
                {"quality": 75, "settle_seconds": 0.01},
            )
            if result.get("status") == "ok":
                frame_bytes = base64.b64decode(str(result["image_base64"]))
                try:
                    self.wfile.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"X-Captured-At: {result.get('captured_at', '')}\r\n\r\n".encode(
                            "utf-8"
                        )
                        + frame_bytes
                        + b"\r\n"
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            time.sleep(0.2)

    def send_event_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        position = EVENT_LOG_PATH.stat().st_size if EVENT_LOG_PATH.exists() else 0
        while True:
            try:
                if not EVENT_LOG_PATH.exists():
                    time.sleep(0.5)
                    continue

                size = EVENT_LOG_PATH.stat().st_size
                if size < position:
                    position = 0

                if size > position:
                    with EVENT_LOG_PATH.open("r", encoding="utf-8") as event_file:
                        event_file.seek(position)
                        for line in event_file:
                            line = line.strip()
                            if not line:
                                continue
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        position = event_file.tell()

                time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the dashboard API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.block_on_close = False
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard API listening on http://{args.host}:{args.port}")
    print(f"Gemini Live event log: {EVENT_LOG_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard API...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
