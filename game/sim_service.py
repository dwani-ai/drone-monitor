#!/usr/bin/env python3
"""Browser drone-simulator service.

Drop-in replacement for ``drone_service.py`` during the simulator demo. It
speaks the exact same TCP JSON protocol on port 8765, so the existing voice
stack (``gemini_live_computer_use.py`` -> ``computer_worker.py``) drives the
simulated drone with no changes. A second HTTP server feeds the Three.js browser
game its authoritative pose and receives the rendered first-person frames that
back the "what do you see?" / explore vision path.

Run this instead of ``drone_service.py``:

    python game/sim_service.py

Then start the browser game (``cd game && npm run dev``) and the unchanged voice
client (``python gemini_live_computer_use.py --vertexai``).
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socketserver
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


HOST = "127.0.0.1"
TCP_PORT = int(os.getenv("DRONE_SERVICE_PORT", "8765"))
HTTP_PORT = int(os.getenv("SIM_HTTP_PORT", "8200"))
MOVE_INCREMENT_CM = 5
TURN_INCREMENT_DEGREES = 15
FLOOR_CLEARANCE_CM = 20  # Lowest hover height so the drone never lands on a move.

REPO_ROOT = Path(__file__).resolve().parent.parent
GAME_ROOT = Path(__file__).resolve().parent
LAYOUT_PATH = GAME_ROOT / "layout.json"
DRONE_CAPTURE_DIR = REPO_ROOT / "drone_captures"
DEFAULT_EVENT_LOG_PATH = REPO_ROOT / ".gemini_live_events.jsonl"
EVENT_LOG_PATH = Path(
    os.getenv("GEMINI_LIVE_EVENT_LOG", str(DEFAULT_EVENT_LOG_PATH))
).expanduser()


def load_layout() -> dict[str, Any]:
    return json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))


class SimDrone:
    """Authoritative simulated drone state shared by the TCP and HTTP servers."""

    def __init__(self, layout: dict[str, Any]) -> None:
        self.layout = layout
        self.lock = threading.RLock()

        start = layout.get("start", {})
        self.x = float(start.get("x", 0.0))
        self.y = float(start.get("y", 0.0))
        self.z = 0.0
        self.yaw = float(start.get("yaw", 0.0))

        drone_cfg = layout.get("drone", {})
        self.radius = float(drone_cfg.get("radius", 20))
        self.takeoff_height = float(drone_cfg.get("takeoff_height", 70))

        self.connected = False
        self.airborne = False
        self.stream_started = False
        self.battery = 100.0
        self.last_command = "none"
        self.command_sequence = 0

        self.frame_b64: str | None = None
        self.frame_captured_at: str | None = None

    # -- telemetry -----------------------------------------------------------
    def status_payload(self, include_telemetry: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "connected": self.connected,
            "stream_started": self.stream_started,
            "airborne": self.airborne,
        }
        if include_telemetry:
            result.update(
                {
                    "battery_percent": int(round(self.battery)),
                    "height_cm": int(round(self.z)),
                    "temperature_c": 72.0,
                    "speed_x": 0,
                    "speed_y": 0,
                    "speed_z": 0,
                }
            )
        return result

    def pose_payload(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "z": round(self.z, 2),
            "yaw": round(self.yaw % 360, 2),
        }

    def _drain(self, amount: float) -> None:
        self.battery = max(1.0, self.battery - amount)

    # -- physics -------------------------------------------------------------
    def _heading(self) -> tuple[tuple[float, float], tuple[float, float]]:
        rad = math.radians(self.yaw)
        forward = (math.sin(rad), math.cos(rad))
        right = (math.cos(rad), -math.sin(rad))
        return forward, right

    def _blocking_obstacle(self, cx: float, cy: float, cz: float) -> str | None:
        """Return a label if the candidate position is out of bounds or hits a box."""
        room = self.layout.get("room", {})
        half_w = float(room.get("width", 600)) / 2
        half_d = float(room.get("depth", 400)) / 2
        height = float(room.get("height", 250))
        r = self.radius

        if cx < -half_w + r or cx > half_w - r:
            return "wall"
        if cy < -half_d + r or cy > half_d - r:
            return "wall"
        if cz > height - r:
            return "ceiling"
        if cz < FLOOR_CLEARANCE_CM:
            return "floor"

        for obstacle in self.layout.get("obstacles", []):
            ox = float(obstacle["x"])
            oy = float(obstacle["y"])
            ow = float(obstacle["w"]) / 2
            od = float(obstacle["d"]) / 2
            oh = float(obstacle["h"])
            within_x = (ox - ow - r) <= cx <= (ox + ow + r)
            within_y = (oy - od - r) <= cy <= (oy + od + r)
            within_z = 0 <= cz <= (oh + r)
            if within_x and within_y and within_z:
                return str(obstacle.get("label", obstacle.get("id", "obstacle")))

        return None

    def _translate(self, command: str) -> dict[str, Any]:
        forward, right = self._heading()
        deltas = {
            "forward": (forward[0] * MOVE_INCREMENT_CM, forward[1] * MOVE_INCREMENT_CM, 0.0),
            "back": (-forward[0] * MOVE_INCREMENT_CM, -forward[1] * MOVE_INCREMENT_CM, 0.0),
            "right": (right[0] * MOVE_INCREMENT_CM, right[1] * MOVE_INCREMENT_CM, 0.0),
            "left": (-right[0] * MOVE_INCREMENT_CM, -right[1] * MOVE_INCREMENT_CM, 0.0),
            "up": (0.0, 0.0, MOVE_INCREMENT_CM),
            "down": (0.0, 0.0, -MOVE_INCREMENT_CM),
        }
        dx, dy, dz = deltas[command]
        cx, cy, cz = self.x + dx, self.y + dy, self.z + dz

        blocker = self._blocking_obstacle(cx, cy, cz)
        if blocker is not None:
            return {
                "status": "ok",
                "movement": command,
                "blocked": True,
                "obstacle": blocker,
                "increment": f"{MOVE_INCREMENT_CM} cm",
                "drone": self.status_payload(),
                "spoken_message": (
                    f"I can't move {command}, the {blocker} is in the way."
                ),
            }

        self.x, self.y, self.z = cx, cy, cz
        self._drain(0.25)
        return {
            "status": "ok",
            "movement": command,
            "increment": f"{MOVE_INCREMENT_CM} cm",
            "drone": self.status_payload(),
        }

    def _turn(self, command: str) -> dict[str, Any]:
        delta = TURN_INCREMENT_DEGREES if command == "turn_right" else -TURN_INCREMENT_DEGREES
        self.yaw = (self.yaw + delta) % 360
        self._drain(0.15)
        return {
            "status": "ok",
            "movement": command,
            "increment": f"{TURN_INCREMENT_DEGREES} degrees",
            "drone": self.status_payload(),
        }

    # -- command dispatch ----------------------------------------------------
    def handle(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.last_command = command
            return self._handle_locked(command, payload)

    def _handle_locked(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command == "ping":
            return {"status": "ok", "message": "sim_service.py is reachable"}

        if command in {"connect", "status"}:
            self.connected = True
            return {"status": "ok", "drone": self.status_payload()}

        if command == "takeoff":
            self.connected = True
            if self.airborne:
                return {
                    "status": "ok",
                    "already_done": True,
                    "message": "Drone is already airborne.",
                    "drone": self.status_payload(),
                }
            self.airborne = True
            self.z = self.takeoff_height
            self._drain(0.5)
            return {
                "status": "ok",
                "message": "Drone takeoff completed.",
                "drone": self.status_payload(),
            }

        if command == "land":
            self.connected = True
            if payload.get("source") in {"gemini_live", "dashboard"} and not payload.get(
                "confirmed_land"
            ):
                return {
                    "status": "error",
                    "error_code": "confirmation_required",
                    "message": "Landing needs explicit confirmation.",
                    "recovery": "Ask the user to confirm landing before sending land.",
                    "drone": self.status_payload(),
                }
            if not self.airborne:
                return {
                    "status": "ok",
                    "already_done": True,
                    "message": "Drone is already landed.",
                    "drone": self.status_payload(),
                }
            self.airborne = False
            self.z = 0.0
            return {
                "status": "ok",
                "message": "Drone landed.",
                "drone": self.status_payload(),
            }

        if command in {"forward", "back", "left", "right", "up", "down", "turn_left", "turn_right", "stop"}:
            if not self.airborne:
                return {
                    "status": "error",
                    "error_code": "not_airborne",
                    "message": "Drone is not airborne.",
                    "recovery": "Say take off before movement commands.",
                    "drone": self.status_payload(),
                }
            if command == "stop":
                return {"status": "ok", "movement": "stop", "drone": self.status_payload()}
            if command.startswith("turn_"):
                return self._turn(command)
            return self._translate(command)

        if command == "snapshot":
            return self._snapshot()

        if command == "frame":
            return self._frame()

        if command == "shutdown":
            self.airborne = False
            self.z = 0.0
            self.connected = False
            self.stream_started = False
            return {"status": "ok", "message": "drone connection closed"}

        return {
            "status": "error",
            "error_code": "unsupported_command",
            "message": f"Unsupported command: {command}",
            "recovery": "Use one of the supported drone_control actions.",
        }

    # -- camera --------------------------------------------------------------
    def _decode_frame(self) -> bytes | None:
        if not self.frame_b64:
            return None
        try:
            return base64.b64decode(self.frame_b64)
        except (ValueError, base64.binascii.Error):
            return None

    def _snapshot(self) -> dict[str, Any]:
        self.stream_started = True
        frame_bytes = self._decode_frame()
        if frame_bytes is None:
            return {
                "status": "error",
                "error_code": "no_frame",
                "message": "No simulator camera frame yet.",
                "recovery": "Open the game in the browser so it streams frames, then retry.",
                "drone": self.status_payload(),
            }

        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        capture_dir = DRONE_CAPTURE_DIR / session_id
        capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = capture_dir / "tello_snapshot.jpg"
        image_path.write_bytes(frame_bytes)

        return {
            "status": "ok",
            "session_id": session_id,
            "capture_dir": str(capture_dir),
            "image": str(image_path),
            "drone": self.status_payload(),
        }

    def _frame(self) -> dict[str, Any]:
        self.stream_started = True
        if not self.frame_b64:
            return {
                "status": "error",
                "error_code": "no_frame",
                "message": "No simulator camera frame yet.",
                "drone": self.status_payload(include_telemetry=False),
            }
        return {
            "status": "ok",
            "mime_type": "image/jpeg",
            "image_base64": self.frame_b64,
            "captured_at": self.frame_captured_at
            or datetime.now(timezone.utc).isoformat(),
            "drone": self.status_payload(include_telemetry=False),
        }

    def set_frame(self, image_b64: str) -> None:
        with self.lock:
            self.frame_b64 = image_b64
            self.frame_captured_at = datetime.now(timezone.utc).isoformat()
            self.stream_started = True

    def state_payload(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": "ok",
                "drone": self.status_payload(),
                "pose": self.pose_payload(),
                "last_command": self.last_command,
                "has_frame": self.frame_b64 is not None,
            }


# ---------------------------------------------------------------------------
# TCP server: identical wire protocol to drone_service.py
# ---------------------------------------------------------------------------
class SimRequestHandler(socketserver.StreamRequestHandler):
    controller: SimDrone

    def handle(self) -> None:
        raw = self.rfile.readline().decode("utf-8").strip()
        try:
            request = json.loads(raw)
            command = str(request.get("command", "ping"))
            payload = request.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {"text": str(payload)}
            source = str(payload.get("source") or "unknown")
            self.controller.command_sequence += 1
            sequence = self.controller.command_sequence
            if command != "frame":
                print(f"Sim request #{sequence}: command={command} source={source}")
            response = self.controller.handle(command, payload)
        except json.JSONDecodeError as exc:
            response = {"status": "error", "message": f"Invalid JSON: {exc}"}

        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class SimTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


# ---------------------------------------------------------------------------
# HTTP server: feeds the browser game
# ---------------------------------------------------------------------------
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


class SimHTTPHandler(BaseHTTPRequestHandler):
    server_version = "DroneSim/1.0"
    controller: SimDrone
    layout: dict[str, Any]

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/sim/state":
            self.send_json(self.controller.state_payload())
            return
        if path == "/api/sim/layout":
            self.send_json(self.layout)
            return
        if path == "/api/health":
            self.send_json({"status": "ok", "mode": "simulator"})
            return
        if path == "/events":
            self.send_event_stream()
            return
        self.send_json(
            {"status": "error", "message": f"Unknown route: {path}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/sim/frame":
            self.send_json(
                {"status": "error", "message": f"Unknown route: {path}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        body = self.read_json_body()
        image_b64 = body.get("image_base64") or body.get("image")
        if not isinstance(image_b64, str) or not image_b64:
            self.send_json(
                {"status": "error", "message": "image_base64 is required."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        # Tolerate a data URL prefix such as "data:image/jpeg;base64,".
        if "," in image_b64 and image_b64.strip().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]

        self.controller.set_frame(image_b64)
        self.send_json({"status": "ok"})

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

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
        # Quiet by default; frame posts and state polls are very frequent.
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browser drone-simulator service.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--tcp-port", type=int, default=TCP_PORT)
    parser.add_argument("--http-port", type=int, default=HTTP_PORT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    layout = load_layout()
    controller = SimDrone(layout)

    SimRequestHandler.controller = controller
    SimHTTPHandler.controller = controller
    SimHTTPHandler.layout = layout

    tcp_server = SimTCPServer((args.host, args.tcp_port), SimRequestHandler)
    http_server = ThreadingHTTPServer((args.host, args.http_port), SimHTTPHandler)
    http_server.daemon_threads = True

    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()

    print(f"Sim drone protocol (TCP) on {args.host}:{args.tcp_port} (drone_service.py compatible)")
    print(f"Sim browser API (HTTP) on http://{args.host}:{args.http_port}")
    print(f"Room: {layout['room']['width']}x{layout['room']['depth']}x{layout['room']['height']} cm, "
          f"{len(layout.get('obstacles', []))} obstacles")
    print("Run the game (cd game && npm run dev) and the voice client, then say 'Take off'.")
    print("Press Ctrl+C to stop.")

    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping simulator service...")
    finally:
        tcp_server.shutdown()
        tcp_server.server_close()
        http_server.server_close()


if __name__ == "__main__":
    main()
