drone-monitor


Control drone via Voice and Vision guidance with Gemini / Gemma model

python3.12 -m venv venv

source venv/bin/activate

pip install -r requirements.txt

python -m playwright install chromium

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
What do you see?
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
