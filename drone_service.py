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
import os
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
TELLO_HOST = os.getenv("TELLO_HOST", "192.168.10.1")
TELLO_RETRY_COUNT = int(os.getenv("TELLO_RETRY_COUNT", "1"))
MOVE_INCREMENT_CM = 5
MOVE_RC_VELOCITY = int(os.getenv("TELLO_MOVE_RC_VELOCITY", "20"))
MOVE_RC_SECONDS = float(os.getenv("TELLO_MOVE_RC_SECONDS", "0.25"))
TURN_INCREMENT_DEGREES = 15
TURN_RC_VELOCITY = int(os.getenv("TELLO_TURN_RC_VELOCITY", "40"))
TURN_RC_SECONDS = float(os.getenv("TELLO_TURN_RC_SECONDS", "0.25"))
# The Tello firmware auto-lands if it receives no command for ~15 seconds.
# Send a heartbeat well under that window while airborne so the drone does not
# land itself during the quiet gaps between voice commands.
KEEPALIVE_INTERVAL_SECONDS = float(os.getenv("TELLO_KEEPALIVE_SECONDS", "5"))
REPO_ROOT = Path(__file__).resolve().parent
DRONE_CAPTURE_DIR = REPO_ROOT / "drone_captures"


class DroneController:
    def __init__(self) -> None:
        self.drone: Tello | None = None
        self.connected = False
        self.stream_started = False
        self.took_off = False
        self.lock = threading.Lock()
        self.command_sequence = 0
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None

    def _start_keepalive(self) -> None:
        """Begin sending periodic heartbeats so the Tello does not auto-land."""
        # Clear the stop flag first so a still-running thread from a previous
        # flight keeps beating instead of exiting after a quick land/takeoff.
        self._keepalive_stop.clear()
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return

        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="tello-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        self._keepalive_stop.set()

    def _keepalive_loop(self) -> None:
        """Reset the Tello auto-land timer every few seconds while airborne.

        Uses a zero RC-control packet rather than send_keepalive(): RC control is
        fire-and-forget (no response wait), so it cannot hold self.lock for the
        7s command timeout and delay a user's land/stop command.
        """
        while not self._keepalive_stop.wait(KEEPALIVE_INTERVAL_SECONDS):
            with self.lock:
                if not self.took_off or self.drone is None:
                    continue
                try:
                    self.drone.send_rc_control(0, 0, 0, 0)
                except Exception as exc:
                    print(f"Warning: keepalive failed: {exc}")

    def _ensure_connected(self) -> Tello:
        if self.drone is None:
            self.drone = Tello(host=TELLO_HOST, retry_count=TELLO_RETRY_COUNT)

        if not self.connected:
            try:
                self.drone.connect()
                self.connected = True
            except Exception as exc:
                self.drone = None
                self.connected = False
                raise RuntimeError(
                    "Could not reach the Tello drone. Make sure the drone is powered "
                    f"on and this computer is connected to the Tello Wi-Fi network "
                    f"({TELLO_HOST}). Original error: {exc}"
                ) from exc

        return self.drone

    def _ensure_stream(self) -> Tello:
        drone = self._ensure_connected()
        if not self.stream_started:
            drone.streamon()
            self.stream_started = True
            time.sleep(2)

        return drone

    def _looks_airborne(self, drone: Tello) -> bool:
        """Best-effort check that the drone is really flying.

        Tello height telemetry is noisy and can briefly read 0, so sample a few
        times and treat any clearly positive height as airborne. Used to detect
        a stale took_off flag after a firmware auto-land.
        """
        for _ in range(3):
            try:
                if drone.get_height() >= 10:
                    return True
            except Exception:
                return True  # Telemetry unavailable; do not relaunch blindly.
            time.sleep(0.2)

        return False

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
                # Tello SDK flight control only needs the command connection.
                # Video streaming is started lazily by frame/snapshot requests.
                drone = self._ensure_connected()
                if self.took_off and self._looks_airborne(drone):
                    return {
                        "status": "ok",
                        "already_done": True,
                        "message": "Drone is already airborne.",
                        "drone": self._status_payload(drone),
                    }

                # If took_off is stale (the firmware auto-landed during a quiet
                # gap), fall through and actually take off again.
                self.took_off = False
                self._stop_keepalive()

                try:
                    drone.takeoff()
                except Exception as exc:
                    return self.drone_error(
                        "takeoff_rejected",
                        "The drone rejected takeoff.",
                        exc,
                        drone,
                        recovery=(
                            "Check battery, propeller clearance, and that the drone is "
                            "on a stable surface. Then try takeoff once."
                        ),
                    )

                self.took_off = True
                drone.is_flying = True
                self._start_keepalive()
                time.sleep(2)
                return {
                    "status": "ok",
                    "message": "Drone takeoff completed.",
                    "drone": self._status_payload(drone),
                }

            if command == "land":
                drone = self._ensure_connected()
                if payload.get("source") in {"gemini_live", "dashboard"} and not payload.get(
                    "confirmed_land"
                ):
                    return {
                        "status": "error",
                        "error_code": "confirmation_required",
                        "message": "Landing needs explicit confirmation.",
                        "recovery": "Ask the user to confirm landing before sending land.",
                        "drone": self._status_payload(drone),
                    }

                if not self.took_off:
                    return {
                        "status": "ok",
                        "already_done": True,
                        "message": "Drone is already landed.",
                        "drone": self._status_payload(drone),
                    }

                self.stop_motion()
                try:
                    drone.land()
                except Exception as exc:
                    status = self._status_payload(drone)
                    height = status.get("height_cm")
                    if height == 0:
                        self.took_off = False
                        drone.is_flying = False
                        self._stop_keepalive()
                        status["airborne"] = False
                        return {
                            "status": "ok",
                            "already_done": True,
                            "message": "Landing command was rejected, but telemetry says height is 0 cm.",
                            "drone": status,
                        }

                    return self.drone_error(
                        "land_rejected",
                        "The drone rejected landing.",
                        exc,
                        drone,
                        recovery=(
                            "Use stop, make sure the drone is stable, then try land once. "
                            "If it is already on the floor, use shutdown after confirming."
                        ),
                    )

                self.took_off = False
                drone.is_flying = False
                self._stop_keepalive()
                return {
                    "status": "ok",
                    "message": "Drone landed.",
                    "drone": self._status_payload(drone),
                }

            if command in {
                "forward",
                "back",
                "left",
                "right",
                "up",
                "down",
                "turn_left",
                "turn_right",
                "stop",
            }:
                return self.move(command)

            if command == "snapshot":
                return self.snapshot(payload)

            if command == "frame":
                return self.frame(payload)

            if command == "shutdown":
                self.close()
                return {"status": "ok", "message": "drone connection closed"}

            return {
                "status": "error",
                "error_code": "unsupported_command",
                "message": f"Unsupported command: {command}",
                "recovery": "Use one of the supported drone_control actions.",
            }
        except Exception as exc:
            return self.drone_error(
                "drone_command_failed",
                "The drone command failed.",
                exc,
                self.drone,
                recovery="Check the drone connection and try the command once.",
            )

    def drone_error(
        self,
        error_code: str,
        message: str,
        exc: Exception,
        drone: Tello | None,
        recovery: str,
    ) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": error_code,
            "message": message,
            "detail": str(exc),
            "recovery": recovery,
            "drone": self._status_payload(drone),
        }

    def move(self, command: str) -> dict[str, Any]:
        # Movement only needs the command channel. Avoid _ensure_stream here so
        # the first move does not pay the streamon + 2s warmup cost and does not
        # add video traffic that competes with flight control.
        drone = self._ensure_connected()
        if not self.took_off:
            return {
                "status": "error",
                "error_code": "not_airborne",
                "message": "Drone is not airborne.",
                "recovery": "Say take off before movement commands.",
                "drone": self._status_payload(drone),
            }

        if command == "stop":
            self.stop_motion()
            return {
                "status": "ok",
                "movement": "stop",
                "drone": self._status_payload(drone),
            }

        rc_vectors = {
            "forward": (0, MOVE_RC_VELOCITY, 0, 0),
            "back": (0, -MOVE_RC_VELOCITY, 0, 0),
            "left": (-MOVE_RC_VELOCITY, 0, 0, 0),
            "right": (MOVE_RC_VELOCITY, 0, 0, 0),
            "up": (0, 0, MOVE_RC_VELOCITY, 0),
            "down": (0, 0, -MOVE_RC_VELOCITY, 0),
            "turn_left": (0, 0, 0, -TURN_RC_VELOCITY),
            "turn_right": (0, 0, 0, TURN_RC_VELOCITY),
        }
        rc_vector = rc_vectors[command]
        duration = TURN_RC_SECONDS if command.startswith("turn_") else MOVE_RC_SECONDS
        increment = (
            f"{TURN_INCREMENT_DEGREES} degrees"
            if command.startswith("turn_")
            else f"{MOVE_INCREMENT_CM} cm"
        )

        drone.send_rc_control(*rc_vector)
        time.sleep(duration)
        self.stop_motion()
        time.sleep(0.15)

        return {
            "status": "ok",
            "movement": command,
            "increment": increment,
            "rc_velocity": rc_vector,
            "duration_seconds": duration,
            "drone": self._status_payload(drone),
        }

    def stop_motion(self) -> None:
        if self.drone is not None:
            self.drone.send_rc_control(0, 0, 0, 0)

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
        self._stop_keepalive()
        if self.drone is None:
            return

        try:
            if self.took_off:
                self.drone.land()
        except Exception as exc:
            print(f"Warning: land failed during shutdown: {exc}")
        finally:
            self.took_off = False
            # Prevent djitellopy.end()/__del__ from issuing a second land command.
            if self.drone is not None:
                self.drone.is_flying = False

        try:
            if self.stream_started:
                self.drone.streamoff()
        except Exception as exc:
            print(f"Warning: streamoff failed during shutdown: {exc}")
        finally:
            self.stream_started = False
            if self.drone is not None:
                self.drone.stream_on = False

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
            source = str(payload.get("source") or "unknown")
            self.controller.command_sequence += 1
            sequence = self.controller.command_sequence
            if command != "frame":
                print(f"Drone request #{sequence}: command={command} source={source}")
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
        print(
            f"Tello target: {TELLO_HOST} "
            f"(retry_count={TELLO_RETRY_COUNT}; override with TELLO_HOST/TELLO_RETRY_COUNT)"
        )
        print("Press Ctrl+C to land, stop the stream, and close the drone connection.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping drone service...")
        finally:
            controller.close()


if __name__ == "__main__":
    main()
