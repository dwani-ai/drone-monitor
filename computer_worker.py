#!/usr/bin/env python3
"""Example external program invoked by Gemini Live tool calls.

Replace or extend the command handlers in this file with the real computer or
drone actions you want the model to be able to request.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse


SUPPORTED_COMMANDS = [
    "status",
    "echo",
    "list_repo_files",
    "browser_open_url",
    "browser_search",
    "browser_get_text",
    "browser_click_text",
    "browser_type_text",
    "browser_screenshot",
    "drone_connect",
    "drone_status",
    "drone_takeoff",
    "drone_snapshot",
    "drone_explore",
    "drone_forward",
    "drone_back",
    "drone_left",
    "drone_right",
    "drone_up",
    "drone_down",
    "drone_turn_left",
    "drone_turn_right",
    "drone_stop",
    "drone_land",
    "drone_shutdown",
    "drone_run_simple",
    "drone_look_around",
]

REPO_ROOT = Path(__file__).resolve().parent
BROWSER_DIR = REPO_ROOT / ".computer_use_browser"
STATE_PATH = BROWSER_DIR / "state.json"
SCREENSHOT_PATH = BROWSER_DIR / "screenshot.png"
DRONE_CAPTURE_DIR = REPO_ROOT / "drone_captures"
DRONE_SERVICE_HOST = os.getenv("DRONE_SERVICE_HOST", "127.0.0.1")
DRONE_SERVICE_PORT = int(os.getenv("DRONE_SERVICE_PORT", "8765"))
DRONE_SERVICE_TIMEOUT_SECONDS = float(os.getenv("DRONE_SERVICE_TIMEOUT_SECONDS", "30"))
SIMPLE_DRONE_PROGRAM = REPO_ROOT / "simple.py"
PHOTO_DRONE_PROGRAM = REPO_ROOT / "360_photo.py"
PHOTO_FILENAMES = [
    "tello_photo_0_deg.jpg",
    "tello_photo_90_deg.jpg",
    "tello_photo_180_deg.jpg",
    "tello_photo_270_deg.jpg",
]
VISION_MODEL_ID = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
EXPLORE_ACTIONS = {
    "forward",
    "back",
    "left",
    "right",
    "up",
    "down",
    "turn_left",
    "turn_right",
    "stop",
}


def parse_payload(payload: str) -> dict[str, Any]:
    """Accept either JSON payloads or plain text payloads."""
    if not payload:
        return {}

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {"text": payload}

    if isinstance(parsed, dict):
        return parsed

    return {"text": str(parsed)}


def normalize_url(value: str) -> str:
    """Return a safe http(s) URL, adding https:// when omitted."""
    if not value:
        raise ValueError("A URL is required.")

    url = value.strip()
    if "://" not in url:
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only http and https URLs are supported.")

    return url


