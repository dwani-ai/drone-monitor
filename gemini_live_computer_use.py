#!/usr/bin/env python3
"""Gemini Live native-audio client with a computer-use tool callback."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors
from google.genai import types


DEVELOPER_MODEL_ID = "gemini-2.5-flash-native-audio-latest"
VERTEX_MODEL_ID = "gemini-live-2.5-flash-native-audio"
DEVELOPER_API_VERSION = "v1beta"
INPUT_SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_DTYPE = "int16"
CHUNK_SIZE = 1024


def load_sounddevice() -> Any:
    """Import sounddevice and explain the native PortAudio dependency if missing."""
    try:
        import sounddevice as sd
    except OSError as exc:
        raise RuntimeError(
            "PortAudio library not found. Install it with:\n"
            "  sudo apt install portaudio19-dev\n"
            "Then reinstall Python dependencies if needed:\n"
            "  pip install -r requirements.txt"
        ) from exc

    return sd


def run_computer_program(
    command: str,
    payload: str,
    program_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Call the separate Python program used for computer actions."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(program_path),
                "--command",
                command,
                "--payload",
                payload,
            ],
            cwd=program_path.parent,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "error",
            "message": f"Computer program timed out after {timeout_seconds}s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    stdout = completed.stdout.strip()
    response: dict[str, Any] = {
        "status": "ok" if completed.returncode == 0 else "error",
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": completed.stderr.strip(),
    }

    if stdout:
        try:
            response["json"] = json.loads(stdout)
        except json.JSONDecodeError:
            pass

    return response


def get_api_key(args: argparse.Namespace, required: bool = True) -> str | None:
    api_key = args.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if required and not api_key:
        raise RuntimeError("Set GEMINI_API_KEY or pass --api-key.")

    return api_key


def get_vertex_project(args: argparse.Namespace) -> str | None:
    return (
        args.project
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    )


def get_vertex_location(args: argparse.Namespace) -> str | None:
    return (
        args.location
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or os.getenv("GOOGLE_CLOUD_REGION")
        or os.getenv("GOOGLE_CLOUD_DEFAULT_REGION")
    )


def create_client(args: argparse.Namespace) -> genai.Client:
    api_version = args.api_version
    if api_version is None and not args.vertexai:
        api_version = DEVELOPER_API_VERSION

    http_options = {"api_version": api_version} if api_version else None

    if args.vertexai:
        project = get_vertex_project(args)
        location = get_vertex_location(args)
        if not project or not location:
            raise RuntimeError(
                "Vertex mode needs --project and --location, or the "
                "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION environment "
                "variables. Authenticate with:\n"
                "  gcloud auth application-default login"
            )

        kwargs: dict[str, Any] = {
            "vertexai": True,
            "project": project,
            "location": location,
        }

        if http_options:
            kwargs["http_options"] = http_options

        return genai.Client(**kwargs)

    return genai.Client(
        api_key=get_api_key(args),
        http_options=http_options,
    )


def resolve_model(args: argparse.Namespace) -> str:
    if args.model:
        return args.model

    if args.vertexai:
        return VERTEX_MODEL_ID

    return DEVELOPER_MODEL_ID


def get_supported_methods(model: Any) -> list[str]:
    methods = (
        getattr(model, "supported_generation_methods", None)
        or getattr(model, "supported_actions", None)
        or []
    )
    return list(methods)


def list_live_models(args: argparse.Namespace) -> None:
    client = create_client(args)

    endpoint = "Vertex AI" if args.vertexai else "Gemini Developer API"
    version = args.api_version or ("default" if args.vertexai else DEVELOPER_API_VERSION)
    print(f"Live-capable models for {endpoint} API version {version}:")
    found = False
    for model in client.models.list():
        methods = get_supported_methods(model)
        if "bidiGenerateContent" not in methods:
            continue

        found = True
        print(f"- {model.name}")

    if not found:
        print("No models with bidiGenerateContent were returned for this key/version.")


def build_live_config(enable_tools: bool) -> dict[str, Any]:
    """Build the Gemini Live config with an explicit function declaration."""
    run_program_declaration = {
        "name": "run_computer_program",
        "description": (
            "Run an allowlisted command in a separate local Python program. "
            "Use this for computer actions, browser actions, drone helper actions, "
            "or local checks. Browser payloads should be JSON strings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Worker command to run. Supported commands are status, echo, "
                        "list_repo_files, browser_open_url, browser_search, "
                        "browser_get_text, browser_click_text, browser_type_text, "
                        "and browser_screenshot."
                    ),
                },
                "payload": {
                    "type": "string",
                    "description": (
                        "Optional text or JSON payload. Browser examples: "
                        "{\"url\":\"https://example.com\"}, "
                        "{\"query\":\"drone safety checklist\"}, "
                        "{\"text\":\"More details\"}, or "
                        "{\"selector\":\"input[name=q]\",\"text\":\"tello drone\","
                        "\"submit\":true}."
                    ),
                },
            },
            "required": ["command"],
        },
    }

    config: dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        "system_instruction": (
            "You are a voice assistant for a drone-monitor project. "
            "When the user asks you to use the computer or run a local helper, "
            "call run_computer_program instead of claiming you did it. "
            "For browser requests, use the browser_* worker commands with JSON "
            "payloads. Do not request arbitrary shell commands. "
            "Keep spoken responses short and confirm tool results clearly."
        ),
    }

    if enable_tools:
        config["tools"] = [{"function_declarations": [run_program_declaration]}]

    return config


async def send_microphone_audio(session: Any, audio_queue: asyncio.Queue[bytes]) -> None:
    """Forward microphone PCM chunks to Gemini Live."""
    while True:
        chunk = await audio_queue.get()
        await session.send_realtime_input(
            audio=types.Blob(
                data=chunk,
                mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
            )
        )


async def play_model_audio(
    speaker_queue: asyncio.Queue[bytes],
    sd_module: Any,
) -> None:
    """Play Gemini Live PCM audio responses."""
    with sd_module.RawOutputStream(
        samplerate=OUTPUT_SAMPLE_RATE,
        channels=CHANNELS,
        dtype=SAMPLE_DTYPE,
    ) as stream:
        while True:
            chunk = await speaker_queue.get()
            stream.write(chunk)


async def receive_live_messages(
    session: Any,
    speaker_queue: asyncio.Queue[bytes],
    program_path: Path,
    timeout_seconds: float,
) -> None:
    """Handle audio, text, and tool-call messages from Gemini Live."""
    async for message in session.receive():
        if getattr(message, "data", None):
            await speaker_queue.put(message.data)

        if getattr(message, "text", None):
            print(message.text, end="", flush=True)

        tool_call = getattr(message, "tool_call", None)
        if not tool_call:
            continue

        function_responses = []
        for function_call in tool_call.function_calls:
            args = dict(function_call.args or {})
            command = str(args.get("command", "status"))
            payload = str(args.get("payload", ""))
            print(f"\nTool call: {function_call.name}({args})")

            if function_call.name != "run_computer_program":
                result = {
                    "status": "error",
                    "message": f"Unknown tool: {function_call.name}",
                }
            else:
                result = run_computer_program(
                    command=command,
                    payload=payload,
                    program_path=program_path,
                    timeout_seconds=timeout_seconds,
                )

            print(f"Tool result: {json.dumps(result)}")
            function_responses.append(
                {
                    "name": function_call.name,
                    "id": function_call.id,
                    "response": {"result": result},
                }
            )

        if function_responses:
            await session.send_tool_response(function_responses=function_responses)


async def run_live_client(args: argparse.Namespace) -> None:
    sd = load_sounddevice()

    program_path = Path(args.program).expanduser().resolve()
    if not program_path.exists():
        raise FileNotFoundError(f"Computer program not found: {program_path}")

    client = create_client(args)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
    speaker_queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def microphone_callback(indata: bytes, frames: int, time: Any, status: Any) -> None:
        if status:
            print(f"Microphone status: {status}", file=sys.stderr)
        loop.call_soon_threadsafe(audio_queue.put_nowait, bytes(indata))

    print("Connecting to Gemini Live. Speak into your microphone.")
    print("Try: 'Use the computer to check status' or 'Use the computer to echo hello'.")
    print("Press Ctrl+C to stop.")

    async with client.aio.live.connect(
        model=resolve_model(args),
        config=build_live_config(enable_tools=not args.disable_tools),
    ) as session:
        with sd.RawInputStream(
            samplerate=INPUT_SAMPLE_RATE,
            channels=CHANNELS,
            dtype=SAMPLE_DTYPE,
            blocksize=CHUNK_SIZE,
            callback=microphone_callback,
        ):
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(send_microphone_audio(session, audio_queue))
                task_group.create_task(play_model_audio(speaker_queue, sd))
                task_group.create_task(
                    receive_live_messages(
                        session=session,
                        speaker_queue=speaker_queue,
                        program_path=program_path,
                        timeout_seconds=args.timeout,
                    )
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gemini Live native-audio assistant with a local computer-use tool."
    )
    parser.add_argument("--api-key", help="API key. Defaults to GEMINI_API_KEY.")
    parser.add_argument("--model", help="Model ID. Defaults depend on API mode.")
    parser.add_argument(
        "--vertexai",
        action="store_true",
        help="Use Vertex AI instead of the Gemini Developer API endpoint.",
    )
    parser.add_argument(
        "--project",
        help="Google Cloud project for Vertex AI. Defaults to GOOGLE_CLOUD_PROJECT.",
    )
    parser.add_argument(
        "--location",
        help="Google Cloud location for Vertex AI. Defaults to GOOGLE_CLOUD_LOCATION.",
    )
    parser.add_argument(
        "--list-live-models",
        action="store_true",
        help="List models that report bidiGenerateContent support, then exit.",
    )
    parser.add_argument(
        "--disable-tools",
        action="store_true",
        help="Connect without function tools to isolate model/audio access issues.",
    )
    parser.add_argument(
        "--api-version",
        help="API version for Live API. Developer API defaults to v1beta; Vertex uses SDK default.",
    )
    parser.add_argument(
        "--program",
        default=str(Path(__file__).resolve().parent / "computer_worker.py"),
        help="Separate Python program to call for computer-use tool requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for the external computer program.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.list_live_models:
            list_live_models(args)
            return

        asyncio.run(run_live_client(args))
    except errors.APIError as exc:
        print(f"\nGemini Live API error: {exc}", file=sys.stderr)
        if not args.vertexai and "API_KEY_SERVICE_BLOCKED" in str(exc):
            print(
                "This key is blocked from the Gemini Developer API endpoint. "
                "If it is a Vertex AI key, rerun with:\n"
                "  python gemini_live_computer_use.py --vertexai --list-live-models\n"
                "  python gemini_live_computer_use.py --vertexai",
                file=sys.stderr,
            )
        elif args.vertexai and "API keys are not supported by this API" in str(exc):
            print(
                "Vertex AI Live requires OAuth/application-default credentials, "
                "not an API key. Run:\n"
                "  gcloud auth application-default login\n"
                "  export GOOGLE_CLOUD_PROJECT=\"your-project-id\"\n"
                "  export GOOGLE_CLOUD_LOCATION=\"us-central1\"\n"
                "  python gemini_live_computer_use.py --vertexai --list-live-models",
                file=sys.stderr,
            )
        else:
            print(
                "Check which Live models your key can use:\n"
                "  python gemini_live_computer_use.py --list-live-models\n"
                "For Vertex AI, include --vertexai and your project/location or API key.\n"
                "If the model is listed, isolate tool support with:\n"
                "  python gemini_live_computer_use.py --disable-tools",
                file=sys.stderr,
            )
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
