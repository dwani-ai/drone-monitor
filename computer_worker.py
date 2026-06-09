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
]

REPO_ROOT = Path(__file__).resolve().parent
BROWSER_DIR = REPO_ROOT / ".computer_use_browser"
STATE_PATH = BROWSER_DIR / "state.json"
SCREENSHOT_PATH = BROWSER_DIR / "screenshot.png"


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
