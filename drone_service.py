#!/usr/bin/env python3
"""Long-lived local service for Tello demo commands.

Run this in one terminal before starting Gemini Live:

    python drone_service.py

Worker subprocesses send short JSON commands to this service so the drone
connection, camera stream, and airborne state can survive across tool calls.
"""

from __future__ import annotations

import argparse
import base64
import json
import socketserver
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
from djitellopy import Tello


HOST = "127.0.0.1"
PORT = 8765
REPO_ROOT = Path(__file__).resolve().parent
DRONE_CAPTURE_DIR = REPO_ROOT / "drone_captures"


class DroneController:
    def __init__(self) -> None:
        self.drone: Tello | None = None
        self.connected = False
        self.stream_started = False
        self.took_off = False
        self.lock = threading.Lock()

    def _ensure_connected(self) -> Tello:
        if self.drone is None:
            self.drone = Tello()

        if not self.connected:
            self.drone.connect()
            self.connected = True

        return self.drone

    def _ensure_stream(self) -> Tello:
        drone = self._ensure_connected()
        if not self.stream_started:
            drone.streamon()
            self.stream_started = True
            time.sleep(2)

        return drone

    def _status_payload(
        self,
        drone: Tello | None = None,
        include_telemetry: bool = True,
    ) -> dict[str, Any]:
        if drone is None:
            drone = self.drone

        result: dict[str, Any] = {
            "connected": self.connected,
            "stream_started": self.stream_started,
            "airborne": self.took_off,
        }
        if not include_telemetry or not self.connected or drone is None:
            return result

        telemetry_getters = {
            "battery_percent": drone.get_battery,
            "height_cm": drone.get_height,
            "temperature_c": drone.get_temperature,
            "speed_x": drone.get_speed_x,
            "speed_y": drone.get_speed_y,
            "speed_z": drone.get_speed_z,
        }
        for key, getter in telemetry_getters.items():
            try:
                result[key] = getter()
            except Exception as exc:
                result[key] = f"unavailable: {exc}"

        return result

    def handle(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command == "frame":
            if not self.lock.acquire(blocking=False):
                return {"status": "busy", "message": "Drone is handling another command."}
            try:
                return self._handle_locked(command, payload)
            finally:
                self.lock.release()

        with self.lock:
            return self._handle_locked(command, payload)

    def _handle_locked(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if command == "ping":
                return {"status": "ok", "message": "drone_service.py is reachable"}

            if command == "connect":
                drone = self._ensure_connected()
                return {"status": "ok", "drone": self._status_payload(drone)}

            if command == "status":
                drone = self._ensure_connected()
                return {"status": "ok", "drone": self._status_payload(drone)}

            if command == "takeoff":
                drone = self._ensure_stream()
                if not self.took_off:
                    drone.takeoff()
                    self.took_off = True
                    time.sleep(2)
                return {"status": "ok", "drone": self._status_payload(drone)}

            if command == "land":
                drone = self._ensure_connected()
                if self.took_off:
                    drone.land()
                    self.took_off = False
                return {"status": "ok", "drone": self._status_payload(drone)}

            if command == "snapshot":
                return self.snapshot(payload)

            if command == "frame":
                return self.frame(payload)

            if command == "shutdown":
                self.close()
                return {"status": "ok", "message": "drone connection closed"}

            return {"status": "error", "message": f"Unsupported command: {command}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        drone = self._ensure_stream()
        frame_bgr = self._current_frame_bgr(drone, payload)

        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        capture_dir = DRONE_CAPTURE_DIR / session_id
        capture_dir.mkdir(parents=True, exist_ok=False)
        image_path = capture_dir / "tello_snapshot.jpg"

        if not cv2.imwrite(str(image_path), frame_bgr):
            return {"status": "error", "message": f"Failed to save {image_path}"}

        return {
            "status": "ok",
            "session_id": session_id,
            "capture_dir": str(capture_dir),
            "image": str(image_path),
            "drone": self._status_payload(drone),
        }

    def frame(self, payload: dict[str, Any]) -> dict[str, Any]:
        drone = self._ensure_stream()
        frame_bgr = self._current_frame_bgr(drone, payload)
        quality = int(payload.get("quality", 75))
        ok, encoded = cv2.imencode(
            ".jpg",
            frame_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, quality))],
        )
        if not ok:
            return {"status": "error", "message": "Failed to JPEG-encode drone frame."}

        return {
            "status": "ok",
            "mime_type": "image/jpeg",
            "image_base64": base64.b64encode(encoded.tobytes()).decode("ascii"),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "drone": self._status_payload(drone, include_telemetry=False),
        }

    def _current_frame_bgr(self, drone: Tello, payload: dict[str, Any]) -> Any:
        frame_read = drone.get_frame_read()
        time.sleep(float(payload.get("settle_seconds", 0.05)))
        frame = frame_read.frame
        if frame is None:
            raise RuntimeError("Drone camera returned no frame.")

        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def close(self) -> None:
        if self.drone is None:
            return

        try:
            if self.took_off:
                self.drone.land()
        except Exception as exc:
            print(f"Warning: land failed during shutdown: {exc}")
        finally:
            self.took_off = False

        try:
            if self.stream_started:
                self.drone.streamoff()
        except Exception as exc:
            print(f"Warning: streamoff failed during shutdown: {exc}")
        finally:
            self.stream_started = False

        try:
            self.drone.end()
        except Exception as exc:
            print(f"Warning: end failed during shutdown: {exc}")
        finally:
            self.drone = None
            self.connected = False


class DroneRequestHandler(socketserver.StreamRequestHandler):
    controller: DroneController

    def handle(self) -> None:
        raw = self.rfile.readline().decode("utf-8").strip()
        try:
            request = json.loads(raw)
            command = str(request.get("command", "ping"))
            payload = request.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {"text": str(payload)}
            response = self.controller.handle(command, payload)
        except json.JSONDecodeError as exc:
            response = {"status": "error", "message": f"Invalid JSON: {exc}"}

        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class DroneServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Tello service for Gemini demo.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    controller = DroneController()
    DroneRequestHandler.controller = controller

    with DroneServer((args.host, args.port), DroneRequestHandler) as server:
        print(f"Drone service listening on {args.host}:{args.port}")
        print("Press Ctrl+C to land, stop the stream, and close the drone connection.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping drone service...")
        finally:
            controller.close()


if __name__ == "__main__":
    main()