def read_browser_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}

    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def write_browser_state(page: Any) -> None:
    BROWSER_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "last_url": page.url,
                "title": page.title(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def get_target_url(data: dict[str, Any]) -> str | None:
    explicit_url = data.get("url") or data.get("text")
    if explicit_url:
        return normalize_url(str(explicit_url))

    last_url = read_browser_state().get("last_url")
    if last_url:
        return normalize_url(str(last_url))

    return None


def get_headless_mode() -> bool:
    """Use a visible browser when DISPLAY is available unless explicitly disabled."""
    import os

    setting = os.getenv("COMPUTER_USE_HEADLESS", "").strip().lower()
    if setting in {"1", "true", "yes"}:
        return True
    if setting in {"0", "false", "no"}:
        return False

    return not bool(os.getenv("DISPLAY"))


def page_summary(page: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    text = page.locator("body").inner_text(timeout=3_000)
    result = {
        "status": "ok",
        "url": page.url,
        "title": page.title(),
        "text_preview": " ".join(text.split())[:2_000],
    }
    if extra:
        result.update(extra)

    return result


def run_browser_action(command: str, payload: str) -> dict[str, Any]:
    """Run one browser action through Playwright."""
    data = parse_payload(payload)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "status": "error",
            "message": (
                "Playwright is not installed. Run: pip install -r requirements.txt "
                "&& python -m playwright install chromium"
            ),
        }

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=get_headless_mode())
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            if command == "browser_search":
                query = str(data.get("query") or data.get("text") or "").strip()
                if not query:
                    raise ValueError("A search query is required.")
                page.goto(
                    f"https://duckduckgo.com/?q={quote_plus(query)}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                write_browser_state(page)
                return page_summary(page, {"query": query})

            target_url = get_target_url(data)
            if command != "browser_open_url" and not target_url:
                raise ValueError("No URL provided and no previous browser page exists.")

            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)

            if command == "browser_open_url":
                write_browser_state(page)
                return page_summary(page)

            if command == "browser_get_text":
                write_browser_state(page)
                return page_summary(page)

            if command == "browser_click_text":
                text = str(data.get("text") or data.get("label") or "").strip()
                if not text:
                    raise ValueError("Text or label to click is required.")
                page.get_by_text(text, exact=False).first.click(timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                write_browser_state(page)
                return page_summary(page, {"clicked_text": text})

            if command == "browser_type_text":
                selector = str(data.get("selector") or "").strip()
                text = str(data.get("text") or data.get("value") or "").strip()
                submit = bool(data.get("submit", False))
                if not selector or not text:
                    raise ValueError("Selector and text are required.")
                page.locator(selector).first.fill(text, timeout=5_000)
                if submit:
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                write_browser_state(page)
                return page_summary(page, {"typed_selector": selector})

            if command == "browser_screenshot":
                BROWSER_DIR.mkdir(exist_ok=True)
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                write_browser_state(page)
                return page_summary(page, {"screenshot": str(SCREENSHOT_PATH)})

            raise ValueError(f"Unsupported browser command: {command}")
    except PlaywrightTimeoutError as exc:
        return {
            "status": "error",
            "message": f"Browser action timed out: {exc}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }


def run_simple_drone_program(payload: str) -> dict[str, Any]:
    """Run the allowlisted simple.py drone program."""
    data = parse_payload(payload)
    timeout_seconds = float(data.get("timeout_seconds", 60))

    if not SIMPLE_DRONE_PROGRAM.exists():
        return {
            "status": "error",
            "message": f"Drone program not found: {SIMPLE_DRONE_PROGRAM}",
        }

    try:
        completed = subprocess.run(
            [sys.executable, str(SIMPLE_DRONE_PROGRAM)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "error",
            "message": f"simple.py timed out after {timeout_seconds}s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "program": str(SIMPLE_DRONE_PROGRAM),
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def create_genai_client() -> Any:
    """Create a Gemini client for summarizing drone photos."""
    from google import genai

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    location = (
        os.getenv("GOOGLE_CLOUD_LOCATION")
        or os.getenv("GOOGLE_CLOUD_REGION")
        or os.getenv("GOOGLE_CLOUD_DEFAULT_REGION")
    )
    if project and location:
        return genai.Client(vertexai=True, project=project, location=location)

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)

    raise RuntimeError(
        "Gemini vision summary needs Vertex project/location env vars or GEMINI_API_KEY."
    )


def summarize_drone_photos(image_paths: list[Path]) -> str:
    """Ask Gemini for a one-line summary of the captured 360-degree photos."""
    from google.genai import types

    client = create_genai_client()
    contents: list[Any] = [
        (
            "These are four photos captured by a drone while rotating 360 degrees. "
            "In one short spoken sentence, answer: what do you see?"
        )
    ]
    contents.extend(
        types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg")
        for image_path in image_paths
    )

    response = client.models.generate_content(
        model=VISION_MODEL_ID,
        contents=contents,
    )
    summary = (response.text or "").strip()
    if not summary:
        raise RuntimeError("Gemini returned an empty image summary.")

    return " ".join(summary.split())


def summarize_drone_snapshot(image_path: Path) -> str:
    """Ask Gemini for a short spoken summary of one live drone frame."""
    from google.genai import types

    client = create_genai_client()
    response = client.models.generate_content(
        model=VISION_MODEL_ID,
        contents=[
            (
                "This is a current camera frame from a Tello drone. "
                "In one short spoken sentence, answer: what do you see?"
            ),
            types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg"),
        ],
    )
    summary = (response.text or "").strip()
    if not summary:
        raise RuntimeError("Gemini returned an empty image summary.")

    return " ".join(summary.split())


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse JSON from plain or fenced model output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object.")

    return parsed


def analyze_exploration_step(image_path: Path, telemetry: dict[str, Any]) -> dict[str, Any]:
    """Ask Gemini for one safe next exploration action without executing it."""
    from google.genai import types

    client = create_genai_client()
    prompt = (
        "You are guiding a small indoor Tello drone using very small movements. "
        "Look at the current camera frame and suggest exactly one safe next action. "
        "Allowed actions are: forward, back, left, right, up, down, turn_left, "
        "turn_right, stop. Translational actions are one 5 cm increment. Turns are "
        "one 15 degree increment. Prefer stop if the scene is too dark, too close "
        "to obstacles, unclear, or unsafe. Return only compact JSON with keys: "
        "observation, suggested_action, reason. Telemetry: "
        f"{json.dumps(telemetry, default=str)}"
    )
    response = client.models.generate_content(
        model=VISION_MODEL_ID,
        contents=[
            prompt,
            types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg"),
        ],
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty exploration analysis.")

    parsed = parse_json_object(text)
    suggested_action = str(parsed.get("suggested_action", "stop")).strip()
    if suggested_action not in EXPLORE_ACTIONS:
        suggested_action = "stop"

    return {
        "observation": " ".join(str(parsed.get("observation", "")).split()),
        "suggested_action": suggested_action,
        "reason": " ".join(str(parsed.get("reason", "")).split()),
        "raw_response": text,
    }


def call_drone_service(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON command to the long-lived local drone service."""
    request = json.dumps({"command": command, "payload": payload}) + "\n"
    try:
        with socket.create_connection(
            (DRONE_SERVICE_HOST, DRONE_SERVICE_PORT),
            timeout=DRONE_SERVICE_TIMEOUT_SECONDS,
        ) as sock:
            sock.settimeout(DRONE_SERVICE_TIMEOUT_SECONDS)
            sock.sendall(request.encode("utf-8"))
            response = sock.makefile("r", encoding="utf-8").readline()
    except OSError as exc:
        return {
            "status": "error",
            "message": (
                f"Drone service is not reachable at "
                f"{DRONE_SERVICE_HOST}:{DRONE_SERVICE_PORT}: {exc}. "
                "Start it with: python drone_service.py"
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

    if isinstance(parsed, dict):
        return parsed

    return {"status": "error", "message": "Drone service response was not an object."}


def run_drone_service_command(command: str, payload: str) -> dict[str, Any]:
    """Proxy a worker drone_* command to drone_service.py."""
    data = parse_payload(payload)
    service_command = command.removeprefix("drone_")
    return call_drone_service(service_command, data)


def run_drone_snapshot(payload: str) -> dict[str, Any]:
    """Capture and summarize one current drone camera frame."""
    data = parse_payload(payload)
    result = call_drone_service("snapshot", data)
    if result.get("status") != "ok":
        return result

    image_path = Path(str(result.get("image", "")))
    if not image_path.exists():
        return {
            "status": "error",
            "message": f"Drone snapshot image was not found: {image_path}",
            "drone_service_result": result,
        }

    try:
        summary = summarize_drone_snapshot(image_path)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Failed to summarize drone snapshot: {exc}",
            "image": str(image_path),
            "drone_service_result": result,
        }

    return {
        **result,
        "summary": summary,
    }


def run_drone_explore(payload: str) -> dict[str, Any]:
    """Capture the current view and suggest one next safe exploration action."""
    data = parse_payload(payload)
    result = call_drone_service("snapshot", data)
    if result.get("status") != "ok":
        return result

    image_path = Path(str(result.get("image", "")))
    if not image_path.exists():
        return {
            "status": "error",
            "message": f"Drone exploration image was not found: {image_path}",
            "drone_service_result": result,
        }

    try:
        analysis = analyze_exploration_step(image_path, dict(result.get("drone") or {}))
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Failed to analyze exploration step: {exc}",
            "image": str(image_path),
            "drone_service_result": result,
        }

    return {
        **result,
        "exploration": analysis,
        "message": (
            f"{analysis['observation']} Suggested next action: "
            f"{analysis['suggested_action']}. {analysis['reason']}"
        ),
    }


def run_look_around(payload: str) -> dict[str, Any]:
    """Capture four drone photos with 360_photo.py and summarize them."""
    service_status = call_drone_service("ping", {})
    if service_status.get("status") == "ok":
        return {
            "status": "error",
            "message": (
                "The live drone service is running, so the legacy 360_photo.py "
                "flow cannot use the Tello socket. Use drone_snapshot for the "
                "real-time demo, or stop drone_service.py before running "
                "drone_look_around."
            ),
        }

    data = parse_payload(payload)
    timeout_seconds = float(data.get("timeout_seconds", 120))
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    capture_dir = DRONE_CAPTURE_DIR / session_id
    photo_paths = [capture_dir / filename for filename in PHOTO_FILENAMES]

    if not PHOTO_DRONE_PROGRAM.exists():
        return {
            "status": "error",
            "message": f"Drone photo program not found: {PHOTO_DRONE_PROGRAM}",
        }

    capture_dir.mkdir(parents=True, exist_ok=False)

    try:
        completed = subprocess.run(
            [sys.executable, str(PHOTO_DRONE_PROGRAM), "--output-dir", str(capture_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "error",
            "message": f"360_photo.py timed out after {timeout_seconds}s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    missing_images = [str(path) for path in photo_paths if not path.exists()]
    if completed.returncode != 0 and missing_images:
        return {
            "status": "error",
            "message": "360_photo.py failed before all expected photos were captured.",
            "program": str(PHOTO_DRONE_PROGRAM),
            "returncode": completed.returncode,
            "session_id": session_id,
            "capture_dir": str(capture_dir),
            "missing_images": missing_images,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

    if missing_images:
        return {
            "status": "error",
            "message": "360_photo.py completed but expected photos are missing.",
            "session_id": session_id,
            "capture_dir": str(capture_dir),
            "missing_images": missing_images,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

    try:
        summary = summarize_drone_photos(photo_paths)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Failed to summarize drone photos: {exc}",
            "session_id": session_id,
            "capture_dir": str(capture_dir),
            "images": [str(path) for path in photo_paths],
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }

    return {
        "status": "ok",
        "warning": (
            "360_photo.py exited nonzero after capturing all photos; summarized "
            "available session images."
            if completed.returncode != 0
            else ""
        ),
        "program_returncode": completed.returncode,
        "session_id": session_id,
        "capture_dir": str(capture_dir),
        "summary": summary,
        "images": [str(path) for path in photo_paths],
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


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
        files = sorted(
            path.name
            for path in REPO_ROOT.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )
        return {
            "status": "ok",
            "files": files,
        }

    if command.startswith("browser_"):
        return run_browser_action(command, payload)

    if command in {
        "drone_connect",
        "drone_status",
        "drone_takeoff",
        "drone_forward",
        "drone_back",
        "drone_left",
        "drone_right",
        "drone_up",
        "drone_down",
        "drone_turn_left",
        "drone_turn_right",
        "drone_stop",
        "drone_land",
        "drone_shutdown",
    }:
        return run_drone_service_command(command, payload)

    if command == "drone_snapshot":
        return run_drone_snapshot(payload)

    if command == "drone_explore":
        return run_drone_explore(payload)

    if command == "drone_run_simple":
        return run_simple_drone_program(payload)

    if command == "drone_look_around":
        return run_look_around(payload)

    return {
        "status": "error",
        "message": f"Unsupported command: {command}",
        "supported_commands": SUPPORTED_COMMANDS,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--payload", default="")
    args = parser.parse_args()

    print(json.dumps(handle_command(args.command, args.payload)))


if __name__ == "__main__":
    main()
