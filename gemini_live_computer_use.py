#!/usr/bin/env python3
"""Gemini Live native-audio client with a computer-use tool callback."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
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
DEFAULT_EVENT_LOG_PATH = Path(__file__).resolve().parent / ".gemini_live_events.jsonl"
EVENT_LOG_PATH = Path(
    os.getenv("GEMINI_LIVE_EVENT_LOG", str(DEFAULT_EVENT_LOG_PATH))
).expanduser()
DUPLICATE_DRONE_COMMAND_SECONDS = float(
    os.getenv("DUPLICATE_DRONE_COMMAND_SECONDS", "8")
)
PROTECTED_DRONE_COMMANDS = {
    "drone_takeoff",
    "drone_snapshot",
    "drone_explore",
    "drone_land",
    "drone_forward",
    "drone_back",
    "drone_left",
    "drone_right",
    "drone_up",
    "drone_down",
    "drone_turn_left",
    "drone_turn_right",
    "drone_stop",
    "drone_shutdown",
}


def append_live_event(event: dict[str, Any]) -> None:
    """Append one dashboard-readable Gemini Live event."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    try:
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG_PATH.open("a", encoding="utf-8") as event_file:
            event_file.write(json.dumps(payload, default=str) + "\n")
    except OSError as exc:
        print(f"Could not write Gemini Live event log: {exc}", file=sys.stderr)


