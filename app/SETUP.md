# IMP-VISION app v0.10.3 — Termux testing rig

Phone-only mode of the IMP-VISION app design: the model picker with
live compatibility checks and downloads, the RAM-checked sets, and
Ask backed by your local llama.cpp server (text + vision). This is
the training/testing rig — it runs the real models on your real
phone before any glasses exist.

## Setup (Termux)

```sh
pkg install python
python -m pip install flask requests pillow
```

(Pillow is new in v0.10: the Senses pipeline decodes feed frames
for IR Watch and renders the simulated glasses' frames. Without
it, Models/Ask still work and Senses reports itself unavailable.)

Unzip the package, then from inside the `imp-vision-app` folder:

```sh
python app.py
```

Open http://127.0.0.1:5000 in the phone browser.

## Ask needs llama-server running

The app talks to llama.cpp's server on port 8080. Start it from your
llama.cpp build with a downloaded model, e.g. after installing the
Balanced set:

```sh
cd ~/imp-vision-models/brain/Qwen__Qwen2.5-1.5B-Instruct-GGUF
~/llama.cpp/build/bin/llama-server -m *.gguf --port 8080
```

For vision questions, run the Eyes model instead with its projector:

```sh
cd ~/imp-vision-models/eyes/ggml-org__SmolVLM-500M-Instruct-GGUF
~/llama.cpp/build/bin/llama-server -m <model>.gguf --mmproj <mmproj>.gguf --port 8080
```

(Exact filenames are whatever the download saved; `ls` the folder.)
One server serves one model at a time in v0 — swap the server when
you swap what you're testing. That is the testing workflow.

## Home-screen app (imp-vision.apk)

The APK is a thin shell: tapping the IMP-VISION icon tells Termux to
run `start.sh` (starts this server if down, and llama-server with
the best complete Brain model if down — the Balanced set's Brain
first, falling through to the next candidate when one fails to
load), then opens the page inside the app, retrying while the
server comes up.

One-time Termux setting so the app is allowed to start things:

```sh
mkdir -p ~/.termux
echo "allow-external-apps=true" >> ~/.termux/termux.properties
```

Then fully close Termux (swipe it away) and reopen it once. Install
the APK by tapping it in your file manager (allow install from that
app when Android asks). After that: tap the icon, wait a few
seconds, the app is up. If the server ever refuses to start, the app
shows the manual command instead.

## What works in v0.1

- Home: llama-server status, what's downloaded.
- Models: browse/search the scanned catalog (1,242 repos), paste any
  HF repo and run the real checks (format, mmproj for Eyes, size,
  license), download with progress, one-tap Light/Balanced/Max sets.
- Downloads land in ~/imp-vision-models/<role>/<repo>/. Every
  finished download is verified against the size HF lists for the
  file: a short file is marked PARTIAL (Android killing Termux
  mid-download is how v0 got corrupt models), is never served, and
  a re-download resumes/replaces it. Keep Termux in front with the
  screen on during big downloads anyway.
- Ask: text questions, and photo questions when the vision server is
  up (attach from the camera). "Speak the answer" uses the browser's
  built-in voice.
- Session log: every Ask is appended to
  ~/imp-vision-models/sessions.jsonl (time, served model, prompt,
  answer, photo y/n). Home has an Export button (GET /api/export).
- Blind A/B (Ask screen): tick A/B mode, ask on one Brain, swap the
  served Brain, ask the same question again — the pair returns as
  A/B in random order, and Prefer A/B writes the preference with the
  sealed model identities into the session log for the eval harness.

## What's new in v0.10 — Senses + the glasses link layer

The Senses screen is live, and with it the phone side of the temple
firmware protocol (designs/temple-f/FIRMWARE.md), in two new files:

- `link.py` — the framed-TCP feed codec (16-byte header:
  type/seq/timestamp/length + payload; FEED/PHOTO/THUMB are JPEG,
  THERM is radiometric uint16), a real TCP feed client for a future
  glasses soft-AP session, and the BLE-side session model:
  lifecycle IDLE → REQUESTED → AP_UP → STREAMING → TEARDOWN,
  per-session credentials generated on request and held in RAM only
  (wiped at teardown), the 20% battery gate, the 15 s idle
  teardown.
- `irwatch.py` — IR Source Watch v0: blown-out cluster detection +
  geometry classification (ring-around-centre = camera-type, lone
  filled panel = illuminator, else single-point), thermal
  cross-check against the co-timed 40×30 preview, emitter-first
  labels with confidence — never a bare "camera found".

Feed sources, honestly labelled in the UI:

- **Simulated glasses** (synthetic, in-process): a FULL-build
  variant-R glasses that emits real protocol frames — a dark-room
  FEED scene holding a ring-array emitter, a panel illuminator and
  a blinking remote, plus 40×30 radiometric THERM previews and
  telemetry. It exists so the whole pipeline runs today.
- **Phone camera** (real): getUserMedia in the page; frames post
  to the server and run through the identical pipeline. Phone-only
  mode is a first-class citizen, per the app design.
- **Glasses over Wi-Fi** (not yet): the TCP client is implemented;
  there is no hardware to connect it to.

IR Watch on the phone camera is best-effort, and the UI says why:
phone lenses filter near-IR and every model filters differently —
an 850 nm array may show, a 940 nm array may not. The glasses'
no-IR-cut sensor is what makes the feature full-sensitivity. The
other honest limit is in the UI too: a camera with its IR off
emits nothing and is invisible — the watch finds emitters, not
cameras.

Thermal surface: the 40×30 radiometric preview (the FW-THM-01
BLE-rate format) renders false-colour with min/max. Full 160×120
radiometric frames ride Wi-Fi feed sessions only; the preview
surface states that instead of pretending to be the full frame.

Detections (at first sighting, with bearing from the phone compass
+ position in frame) and photos from either source are appended to
sessions.jsonl as `ir_detection` / `photo` records and come out
with the usual Export.

## Honest stubs

- Ears (whisper.cpp) and Voice (Piper) files download fine, but the
  in-app speech wiring lands in v1 — for now the browser's speech
  covers voice-out, and voice-in is typing or the browser mic.
- No glasses hardware pairing yet — the link layer is modelled
  end-to-end against the simulated glasses, and the phone's
  camera/mic stand in as a real source, per the app design. HUD
  and Pair IMP arrive with their hardware.
