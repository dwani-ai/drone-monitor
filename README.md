drone-monitor


Control drone via Voice and Vision guidance with Gemini / Gemma model

python3.12 -m venv venv

source venv/bin/activate

pip install -r requirements.txt

python -m playwright install chromium

npm install

## Gemini Live native audio

For a Gemini Developer API key:

```bash
export GEMINI_API_KEY="your-api-key"
```

Run the native-audio Live API client:

```bash
python gemini_live_computer_use.py
```

The Live client is interactive: it keeps listening after tool calls and
reconnects automatically if the Live receive stream ends.
The default worker timeout is 180 seconds so drone photo commands have time to
take off, capture images, summarize them, and land.

### Real-time drone demo

For the fastest live demo, run a long-lived local drone service and the React
dashboard. The service keeps the Tello connection and camera stream open across
Gemini tool calls, so "what do you see?" can use the current camera frame
without waiting for the drone to land.

```bash
python3 drone_service.py
```

In a second terminal, start the dashboard API:

```bash
python3 web_server.py
```

In a third terminal, start the React dashboard:

```bash
npm run dev
```

Open `http://127.0.0.1:5173` to see the live drone camera stream, telemetry,
flight controls, and Gemini Live event feed.

Then start Gemini Live in another terminal:

```bash
python3 gemini_live_computer_use.py
```

Try saying:

```text
Take off.
Drone status.
What do you see?
Land.
```

The real-time demo commands are:

```bash
python3 computer_worker.py --command drone_status
python3 computer_worker.py --command drone_takeoff
python3 computer_worker.py --command drone_snapshot
python3 computer_worker.py --command drone_land
```

The React `Take off` button and the Gemini Live voice command both go through
the same long-lived `drone_service.py` path. Gemini Live uses a dedicated
`drone_control` tool with a small action list: `connect`, `status`, `takeoff`,
`snapshot`, `land`, and `shutdown`. This keeps drone control separate from the
generic browser/computer tool and avoids legacy commands during the live demo.
If the button works but voice does not, restart `gemini_live_computer_use.py` so
it picks up the latest tool instructions, then test the worker path directly
with `drone_takeoff`.

`drone_snapshot` captures one current Tello camera frame through the service,
saves it under `drone_captures/<session-id>/`, and asks Gemini Vision for a
short spoken summary. The older `drone_look_around` command still performs the
full 360-degree capture sequence and lands at the end.

The dashboard reads Gemini Live events from `.gemini_live_events.jsonl`. Override
that path for both `gemini_live_computer_use.py` and `web_server.py` with
`GEMINI_LIVE_EVENT_LOG=/path/to/events.jsonl` if needed.

If `drone_service.py` repeatedly logs `Aborting command 'command'. Did not
receive a response after 7 seconds`, the local service is running but the Tello
itself is not reachable. Check that the drone is powered on, the computer is
connected to the Tello Wi-Fi network, and no other script is already connected
to the drone. Then retry:

```bash
python3 computer_worker.py --command drone_connect
```

The service defaults to a single Tello command retry so failed preflight checks
return quickly. You can override the target or retries if needed:

```bash
TELLO_HOST=192.168.10.1 TELLO_RETRY_COUNT=1 python3 drone_service.py
```

For Vertex AI, use Google Cloud application-default credentials. The Vertex AI
Live endpoint does not accept API keys for these calls:

```bash
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update
sudo apt-get install -y google-cloud-cli
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
gcloud auth application-default login
python gemini_live_computer_use.py --vertexai
```

If you already have a service account JSON file with Vertex AI permissions, you
can use it instead of `gcloud auth application-default login`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
python gemini_live_computer_use.py --vertexai
```

In Developer API mode, the client defaults to
`gemini-2.5-flash-native-audio-latest`. In Vertex AI mode, it defaults to
`gemini-live-2.5-flash-native-audio`. It listens to your
microphone, and plays Gemini's audio response. When Gemini calls the
`run_computer_program` tool, the client invokes a separate Python program:
`computer_worker.py`.

The Developer API path defaults to `--api-version v1beta`. Vertex AI uses the
SDK default unless you pass `--api-version`. You can override the model if your
account exposes a different Live model:

```bash
python gemini_live_computer_use.py --model gemini-2.5-flash-native-audio-preview-12-2025
python gemini_live_computer_use.py --vertexai --model gemini-live-2.5-flash-native-audio
```

If Live API connection fails, list the models your credentials can use:

```bash
python gemini_live_computer_use.py --list-live-models
python gemini_live_computer_use.py --vertexai --list-live-models
```

If a model is listed but the tool-enabled connection fails, isolate native
audio access from function-calling support:

```bash
python gemini_live_computer_use.py --disable-tools
python gemini_live_computer_use.py --vertexai --disable-tools
```

Try saying:

```text
Use the computer to check status.
Use the computer to echo hello from Gemini.
Use the computer to list repo files.
Use the browser to open example.com.
Use the browser to search for Tello drone SDK docs.
Use the browser to summarize the current page.
Use the browser to take a screenshot.
Run the drone simple program.
Run simple.py on the drone.
Take off.
Drone status.
What do you see?
Land.
Look around and tell me what you see.
```

You can swap in another worker program:

```bash
python gemini_live_computer_use.py --program ./my_worker.py
```

The worker should accept `--command` and `--payload`, then print a result to
stdout. On Linux, `sounddevice` may require PortAudio, for example:

```bash
sudo apt install portaudio19-dev
```

Browser computer-use commands are allowlisted in `computer_worker.py`. They use
Playwright and accept JSON payloads:

```bash
python computer_worker.py --command browser_open_url --payload '{"url":"https://example.com"}'
python computer_worker.py --command browser_search --payload '{"query":"Tello drone SDK docs"}'
python computer_worker.py --command browser_get_text
python computer_worker.py --command browser_screenshot
```

Set `COMPUTER_USE_HEADLESS=false` to force a visible browser window when a
desktop display is available.

Drone commands are also allowlisted in `computer_worker.py`. To test the
`simple.py` handoff directly:

```bash
python computer_worker.py --command drone_run_simple --payload '{"timeout_seconds":60}'
```

To test the 360-degree visual summary handoff directly:

```bash
python computer_worker.py --command drone_look_around --payload '{"timeout_seconds":120}'
```

Each look-around run stores its photos in a fresh `drone_captures/<session-id>/`
directory, so summaries only use images from the current run.
If all four photos are captured but the final cleanup rotation fails, the worker
still summarizes the current session photos and returns a warning.