def is_normal_connection_close(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "ConnectionClosedOK" or "1000 None" in str(exc)


def duplicate_drone_result(
    command: str,
    cached_result: dict[str, Any],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Return a non-executing result for an accidental repeated drone action."""
    return {
        "status": "ok",
        "duplicate_suppressed": True,
        "command": command,
        "message": (
            f"Duplicate {command} ignored because it was requested "
            f"{elapsed_seconds:.1f}s ago. No drone command was sent."
        ),
        "spoken_message": "Already handled. I did not send another drone command.",
        "previous_result": cached_result,
    }


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
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call the separate Python program used for computer actions."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

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
            env=env,
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
    stderr = completed.stderr.strip()
    parsed_stdout: Any | None = None
    if stdout:
        try:
            parsed_stdout = json.loads(stdout)
        except json.JSONDecodeError:
            parsed_stdout = None

    if isinstance(parsed_stdout, dict):
        return {
            **parsed_stdout,
            "worker_returncode": completed.returncode,
            "worker_stderr": stderr,
        }

    response: dict[str, Any] = {
        "status": "ok" if completed.returncode == 0 else "error",
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }

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


def build_worker_env(args: argparse.Namespace) -> dict[str, str]:
    worker_env: dict[str, str] = {}
    if args.vertexai:
        project = get_vertex_project(args)
        location = get_vertex_location(args)
        if project:
            worker_env["GOOGLE_CLOUD_PROJECT"] = project
        if location:
            worker_env["GOOGLE_CLOUD_LOCATION"] = location

    return worker_env


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
    """Build the Gemini Live config with explicit function declarations."""
    run_program_declaration = {
        "name": "run_computer_program",
        "description": (
            "Run an allowlisted command in a separate local Python program. "
            "Use this only for computer actions, browser actions, or local checks. "
            "Browser payloads should be JSON strings. Do not use this for drone "
            "flight control."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "status",
                        "echo",
                        "list_repo_files",
                        "browser_open_url",
                        "browser_search",
                        "browser_get_text",
                        "browser_click_text",
                        "browser_type_text",
                        "browser_screenshot",
                    ],
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
    drone_control_declaration = {
        "name": "drone_control",
        "description": (
            "Control the live Tello drone service using a small safe action set. "
            "Use this for all drone flight, telemetry, and current-view requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "connect",
                        "status",
                        "takeoff",
                        "snapshot",
                        "explore",
                        "forward",
                        "back",
                        "left",
                        "right",
                        "up",
                        "down",
                        "turn_left",
                        "turn_right",
                        "stop",
                        "land",
                        "shutdown",
                    ],
                    "description": (
                        "Drone action. connect checks the service/drone connection; "
                        "status reads telemetry; takeoff starts flight; snapshot "
                        "captures and summarizes the current camera view; explore "
                        "captures the current view and suggests one safe next action "
                        "without moving; forward, back, left, right, up, and down move one 5 cm increment; "
                        "turn_left and turn_right rotate one small 15 degree increment; "
                        "stop sends zero RC velocity; land lands the drone; shutdown "
                        "closes the drone service connection."
                    ),
                },
                "settle_seconds": {
                    "type": "number",
                    "description": (
                        "Optional snapshot delay before reading the camera frame. "
                        "Only relevant for action=snapshot; default is 0.2."
                    ),
                },
                "confirmed_land": {
                    "type": "boolean",
                    "description": (
                        "Set true only after the user explicitly confirms landing. "
                        "Never set true for the first land request."
                    ),
                },
            },
            "required": ["action"],
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
            "For every drone request, use the drone_control tool. Never use "
            "run_computer_program for drone control. If the user says take off, "
            "start, lift off, or fly, call drone_control with action=takeoff "
            "exactly once, then report the result. Do not also call snapshot in "
            "the same turn. If the user asks for battery, height, telemetry, or "
            "drone status, call drone_control with action=status. For movement "
            "requests, call exactly one movement action per turn. Use forward, "
            "back, left, right, up, or down for one 5 cm increment. Use turn_left "
            "or turn_right for one 15 degree increment. If the user says 'a little', "
            "'slightly', or gives no distance, still use exactly one 5 cm increment. "
            "Do not multiply movements or loop. If the user asks 'what do you see?', "
            "'what can you see?', 'look', 'look around', or asks for the current "
            "view, call drone_control with action=snapshot "
            "and speak the returned one-line summary directly. If the user asks "
            "to explore, guide me, inspect the room, or asks what to do next, call "
            "drone_control with action=explore. The explore action only suggests "
            "one next action; do not execute it automatically. Tell the user the "
            "observation and suggested action, then ask for confirmation. If the "
            "user confirms, call exactly that one movement action. If the user asks "
            "to land, do not land immediately. Ask 'Confirm landing?' first. Only "
            "after the user explicitly says yes or confirms landing, call "
            "drone_control with action=land and confirmed_land=true exactly once. "
            "Do not run legacy 360-degree scan programs during the live demo; "
            "explain that live snapshot is available instead. "
            "For drone tool results, prefer the spoken_message field exactly. "
            "If status is error, say one short sentence with the message and recovery. "
            "Do not read raw detail, stderr, tracebacks, file paths, JSON, or error codes aloud. "
            "Do not retry a failed drone action automatically; wait for the user. "
            "Keep spoken responses short and confirm tool results clearly once. "
            "Do not repeat the same confirmation, observation, or instruction. "
            "If a tool result has duplicate_suppressed=true, do not repeat the "
            "previous message; say briefly that the command was already handled. "
            "After each tool result, continue listening for the user's next request."
        ),
    }

    if enable_tools:
        config["tools"] = [
            {"function_declarations": [run_program_declaration, drone_control_declaration]}
        ]

    return config


async def send_microphone_audio(
    session: Any,
    audio_queue: asyncio.Queue[bytes],
    stop_event: asyncio.Event,
) -> None:
    """Forward microphone PCM chunks to Gemini Live."""
    while not stop_event.is_set():
        chunk = await audio_queue.get()
        try:
            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                )
            )
        except Exception as exc:
            if stop_event.is_set() or is_normal_connection_close(exc):
                return
            raise


async def play_model_audio(
    speaker_queue: asyncio.Queue[bytes],
    sd_module: Any,
    stop_event: asyncio.Event,
) -> None:
    """Play Gemini Live PCM audio responses."""
    with sd_module.RawOutputStream(
        samplerate=OUTPUT_SAMPLE_RATE,
        channels=CHANNELS,
        dtype=SAMPLE_DTYPE,
    ) as stream:
        while not stop_event.is_set():
            chunk = await speaker_queue.get()
            try:
                stream.write(chunk)
            except KeyboardInterrupt:
                stop_event.set()
                return


async def receive_live_messages(
    session: Any,
    speaker_queue: asyncio.Queue[bytes],
    program_path: Path,
    timeout_seconds: float,
    worker_env: dict[str, str],
    stop_event: asyncio.Event,
) -> None:
    """Handle audio, text, and tool-call messages from Gemini Live."""
    recent_drone_commands: dict[str, tuple[float, dict[str, Any]]] = {}

    while not stop_event.is_set():
        saw_message = False

        try:
            receive_stream = session.receive()
            async for message in receive_stream:
                saw_message = True
                if stop_event.is_set():
                    break

                if getattr(message, "data", None):
                    await speaker_queue.put(message.data)

                if getattr(message, "text", None):
                    print(message.text, end="", flush=True)
                    append_live_event({"type": "gemini_text", "text": message.text})

                tool_call = getattr(message, "tool_call", None)
                if not tool_call:
                    continue

                function_responses = []
                for function_call in tool_call.function_calls:
                    args = dict(function_call.args or {})
                    tool_name = function_call.name
                    command = str(args.get("command", "status"))
                    payload = str(args.get("payload", ""))
                    if tool_name == "drone_control":
                        action = str(args.get("action", "status"))
                        command = f"drone_{action}"
                        payload_data: dict[str, Any] = {"source": "gemini_live"}
                        if action == "snapshot" and "settle_seconds" in args:
                            payload_data["settle_seconds"] = args["settle_seconds"]
                        if action == "land" and args.get("confirmed_land") is True:
                            payload_data["confirmed_land"] = True
                        payload = json.dumps(payload_data)

                    print(f"\nTool call: {function_call.name}({args})")
                    append_live_event(
                        {
                            "type": "tool_call",
                            "name": tool_name,
                            "command": command,
                            "args": args,
                        }
                    )

                    if tool_name == "run_computer_program" and command.startswith("drone_"):
                        result = {
                            "status": "error",
                            "message": (
                                "Drone commands must use the drone_control tool, not "
                                "run_computer_program."
                            ),
                        }
                    elif tool_name not in {"run_computer_program", "drone_control"}:
                        result = {
                            "status": "error",
                            "message": f"Unknown tool: {tool_name}",
                        }
                    elif command in PROTECTED_DRONE_COMMANDS:
                        now = time.monotonic()
                        previous = recent_drone_commands.get(command)
                        if previous and now - previous[0] < DUPLICATE_DRONE_COMMAND_SECONDS:
                            result = duplicate_drone_result(
                                command=command,
                                cached_result=previous[1],
                                elapsed_seconds=now - previous[0],
                            )
                            recent_drone_commands[command] = (now, previous[1])
                        else:
                            result = run_computer_program(
                                command=command,
                                payload=payload,
                                program_path=program_path,
                                timeout_seconds=timeout_seconds,
                                extra_env=worker_env,
                            )
                            recent_drone_commands[command] = (now, result)
                    else:
                        result = run_computer_program(
                            command=command,
                            payload=payload,
                            program_path=program_path,
                            timeout_seconds=timeout_seconds,
                            extra_env=worker_env,
                        )

                    print(f"Tool result: {json.dumps(result)}")
                    append_live_event(
                        {
                            "type": "tool_result",
                            "name": tool_name,
                            "command": command,
                            "result": result,
                        }
                    )
                    function_responses.append(
                        {
                            "name": tool_name,
                            "id": function_call.id,
                            "response": {"result": result},
                        }
                    )

                if function_responses:
                    await session.send_tool_response(function_responses=function_responses)
        except Exception as exc:
            if stop_event.is_set() or is_normal_connection_close(exc):
                return
            raise

        if saw_message:
            print("\nGemini Live turn ended. Continuing to listen...", file=sys.stderr)
            append_live_event(
                {"type": "session", "message": "Gemini Live turn ended."}
            )
        else:
            await asyncio.sleep(0.1)


async def run_live_client(args: argparse.Namespace) -> None:
    sd = load_sounddevice()

    program_path = Path(args.program).expanduser().resolve()
    if not program_path.exists():
        raise FileNotFoundError(f"Computer program not found: {program_path}")

    client = create_client(args)
    worker_env = build_worker_env(args)
    audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
    speaker_queue: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    reconnect_delay = 2.0

    def microphone_callback(indata: bytes, frames: int, time: Any, status: Any) -> None:
        if status:
            print(f"Microphone status: {status}", file=sys.stderr)
        loop.call_soon_threadsafe(audio_queue.put_nowait, bytes(indata))

    print("Connecting to Gemini Live. Speak into your microphone.")
    print("Try: 'Take off', 'what do you see?', 'drone status', or 'land'.")
    print(f"Dashboard event log: {EVENT_LOG_PATH}")
    print("Press Ctrl+C to stop.")
    append_live_event({"type": "session", "message": "Connecting to Gemini Live."})

    with sd.RawInputStream(
        samplerate=INPUT_SAMPLE_RATE,
        channels=CHANNELS,
        dtype=SAMPLE_DTYPE,
        blocksize=CHUNK_SIZE,
        callback=microphone_callback,
    ):
        while True:
            stop_event = asyncio.Event()
            while not audio_queue.empty():
                audio_queue.get_nowait()
            while not speaker_queue.empty():
                speaker_queue.get_nowait()

            try:
                async with client.aio.live.connect(
                    model=resolve_model(args),
                    config=build_live_config(enable_tools=not args.disable_tools),
                ) as session:
                    print("Connected. Listening for requests...")
                    append_live_event(
                        {
                            "type": "session",
                            "message": "Connected. Listening for requests.",
                            "model": resolve_model(args),
                        }
                    )

                    tasks = [
                        asyncio.create_task(
                            send_microphone_audio(session, audio_queue, stop_event)
                        ),
                        asyncio.create_task(play_model_audio(speaker_queue, sd, stop_event)),
                        asyncio.create_task(
                            receive_live_messages(
                                session=session,
                                speaker_queue=speaker_queue,
                                program_path=program_path,
                                timeout_seconds=args.timeout,
                                worker_env=worker_env,
                                stop_event=stop_event,
                            )
                        ),
                    ]

                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    stop_event.set()

                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    for task in done:
                        try:
                            task.result()
                        except Exception as exc:
                            if is_normal_connection_close(exc):
                                continue
                            raise
            except asyncio.CancelledError:
                raise
            except errors.APIError:
                raise
            except Exception as exc:
                print(f"\nLive session ended: {exc}", file=sys.stderr)
                append_live_event(
                    {"type": "session", "message": f"Live session ended: {exc}"}
                )

            print(f"Reconnecting in {reconnect_delay:.0f}s...", file=sys.stderr)
            await asyncio.sleep(reconnect_delay)


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
        default=180.0,
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
