"""IMP-VISION app v0.10.3 — Termux testing rig (phone-only mode).

Implements the app design's first slice: Home status, the Models
picker (catalog browse + live repo check + downloads + RAM-checked
sets), and Ask backed by a local llama-server (text + vision).
Ears/Voice wire in when whisper.cpp / Piper are present; until then
Ask is text+image and answers can be spoken by the browser.

v0.1 adds: download completeness verification against the sizes HF
lists for each repo (truncated downloads are marked PARTIAL and are
never served), a JSONL session log of every Ask with an export
endpoint, and blind A/B preference capture for the eval harness.

v0.10 adds the Senses screen and the glasses link layer (link.py):
the phone side of the temple firmware protocol — framed-TCP feed
codec, the BLE-side session model (per-session credentials, battery
gate, idle teardown) — running end-to-end against a simulated
glasses source, with the phone's own camera as a first-class feed
source through the same pipeline. IR Source Watch v0 (irwatch.py)
detects and classifies IR emitter clusters on either source;
detections and photos join the session log. The 40x30 radiometric
thermal preview (FW-THM-01 format) renders as a false-colour view;
full 160x120 thermal frames stay Wi-Fi-session-only, and the UI
says so. No glasses hardware exists yet — what is simulated vs
real is labelled in the UI and in SETUP.md.

v0.10.3: Senses auto-opens the phone camera feed on entry
(session and camera start together; leaving the screen stops
both). v0.10.1: start.sh launches llama-server with -c 1024
-t 4 — load-bearing limits on the A53 (defaults die mid-load).

Run:  python app.py            (serves http://127.0.0.1:5000)
Env:  IMP_VISION_LLAMA  llama-server base URL (default 127.0.0.1:8080)
      IMP_VISION_MODELS model download dir (default ~/imp-vision-models)
"""
import base64, io, json, os, random, struct, threading, time, urllib.parse

import requests
from flask import Flask, Response, jsonify, render_template_string, request

import irwatch
import link as linkmod

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.environ.get("IMP_VISION_MODELS",
                            os.path.expanduser("~/imp-vision-models"))
LLAMA = os.environ.get("IMP_VISION_LLAMA", "http://127.0.0.1:8080")
HF = "https://huggingface.co"
UA = {"User-Agent": "imp-vision-app"}

with open(os.path.join(BASE, "catalog.json")) as f:
    CATALOG = json.load(f)
with open(os.path.join(BASE, "sets.json")) as f:
    SETS = json.load(f)

app = Flask(__name__)


@app.after_request
def _no_store(resp):
    # The UI must never come from a WebView/browser cache — a stale page
    # looks exactly like "the update didn't take".
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp
jobs = {}          # repo -> {pct, done, error, role}
jobs_lock = threading.Lock()

# ---------------- expected sizes / completeness ----------------
# meta.json in the models dir records, per repo, the byte size HF
# lists for each downloaded file. A file shorter than its listed
# size is a truncated download (Android killed Termux mid-transfer)
# and is marked PARTIAL everywhere: listings, health, the launcher.

META_FILE = os.path.join(MODELS_DIR, "meta.json")
JOBS_FILE = os.path.join(MODELS_DIR, "jobs.json")

def persist_job(repo, role, files):
    """Remember an in-flight download on disk so a killed server can
    resume it on the next start. files=None clears the record (the job
    reached a terminal state: done, partial, or error)."""
    try:
        os.makedirs(MODELS_DIR, exist_ok=True)
        data = {}
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE) as f:
                data = json.load(f)
        if files is None:
            data.pop(repo, None)
        else:
            data[repo] = {"role": role, "files": files}
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, JOBS_FILE)
    except Exception:
        pass

def resume_jobs():
    """Relaunch every download that was mid-flight when the server
    died. run_download resumes each file from its .part via Range."""
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
    except Exception:
        return
    for repo, j in data.items():
        role, files = j.get("role", "brain"), j.get("files") or []
        if not files:
            continue
        with jobs_lock:
            if repo in jobs:
                continue
            jobs[repo] = {"pct": 0, "done": False, "error": None,
                          "role": role, "got_mb": 0, "resumed": True}
        threading.Thread(target=run_download, args=(repo, role, files),
                         daemon=True).start()
meta_lock = threading.Lock()

def load_meta():
    try:
        with open(META_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

META = load_meta()

def save_meta():
    try:
        os.makedirs(MODELS_DIR, exist_ok=True)
        tmp = META_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(META, f)
        os.replace(tmp, META_FILE)
    except Exception:
        pass

def record_expected(repo, role, files):
    """files: {path: size} from the HF tree — remember the picked
    files' expected sizes so finished downloads can be verified."""
    files = {os.path.basename(p): int(s) for p, s in files.items() if s}
    if not files:
        return
    with meta_lock:
        META[repo] = {"role": role, "files": files, "checked": time.time()}
        save_meta()

def file_status(actual, expected):
    """One file: complete / partial / unverified (no size on record)."""
    if expected is None:
        return "unverified"
    return "complete" if actual >= expected else "partial"

# ---------------- session log ----------------

SESSIONS = os.path.join(MODELS_DIR, "sessions.jsonl")
sess_lock = threading.Lock()

def log_session(rec):
    rec = dict(rec)
    rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        with sess_lock:
            os.makedirs(MODELS_DIR, exist_ok=True)
            with open(SESSIONS, "a") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

# ---------------- Hugging Face ----------------

def repo_files(repo):
    """Return (files {path: size}, license) using the tree API (has sizes)."""
    r = requests.get(f"{HF}/api/models/{urllib.parse.quote(repo, safe='/')}",
                     headers=UA, timeout=30)
    r.raise_for_status()
    lic = (r.json().get("cardData") or {}).get("license", "unknown")
    t = requests.get(f"{HF}/api/models/{urllib.parse.quote(repo, safe='/')}"
                     f"/tree/main?recursive=true", headers=UA, timeout=30)
    t.raise_for_status()
    files = {f["path"]: f.get("size", 0) for f in t.json()
             if f.get("type") == "file"}
    return files, str(lic)

QUANT_PREF = ["Q4_K_M", "Q4_K_S", "IQ4_XS", "Q4_0", "Q5_K_M", "Q3_K_M"]

def pick_gguf(files, want_mmproj):
    gg = {p: s for p, s in files.items() if p.lower().endswith(".gguf") and s > 0}
    mm = {p: s for p, s in gg.items() if "mmproj" in p.lower()}
    main = {p: s for p, s in gg.items() if "mmproj" not in p.lower()}
    # whole-model total for split files: sum shards of the chosen stem
    def total_for(path):
        stem = path.split("-00001-of-")[0]
        return sum(s for p, s in main.items()
                   if p == path or p.startswith(stem + "-000"))
    chosen = None
    for q in QUANT_PREF:
        cand = [p for p in main if q.lower() in p.lower()]
        if cand:
            chosen = min(cand, key=lambda p: total_for(p))
            break
    if not chosen and main:
        chosen = min(main, key=lambda p: total_for(p))
    out = []
    if chosen:
        stem = chosen.split("-00001-of-")[0]
        out = sorted(p for p in main if p == chosen or p.startswith(stem + "-000"))
    if want_mmproj and mm:
        out.append(min(mm, key=lambda p: mm[p]))
    return out

def check_repo(repo, role):
    files, lic = repo_files(repo)
    notes, picked = [], []
    if role == "eyes":
        picked = pick_gguf(files, True)
        if not any("mmproj" in p.lower() for p in picked):
            notes.append("No mmproj projector in this repo — it cannot see.")
    elif role == "brain":
        picked = pick_gguf(files, False)
    elif role == "ears":
        cand = {p: s for p, s in files.items()
                if (p.lower().endswith(".bin") or p.lower().endswith(".gguf"))
                and 0 < s <= 600e6}
        if cand: picked = [min(cand, key=lambda p: cand[p])]
    elif role == "voice":
        cand = {p: s for p, s in files.items()
                if p.lower().endswith(".onnx") and 0 < s <= 400e6}
        if cand:
            p = min(cand, key=lambda p: cand[p])
            picked = [p] + ([p + ".json"] if p + ".json" in files else [])
    total = sum(files.get(p, 0) for p in picked)
    if not picked:
        notes.append("No usable artifact found for this role.")
    return {"repo": repo, "role": role, "license": lic, "files": picked,
            "sizes": {p: files.get(p, 0) for p in picked},
            "total_mb": round(total / 1e6), "ok": bool(picked) and not notes,
            "notes": notes}

# ---------------- downloads ----------------

def dest_for(repo, role, path):
    d = os.path.join(MODELS_DIR, role, repo.replace("/", "__"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, os.path.basename(path))

def run_download(repo, role, files):
    """Download to <name>.part (resumed with a Range request when a
    partial file is already there), move into place per file, then
    verify every file against the size HF listed for it. A short
    file marks the job PARTIAL — it stays on disk for inspection but
    is excluded from serving until a re-download completes it."""
    key = repo
    try:
        total = 0
        for path in files:
            url = f"{HF}/{repo}/resolve/main/{urllib.parse.quote(path)}"
            dest = dest_for(repo, role, path)
            part = dest + ".part"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = dict(UA)
            if have:
                headers["Range"] = f"bytes={have}-"
            with requests.get(url, headers=headers, stream=True,
                              timeout=60) as r:
                r.raise_for_status()
                if have and r.status_code == 200:
                    have = 0  # Range ignored — start this file over
                size = have + int(r.headers.get("Content-Length", 0))
                got = have
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk); got += len(chunk)
                        with jobs_lock:
                            jobs[key]["pct"] = -1  # bytes mode, see status
                            jobs[key]["got_mb"] = round((total + got) / 1e6)
                os.replace(part, dest)
                total += size
        entry = (META.get(repo) or {}).get("files") or {}
        d = os.path.join(MODELS_DIR, role, repo.replace("/", "__"))
        short = [name for name, exp in entry.items()
                 if not os.path.exists(os.path.join(d, name))
                 or os.path.getsize(os.path.join(d, name)) < exp]
        with jobs_lock:
            jobs[key].update({"done": True, "pct": 100,
                              "partial": bool(short),
                              "note": ("PARTIAL — short file(s): "
                                       + ", ".join(short)) if short else ""})
        persist_job(repo, role, None)
    except Exception as e:
        with jobs_lock:
            jobs[key].update({"error": str(e)[:200]})
        persist_job(repo, role, None)

def assess_repo(role, dirname):
    """Completeness of one downloaded repo dir, per file and overall."""
    d = os.path.join(MODELS_DIR, role, dirname)
    repo = dirname.replace("__", "/")
    expected = (META.get(repo) or {}).get("files") or {}
    files, total, exp_total, status = [], 0, 0, "complete"
    seen = set()
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p) or name.endswith(".part"):
            continue
        size = os.path.getsize(p)
        seen.add(name)
        exp = expected.get(name)
        st = file_status(size, exp)
        if exp is not None:
            exp_total += exp
        if st == "partial":
            status = "partial"
        total += size
        files.append({"name": name, "size_mb": round(size / 1e6, 1),
                      "expected_mb": (round(exp / 1e6, 1)
                                      if exp is not None else None),
                      "status": st})
    for name, exp in expected.items():
        if name not in seen:
            status = "partial"
            exp_total += exp
            files.append({"name": name, "size_mb": 0,
                          "expected_mb": round(exp / 1e6, 1),
                          "status": "missing"})
    if not expected:
        status = "unverified"
    return {"role": role, "repo": repo, "size_mb": round(total / 1e6, 1),
            "expected_mb": (round(exp_total / 1e6, 1) if expected else None),
            "status": status, "files": files}

def downloaded():
    out = []
    if os.path.isdir(MODELS_DIR):
        for role in sorted(os.listdir(MODELS_DIR)):
            rd = os.path.join(MODELS_DIR, role)
            if not os.path.isdir(rd): continue
            for repo in sorted(os.listdir(rd)):
                if os.path.isdir(os.path.join(rd, repo)):
                    out.append(assess_repo(role, repo))
    return out

# ---------------- llama-server ----------------

def llama_answer(prompt, image_b64):
    content = prompt
    if image_b64:
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image_b64}}]
    r = requests.post(f"{LLAMA}/v1/chat/completions",
                      json={"messages": [{"role": "user", "content": content}],
                            "max_tokens": 512, "temperature": 0.7},
                      timeout=600)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def served_model():
    """Best knowledge of which repo llama-server is currently serving:
    start.sh's serving.txt first, then the server's own model list."""
    try:
        with open(os.path.join(BASE, "serving.txt")) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if lines:
            repo = lines[-1].split("\t")[0]
            if repo and repo != "unknown":
                return repo
    except Exception:
        pass
    try:
        mid = requests.get(f"{LLAMA}/v1/models", timeout=3).json()["data"][0]["id"]
        base = os.path.basename(mid)
        for e in downloaded():
            if any(f["name"] == base for f in e["files"]):
                return e["repo"]
    except Exception:
        pass
    return "unknown"

# ---------------- glasses link layer (v0.10) ----------------
# Phone side of the FIRMWARE.md protocol. No glasses hardware yet:
# the simulated glasses speaks the real framed protocol in-process,
# and the phone's own camera is a first-class feed source whose
# frames (posted from the browser) run through the same pipeline —
# detect, track, log — that the glasses feed will use.

SIM = linkmod.SimulatedGlasses()
SESSION = linkmod.LinkSession(SIM)
TRACKER = irwatch.DetectionTracker()
feed_state = {"last_detect_seq": -1, "last_detect_at": 0.0,
              "phone_seq": 0, "note": None,
              "sess_state_seen": None, "sess_state_at": 0.0}


def thermal_preview():
    """Latest 40x30 radiometric preview as °C rows (FW-THM-01 BLE
    format), from whichever thermal source is live — today the
    simulated Lepton preview. None when no source is live."""
    fr = SIM.latest(linkmod.THERM)
    if not fr:
        return None
    hdr, payload = fr
    w, h = linkmod.THERM_PREVIEW_W, linkmod.THERM_PREVIEW_H
    if len(payload) < 2 * w * h:
        return None
    raw = struct.unpack("<%dH" % (w * h), payload[:2 * w * h])
    grid = [[round(raw[y * w + x] / 100.0 - 273.15, 1)
             for x in range(w)] for y in range(h)]
    return {"grid": grid, "w": w, "h": h, "seq": hdr["seq"],
            "source": "simulated Lepton preview (BLE-rate, FW-THM-01)"}


def _snapshot(jpeg_bytes):
    """Downscaled data-URL snapshot for a detection's detail card."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.load()
        if img.width > 480:
            img = img.resize((480, int(img.height * 480 / img.width)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=60)
        return ("data:image/jpeg;base64,"
                + base64.b64encode(buf.getvalue()).decode())
    except Exception:
        return None


def process_frame(jpeg_bytes, source, heading_deg):
    """One frame through the IR Watch pipeline: detect emitter
    clusters (with the thermal cross-check when a co-timed preview
    exists), track them across frames, and log each new detection
    to the session log. Returns the active detection list."""
    tp = thermal_preview()
    dets, err = irwatch.detect(jpeg_bytes, tp["grid"] if tp else None)
    if err:
        feed_state["note"] = err
        return TRACKER.public()
    feed_state["note"] = None
    snap = _snapshot(jpeg_bytes) if dets else None
    _active, fresh = TRACKER.update(dets, source, heading_deg,
                                     snapshot=snap)
    for d in fresh:
        log_session({"type": "ir_detection", "id": d["id"],
                     "label": d["label"],
                     "classification": d["classification"],
                     "confidence": d["confidence"],
                     "bearing_deg": d["bearing_deg"],
                     "source": source,
                     "thermal_note": d["thermal_note"],
                     "first_seen": d["first_seen"]})
    return TRACKER.public()


# ---------------- routes ----------------

@app.get("/")
def index():
    return render_template_string(UI)

@app.get("/api/health")
def health():
    llama = False
    try:
        llama = requests.get(f"{LLAMA}/health", timeout=3).status_code == 200
    except Exception:
        try:
            llama = requests.get(f"{LLAMA}/v1/models", timeout=3).status_code == 200
        except Exception:
            llama = False
    return jsonify({"app_version": "v0.10.3",
                    "llama": llama, "llama_url": LLAMA,
                    "served_model": served_model() if llama else None,
                    "downloaded": downloaded(),
                    "catalog": {k: len(v) for k, v in CATALOG.items()},
                    "sets": SETS})

@app.get("/api/search")
def search():
    role = request.args.get("role", "brain")
    q = (request.args.get("q") or "").lower()
    rows = [r for r in CATALOG.get(role, []) if q in r["repo"].lower()]
    return jsonify(rows[:60])

@app.get("/api/user")
def api_user():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Give a Hugging Face username"})
    try:
        r = requests.get(f"{HF}/api/models",
                         params={"author": name, "sort": "downloads",
                                 "direction": -1, "limit": 100},
                         headers=UA, timeout=30)
        r.raise_for_status()
        rows = [{"repo": m["id"], "downloads": m.get("downloads", 0),
                 "likes": m.get("likes", 0)} for m in r.json()]
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]})

@app.get("/api/check")
def api_check():
    repo = request.args.get("repo", "").strip()
    role = request.args.get("role", "brain")
    if "/" not in repo:
        return jsonify({"ok": False, "notes": ["Give a full repo: owner/name"]})
    try:
        res = check_repo(repo, role)
        record_expected(repo, role, res.get("sizes") or {})
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "notes": [f"Check failed: {str(e)[:160]}"]})

@app.post("/api/download")
def api_download():
    d = request.get_json(force=True)
    repo, role = d["repo"], d.get("role", "brain")
    files = d.get("files")
    if not files:
        try:
            files = check_repo(repo, role)["files"]
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]})
    if not files:
        return jsonify({"ok": False, "error": "Nothing downloadable for this role"})
    try:  # remember HF's listed sizes so the result can be verified
        tree, _lic = repo_files(repo)
        record_expected(repo, role, {p: tree.get(p, 0) for p in files})
    except Exception:
        pass
    with jobs_lock:
        if repo in jobs and not jobs[repo].get("done") and not jobs[repo].get("error"):
            return jsonify({"ok": True, "already": True})
        jobs[repo] = {"pct": 0, "done": False, "error": None, "role": role,
                      "got_mb": 0}
    persist_job(repo, role, files)
    threading.Thread(target=run_download, args=(repo, role, files),
                     daemon=True).start()
    return jsonify({"ok": True, "files": files})

@app.get("/api/jobs")
def api_jobs():
    with jobs_lock:
        return jsonify(dict(jobs))

# ---------------- link layer routes (v0.10) ----------------

@app.get("/api/link/status")
def api_link_status():
    SIM.start()  # the BLE side is always on; the sim's thermal
                 # preview flows whether or not a session is live
    SESSION.check_idle()
    # walk the modelled join (REQUESTED -> AP_UP -> STREAMING) at
    # poll pace so the lifecycle is visible in the UI
    st = SESSION.status()
    now = time.time()
    if feed_state["sess_state_seen"] != st["state"]:
        feed_state["sess_state_seen"] = st["state"]
        feed_state["sess_state_at"] = now
    elif st["state"] in (SESSION.REQUESTED, SESSION.AP_UP) \
            and now - feed_state["sess_state_at"] > 0.7:
        SESSION.advance()
        st = SESSION.status()
    tp = thermal_preview()
    return jsonify({
        "app_version": "v0.10.3",
        "session": st,
        "active_source": SESSION.source_kind,
        "sources": [
            {"kind": "simulated", "name": SIM.name, "real": False,
             "available": linkmod.HAVE_PIL,
             "note": "synthetic glasses in-process; emits the real "
                     "framed protocol (FEED/THERM) + telemetry"},
            {"kind": "phone", "name": "Phone camera", "real": True,
             "available": True,
             "note": "this phone's camera via the browser — a "
                     "first-class feed source, same pipeline"},
            {"kind": "tcp", "name": "Glasses feed (Wi-Fi session)",
             "real": False, "available": False,
             "note": "TCPFeedSource is implemented against the "
                     "framed protocol; no glasses hardware exists "
                     "to connect to yet"}],
        "sim_telemetry": SIM.telemetry(),
        "thermal": {"preview_available": tp is not None,
                    "full_frame": "wifi-only",
                    "full_frame_note": "Full 160x120 radiometric "
                        "frames ride Wi-Fi feed sessions only "
                        "(FW-THM-01). This surface shows the 40x30 "
                        "BLE-rate preview."},
        "irwatch_note": feed_state["note"]})


@app.post("/api/link/session")
def api_link_session():
    d = request.get_json(force=True)
    action = d.get("action")
    kind = d.get("source", "simulated")
    if action == "stop":
        SESSION.teardown("app request")
        return jsonify({"ok": True, "session": SESSION.status()})
    if action != "start":
        return jsonify({"ok": False,
                        "error": "action must be start or stop"})
    if kind == "simulated":
        if not SIM.start():
            return jsonify({"ok": False,
                            "error": "simulated source needs Pillow "
                                     "(pkg install python-pillow)"})
        soc = SIM.telemetry()["battery_pct"]
    else:
        soc = None
    ok, msg = SESSION.request(kind, soc)
    return jsonify({"ok": ok, "message": msg,
                    "session": SESSION.status()})


@app.get("/api/feed/latest")
def api_feed_latest():
    SESSION.check_idle()
    if SESSION.state != SESSION.STREAMING \
            or SESSION.source_kind != "simulated":
        return jsonify({"frame": None,
                        "reason": "no simulated feed streaming",
                        "detections": TRACKER.public()})
    fr = SIM.latest(linkmod.FEED)
    if not fr:
        return jsonify({"frame": None,
                        "reason": "first frame not generated yet",
                        "detections": TRACKER.public()})
    hdr, payload = fr
    SESSION.note_frame()
    dets = TRACKER.public()
    now = time.time()
    if hdr["seq"] != feed_state["last_detect_seq"] \
            and now - feed_state["last_detect_at"] >= 0.7:
        feed_state["last_detect_seq"] = hdr["seq"]
        feed_state["last_detect_at"] = now
        dets = process_frame(payload, "simulated glasses",
                             SIM.telemetry()["heading_deg"])
    return jsonify({"frame": {"seq": hdr["seq"], "ts_ms": hdr["ts_ms"],
                              "image": "data:image/jpeg;base64,"
                              + base64.b64encode(payload).decode()},
                    "detections": dets})


@app.post("/api/feed/frame")
def api_feed_frame():
    """The phone-camera source: the browser posts a frame from
    getUserMedia; it runs through the same IR Watch pipeline as a
    glasses frame. photo=true also files it as a session photo."""
    d = request.get_json(force=True)
    b64 = d.get("image") or ""
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        jpeg = base64.b64decode(b64)
    except Exception:
        return jsonify({"ok": False, "error": "image did not decode"})
    heading = d.get("heading")
    feed_state["phone_seq"] += 1
    SESSION.note_frame()
    dets = process_frame(jpeg, "phone camera", heading)
    if d.get("photo"):
        log_session({"type": "photo", "source": "phone camera",
                     "heading_deg": heading,
                     "note": "captured from the live phone-camera feed"})
        return jsonify({"ok": True, "photo": True, "detections": dets})
    return jsonify({"ok": True, "seq": feed_state["phone_seq"],
                    "detections": dets})


@app.post("/api/feed/photo")
def api_feed_photo():
    """Photo from the simulated glasses feed — a PHOTO on the same
    channel discipline as FW-FEED-04, logged like any photo."""
    fr = SIM.latest(linkmod.FEED)
    if not fr:
        return jsonify({"ok": False, "error": "no feed frame yet"})
    hdr, _payload = fr
    log_session({"type": "photo", "source": "simulated glasses",
                 "seq": hdr["seq"],
                 "note": "photo taken from the live simulated feed"})
    return jsonify({"ok": True, "photo": True, "seq": hdr["seq"]})


@app.get("/api/feed/thermal")
def api_feed_thermal():
    SIM.start()
    tp = thermal_preview()
    if not tp:
        return jsonify({"available": False,
                        "note": "no thermal source live",
                        "full_frame": "wifi-only"})
    flat = [v for row in tp["grid"] for v in row]
    return jsonify({"available": True, "source": tp["source"],
                    "seq": tp["seq"], "w": tp["w"], "h": tp["h"],
                    "grid": tp["grid"],
                    "min_c": min(flat), "max_c": max(flat),
                    "full_frame": "wifi-only",
                    "full_frame_note": "Full 160x120 radiometric "
                        "frames ride Wi-Fi feed sessions only "
                        "(FW-THM-01) — not this preview."})


@app.get("/api/irwatch")
def api_irwatch():
    return jsonify({
        "detections": TRACKER.public(),
        "limit": "A camera with its IR off emits nothing and is "
                 "invisible to this — IR Watch finds emitters, "
                 "not cameras."})

@app.post("/api/ask")
def api_ask():
    d = request.get_json(force=True)
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Ask something first."})
    try:
        ans = llama_answer(prompt, d.get("image"))
    except Exception as e:
        return jsonify({"error": "llama-server did not answer "
                                 f"({str(e)[:140]}). Start it per SETUP.md."})
    log_session({"type": "ask", "model": served_model(), "prompt": prompt,
                 "response": ans, "photo": bool(d.get("image"))})
    return jsonify({"answer": ans})

# ---------------- blind A/B (eval harness) ----------------
# Ask the same prompt on two served Brains. The first answer is held
# unlabeled; the second completes a pair shown as A/B in random
# order. Only the preference record — written after the user picks —
# names which model produced which answer.

ab_lock = threading.Lock()
ab_state = {"pending": None}

@app.post("/api/ab/ask")
def api_ab_ask():
    d = request.get_json(force=True)
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Ask something first."})
    try:
        ans = llama_answer(prompt, d.get("image"))
    except Exception as e:
        return jsonify({"error": "llama-server did not answer "
                                 f"({str(e)[:140]}). Start it per SETUP.md."})
    cand = {"model": served_model(), "answer": ans}
    with ab_lock:
        p = ab_state["pending"]
        if not p or p["prompt"] != prompt:
            ab_state["pending"] = {"prompt": prompt,
                                   "photo": bool(d.get("image")),
                                   "cands": [cand], "pair": None}
            return jsonify({"stage": "first",
                            "note": "First answer stored, unlabeled. Swap "
                                    "the served Brain, then ask the exact "
                                    "same question again."})
        if len(p["cands"]) == 1:
            p["cands"].append(cand)
            order = [0, 1]
            random.shuffle(order)
            p["pair"] = {"A": order[0], "B": order[1]}
        return jsonify({"stage": "second",
                        "a": p["cands"][p["pair"]["A"]]["answer"],
                        "b": p["cands"][p["pair"]["B"]]["answer"]})

@app.post("/api/ab/prefer")
def api_ab_prefer():
    d = request.get_json(force=True)
    choice = d.get("choice")
    if choice not in ("A", "B"):
        return jsonify({"error": "choice must be A or B"})
    with ab_lock:
        p = ab_state["pending"]
        if not p or not p.get("pair"):
            return jsonify({"error": "No A/B pair is waiting."})
        a = p["cands"][p["pair"]["A"]]
        b = p["cands"][p["pair"]["B"]]
        pref = a if choice == "A" else b
        log_session({"type": "ab_preference", "prompt": p["prompt"],
                     "photo": p["photo"],
                     "a_model": a["model"], "a_answer": a["answer"],
                     "b_model": b["model"], "b_answer": b["answer"],
                     "preferred": choice,
                     "preferred_model": pref["model"]})
        ab_state["pending"] = None
    return jsonify({"ok": True})

@app.post("/api/ab/reset")
def api_ab_reset():
    with ab_lock:
        ab_state["pending"] = None
    return jsonify({"ok": True})

@app.get("/api/export")
def api_export():
    data = b""
    if os.path.exists(SESSIONS):
        with open(SESSIONS, "rb") as f:
            data = f.read()
    return Response(data, mimetype="application/x-ndjson", headers={
        "Content-Disposition": "attachment; filename=sessions.jsonl"})

@app.get("/api/launch-candidates")
def api_launch_candidates():
    """Brain .gguf files start.sh may serve, best first: the Balanced
    set's Brain, then the other sets', then the rest by size. Partial
    downloads are excluded. (candidates.py mirrors this for when the
    app server itself is down.)"""
    order = []
    for name in SETS:
        repo = SETS[name]["parts"]["brain"]["repo"]
        if repo not in order:
            order.append(repo)
    brains = [e for e in downloaded()
              if e["role"] == "brain" and e["status"] != "partial"]
    brains.sort(key=lambda e: (order.index(e["repo"]) if e["repo"] in order
                               else 99, -e["size_mb"]))
    out = []
    for e in brains:
        d = os.path.join(MODELS_DIR, "brain", e["repo"].replace("/", "__"))
        for f in e["files"]:
            if (f["name"].lower().endswith(".gguf")
                    and "mmproj" not in f["name"].lower()):
                out.append({"repo": e["repo"], "status": e["status"],
                            "path": os.path.join(d, f["name"])})
    return jsonify(out)

UI = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IMP-VISION</title>
<script>document.documentElement.dataset.theme=localStorage.getItem('iv-theme')||'ash';</script>
<style>
:root,html[data-theme="ash"]{--bg:#060607;--panel:rgba(18,20,24,.78);--glass:rgba(18,20,24,.45);
--line:#33302e;--txt:#F4F1EA;--mut:#A39E93;--acc:#F80C00;--ochre:#E08E26;--ok:#7FB069;
--grid:rgba(163,158,147,.05);--sweep:rgba(248,12,0,.07);--vig:rgba(42,46,53,.38);--scan:rgba(255,255,255,.07)}
html[data-theme="sulfur"]{--bg:#060605;--panel:rgba(22,21,13,.78);--glass:rgba(22,21,13,.45);
--line:#3a3620;--txt:#F5F0DC;--mut:#ABA487;--acc:#E8C832;--ochre:#E07B00;--ok:#7FB069;
--grid:rgba(196,186,120,.055);--sweep:rgba(232,200,50,.09);--vig:rgba(48,45,30,.40);--scan:rgba(255,244,200,.06)}
html[data-theme="forge"]{--bg:#070504;--panel:rgba(24,17,12,.78);--glass:rgba(24,17,12,.45);
--line:#3d2c1e;--txt:#F7EDE4;--mut:#B09A86;--acc:#E8620C;--ochre:#E8C832;--ok:#7FB069;
--grid:rgba(216,160,110,.05);--sweep:rgba(232,98,12,.09);--vig:rgba(52,34,22,.40);--scan:rgba(255,230,200,.06)}
*{box-sizing:border-box}html,body{background:var(--bg)}
body{margin:0;color:var(--txt);
font-family:sans-serif;padding:14px 14px 84px}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:999;
background:repeating-linear-gradient(0deg,rgba(0,0,0,.26) 0 1px,transparent 1px 3px),
repeating-linear-gradient(0deg,var(--scan) 0 1px,transparent 1px 3px);
background-position:0 0,0 1px}
#tear{position:fixed;left:0;right:0;height:52px;top:-15%;z-index:998;pointer-events:none;opacity:0;
background:linear-gradient(180deg,transparent,var(--sweep),transparent);
animation:tear 7s steps(70) infinite}
@keyframes tear{0%{top:-15%;opacity:0;transform:none}
5%{opacity:.55}7%{transform:translateX(-6px)}9%{transform:translateX(5px)}
12%{top:108%;opacity:0;transform:none}100%{top:108%;opacity:0}}
.glitch{display:inline-block;animation:gl 4.6s steps(1) infinite}
@keyframes gl{0%,86%{text-shadow:none;transform:none}
87%{text-shadow:2px 0 var(--acc),-2px 0 var(--ochre);transform:translateX(1px)}
89%{text-shadow:-3px 0 var(--acc),3px 0 var(--ochre);transform:translateX(-2px) skewX(-4deg)}
91%{text-shadow:1px 0 var(--acc),-1px 0 var(--ochre);transform:none}
93%{text-shadow:-2px 0 var(--acc),2px 0 var(--ochre);transform:translateX(2px)}
95%,100%{text-shadow:none;transform:none}}
.sw{display:inline-block;width:14px;height:14px;border-radius:50%;margin-right:5px;
border:1px solid var(--line);vertical-align:-2px}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:-1;
background:
radial-gradient(ellipse at 50% 28%,var(--vig),transparent 62%),
linear-gradient(var(--grid) 1px,transparent 1px),
linear-gradient(90deg,var(--grid) 1px,transparent 1px),
linear-gradient(180deg,transparent 46%,var(--sweep) 50%,transparent 54%);
background-size:100% 100%,44px 44px,44px 44px,100% 280px;
background-repeat:no-repeat,repeat,repeat,repeat;
animation:bgmove 12s linear infinite}
@keyframes bgmove{
from{background-position:0 0,0 0,0 0,0 -280px}
to{background-position:0 0,44px 88px,44px 88px,0 0}}
.tag{font-family:monospace;font-size:11px;color:var(--mut);border:1px solid var(--line);
border-radius:6px;padding:2px 7px;letter-spacing:.14em;vertical-align:middle}
.card{position:relative}
.card.hot{border-color:var(--acc)}
.card.hot::before{content:"";position:absolute;top:-2px;left:-2px;width:15px;height:15px;
border-top:2px solid var(--acc);border-left:2px solid var(--acc);border-top-left-radius:13px;pointer-events:none}
.card.hot::after{content:"";position:absolute;bottom:-2px;right:-2px;width:15px;height:15px;
border-bottom:2px solid var(--acc);border-right:2px solid var(--acc);border-bottom-right-radius:13px;pointer-events:none}
.tchip.on{border-color:var(--acc);color:var(--acc)}
h1{font-size:20px;margin:4px 0}h2{font-size:16px;margin:18px 0 8px}
.mut{color:var(--mut);font-size:13px}.card{background:var(--panel);
border:1.5px solid var(--line);border-radius:14px;padding:12px;margin:8px 0;
box-shadow:inset 0 1px 0 rgba(244,241,234,.07);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
button{background:var(--glass);color:var(--txt);border:1px solid var(--line);
border-radius:22px;padding:10px 16px;font-size:15px;
backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
button:active{border-color:var(--acc)}
input,select,textarea{background:var(--glass);color:var(--txt);
border:1px solid var(--line);border-radius:10px;padding:10px;font-size:15px;width:100%}
.chip{display:inline-block;margin:3px}.chip.on{border-color:var(--ochre);color:var(--ochre)}
#askbtn{display:block;margin:22px auto;width:170px;height:170px;border-radius:50%;
border:4px solid var(--acc);background:none;color:var(--txt);font-size:26px;
box-shadow:0 0 0 7px #15171a,0 0 0 9px var(--line)}
nav{position:fixed;left:0;right:0;bottom:0;background:rgba(11,12,14,.85);display:flex;
border-top:1px solid var(--line);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);z-index:1000}
nav button{flex:1;border:none;border-radius:0;background:none;padding:16px 0}
nav button.on{color:var(--acc)}
.lic{color:var(--ok);font-size:12px;float:right}
.row b{font-size:15px}.view{display:none}.view.on{display:block}
textarea{min-height:74px}
</style></head><body>
<div id="tear"></div>

<div id="v-home" class="view on">
<h1><span class="glitch">IMP-VISION</span> <span class="tag">v0.10.3</span> <span class="mut" id="h-status">checking…</span></h1>
<div class="card"><b>Phone-only mode</b><div class="mut">No glasses paired —
this phone's camera and mic stand in for the senses. Models run locally;
nothing leaves the phone.</div></div>
<button id="askbtn" onclick="show('ask')">ASK</button>
<div class="card hot"><b>Compass</b> <span class="mut">phone sensor for now —
the glasses' temple IMU drives this same ribbon on hardware</span>
<div style="font-size:34px;text-align:center;margin:6px 0" id="compass-deg">—</div>
<div style="overflow:hidden;border:1px solid var(--line);border-radius:10px;position:relative;height:34px;background:var(--panel)">
<div id="ribbon" style="position:absolute;width:2160px;height:34px"></div>
<div style="position:absolute;left:50%;top:0;width:2px;height:34px;background:var(--acc)"></div>
</div>
<div class="mut">Heading follows where the phone points. On the glasses it follows your head.</div></div>
<div class="card"><b>On this phone</b><div class="mut" id="h-models">—</div></div>
<div class="card"><b>Session log</b><div class="mut">Every Ask is logged on
this phone (sessions.jsonl) for the eval harness — nothing leaves the phone
until you export it.</div>
<a href="/api/export"><button style="margin-top:8px">Export session log</button></a></div>
</div>

<div id="v-ask" class="view">
<h1>Ask</h1>
<textarea id="q" placeholder="Ask something… attach a photo when it needs eyes."></textarea>
<div style="margin:8px 0">
<label class="mut">Photo (camera): <input type="file" id="photo" accept="image/*" capture="environment" style="width:auto"></label>
</div>
<button onclick="ask()">Send</button>
<button onclick="speak()">Speak the answer</button>
<div class="card hot" id="answer" style="display:none"></div>
<div class="card"><b>Blind A/B</b>
<div class="mut">Ask the same question on two Brains; the pair comes back
labeled only A and B, in random order. Your pick is logged with the sealed
model identities for the eval harness.</div>
<label class="mut"><input type="checkbox" id="abmode" style="width:auto">
A/B mode — Send stores an unlabeled candidate</label><br>
<button onclick="abReset()" style="margin-top:8px">Reset A/B</button>
<div class="mut" id="ab-status" style="margin-top:6px"></div>
<div id="ab-pair" style="display:none">
<div class="card"><b>A</b><div id="ab-a"></div></div>
<div class="card"><b>B</b><div id="ab-b"></div></div>
<button onclick="abPrefer('A')">Prefer A</button>
<button onclick="abPrefer('B')">Prefer B</button>
</div></div>
</div>

<div id="v-senses" class="view">
<h1>Senses</h1>
<div class="card"><b>Feed source</b>
<div style="margin:8px 0">
<button class="chip src on" data-s="simulated">Simulated glasses</button>
<button class="chip src" data-s="phone">Phone camera</button>
</div>
<div class="mut" id="sess-state">No session. The link layer runs the
firmware protocol end-to-end — BLE-style control, per-session Wi-Fi
credentials, framed feed — against a simulated glasses until hardware
lands. The phone camera is a real source through the same pipeline.</div>
<button id="sessbtn" onclick="toggleSession()" style="margin-top:8px">Start feed session</button>
<div class="mut" id="sess-tele" style="margin-top:6px"></div>
</div>

<div class="card hot"><b>Live feed</b> <span class="mut" id="feed-src">—</span>
<div style="position:relative;margin-top:8px">
<canvas id="feedcv" width="640" height="480" style="width:100%;border-radius:10px;background:#000;display:block"></canvas>
<video id="phonevid" autoplay playsinline muted style="width:100%;border-radius:10px;background:#000;display:none"></video>
<canvas id="phonecv" width="640" height="480" style="position:absolute;inset:0;width:100%;height:100%;display:none"></canvas>
</div>
<div class="mut" id="feed-note">IR Watch analyses this feed — on hardware
at 5 fps (FW-FEED-03); in this rig about a frame a second.</div>
<button onclick="takePhoto()" style="margin-top:8px">Take photo</button>
<span class="mut" id="photo-note"></span>
</div>

<div class="card"><b>IR Source Watch</b>
<div class="mut">Passive detection of active IR emitters — camera
night-vision arrays, standalone illuminators, remotes. Every label names
the emitter and its pattern; there is no bare "camera found".</div>
<div class="mut" style="margin-top:6px;color:var(--ochre)">A camera with
its IR off emits nothing and is invisible to this — IR Watch finds
emitters, not cameras.</div>
<div class="mut" style="margin-top:6px">On this phone's camera: phone
lenses filter near-IR, and every model filters it differently — a bright
850 nm array may show, a 940 nm array may not. Detection on the phone
camera is best-effort. The glasses' camera has no IR-cut filter, so full
sensitivity there is a hardware property, not a setting.</div>
<div id="dets" style="margin-top:8px"><div class="mut">No emitters in view.</div></div>
<div class="card" id="det-detail" style="display:none"></div>
</div>

<div class="card"><b>Thermal</b> <span class="mut" id="therm-src">—</span>
<canvas id="thermcv" width="320" height="240" style="width:100%;border-radius:10px;background:#000"></canvas>
<div class="mut" id="therm-note">Preview rides the always-on link at
40×30 radiometric. Full 160×120 thermal frames ride a Wi-Fi feed session
only (FW-THM-01) — they are not part of this preview.</div>
</div>

<div class="card"><b>Detections &amp; photos</b><div class="mut">New
detections and photos join the session log (sessions.jsonl) alongside
every Ask — nothing leaves the phone until you export it.</div>
<a href="/api/export"><button style="margin-top:8px">Export session log</button></a></div>
</div>

<div id="v-models" class="view">
<h1>Models — the picker</h1>
<h2>Sets (RAM-checked, one tap)</h2>
<div id="sets"></div>
<h2>Browse the catalog</h2>
<div>
<button class="chip role on" data-r="eyes">Eyes</button>
<button class="chip role" data-r="brain">Brain</button>
<button class="chip role" data-r="ears">Ears</button>
<button class="chip role" data-r="voice">Voice</button>
</div>
<input id="search" placeholder="Search this role's list…" oninput="doSearch()">
<div id="results"></div>
<h2>Search by user</h2>
<input id="usearch" placeholder="Hugging Face username — e.g. Devilishcode">
<button onclick="doUser()" style="margin-top:8px">Show their repos</button>
<div class="mut" style="margin-top:6px">Every public model repo under that name, most downloaded first. Download files it under the role selected above. Public repos only — private ones stay hidden without a sign-in.</div>
<div id="uresults"></div>
<h2>Paste any Hugging Face repo</h2>
<input id="paste" placeholder="owner/name">
<button onclick="doCheck()" style="margin-top:8px">Run the checks</button>
<div id="check"></div>
<h2>Downloads</h2>
<div id="jobs" class="mut">None running.</div>
</div>

<div id="v-themes" class="view">
<h1>Themes</h1>
<div class="mut" style="margin-bottom:8px">Window colorway for the whole interface — glass, grid, sweep and accents move together. Saved on this phone.</div>
<div class="card tchip-card" data-t="ash"><b>TUNGSTEN ASH</b>
<div style="margin:7px 0"><span class="sw" style="background:#F80C00"></span><span class="sw" style="background:#121418"></span><span class="sw" style="background:#F4F1EA"></span><span class="sw" style="background:#E08E26"></span></div>
<div class="mut">The OS colorway. Hazard red on smoked optic glass, titanium telemetry.</div>
<button class="chip tchip" data-t="ash" onclick="setTheme('ash')">Apply</button></div>
<div class="card tchip-card" data-t="sulfur"><b>SULFUR</b>
<div style="margin:7px 0"><span class="sw" style="background:#E8C832"></span><span class="sw" style="background:#16150d"></span><span class="sw" style="background:#F5F0DC"></span><span class="sw" style="background:#E07B00"></span></div>
<div class="mut">Brimstone. Sulfur yellow accent, warmed glass and grid.</div>
<button class="chip tchip" data-t="sulfur" onclick="setTheme('sulfur')">Apply</button></div>
<div class="card tchip-card" data-t="forge"><b>FORGE</b>
<div style="margin:7px 0"><span class="sw" style="background:#E8620C"></span><span class="sw" style="background:#18110c"></span><span class="sw" style="background:#F7EDE4"></span><span class="sw" style="background:#E8C832"></span></div>
<div class="mut">Thermal orange off the bench — heat accent on scorched glass.</div>
<button class="chip tchip" data-t="forge" onclick="setTheme('forge')">Apply</button></div>
</div>

<nav>
<button data-v="home" class="on" onclick="show('home')">Home</button>
<button data-v="ask" onclick="show('ask')">Ask</button>
<button data-v="senses" onclick="show('senses')">Senses</button>
<button data-v="models" onclick="show('models')">Models</button>
<button data-v="themes" onclick="show('themes')">Themes</button>
</nav>

<script>
let role='eyes', lastAnswer='';
function show(v){document.querySelectorAll('.view').forEach(e=>e.classList.remove('on'));
document.getElementById('v-'+v).classList.add('on');
document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
if(v==='senses')startSenses();else stopSenses();}
function setTheme(t){document.documentElement.dataset.theme=t;
localStorage.setItem('iv-theme',t);
document.querySelectorAll('.tchip').forEach(c=>c.classList.toggle('on',c.dataset.t===t));
document.querySelectorAll('.tchip-card').forEach(c=>c.classList.toggle('hot',c.dataset.t===t));}
setTheme(document.documentElement.dataset.theme||'ash');
document.querySelectorAll('.role').forEach(b=>b.onclick=()=>{role=b.dataset.r;
document.querySelectorAll('.role').forEach(x=>x.classList.toggle('on',x===b));doSearch();});
async function health(){let h=await (await fetch('/api/health')).json();
document.getElementById('h-status').textContent=
(h.llama?'· llama-server up':'· llama-server down — see SETUP.md');
document.getElementById('h-models').textContent=h.downloaded.length?
h.downloaded.map(d=>d.role+': '+d.repo+' ('+d.size_mb+' MB'+(d.status==='partial'?' of '+d.expected_mb+' — PARTIAL, re-download to complete':'')+')').join(' · '):'No models yet — open Models.';
let sd=document.getElementById('sets');sd.innerHTML='';
Object.entries(h.sets).forEach(([name,s])=>{let d=document.createElement('div');
d.className='card';d.innerHTML='<b>'+name+'</b> <span class="mut">'+s.note+'</span><br>'+
Object.entries(s.parts).map(([r,p])=>'<span class="mut">'+r+': '+p.repo+'</span>').join('<br>')+
'<br><button>Install set</button>';d.querySelector('button').onclick=()=>installSet(s);
sd.appendChild(d);});}
async function installSet(s){for(let [r,p] of Object.entries(s.parts)){
await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({repo:p.repo,role:r,files:p.files||null})});}pollJobs();}
async function doSearch(){let q=document.getElementById('search').value;
let rows=await (await fetch('/api/search?role='+role+'&q='+encodeURIComponent(q))).json();
let el=document.getElementById('results');el.innerHTML='';
rows.forEach(r=>{let d=document.createElement('div');d.className='card row';
d.innerHTML='<span class="lic">'+r.license+'</span><b>'+r.repo+'</b><br>'+
'<span class="mut">'+r.size_mb+' MB · '+r.downloads+' downloads</span> ';
let b=document.createElement('button');b.textContent='Download';
b.onclick=()=>download(r.repo);d.appendChild(b);el.appendChild(d);});}
async function doUser(){let name=document.getElementById('usearch').value.trim();
let el=document.getElementById('uresults');el.innerHTML='<div class="card">Searching…</div>';
let r=await (await fetch('/api/user?name='+encodeURIComponent(name))).json();
el.innerHTML='';
if(!r.ok){el.innerHTML='<div class="card">'+(r.error||'Search failed')+'</div>';return;}
if(!r.rows.length){el.innerHTML='<div class="card">No public repos under that name.</div>';return;}
r.rows.forEach(m=>{let d=document.createElement('div');d.className='card row';
d.innerHTML='<b>'+m.repo+'</b><br>'+
'<span class="mut">'+m.downloads+' downloads · '+m.likes+' likes</span> ';
let b=document.createElement('button');b.textContent='Download';
b.onclick=()=>download(m.repo);d.appendChild(b);el.appendChild(d);});}
async function download(repo,files){await fetch('/api/download',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({repo:repo,role:role,files:files||null})});pollJobs();}
async function doCheck(){let repo=document.getElementById('paste').value.trim();
let el=document.getElementById('check');el.innerHTML='<div class="card">Checking…</div>';
let c=await (await fetch('/api/check?role='+role+'&repo='+encodeURIComponent(repo))).json();
let d=document.createElement('div');d.className='card';
d.innerHTML='<b>'+c.repo+'</b> <span class="lic">'+(c.license||'')+'</span><br>'+
'<span class="mut">'+(c.files?c.files.join('<br>'):'')+'</span><br>'+
'<span class="mut">Total '+(c.total_mb||0)+' MB. '+(c.notes||[]).join(' ')+'</span> ';
if(c.files&&c.files.length){let b=document.createElement('button');b.textContent='Download';
b.onclick=()=>download(c.repo,c.files);d.appendChild(b);}
el.innerHTML='';el.appendChild(d);}
async function pollJobs(){let j=await (await fetch('/api/jobs')).json();
let el=document.getElementById('jobs');let ks=Object.keys(j);
el.textContent=ks.length?ks.map(k=>{let v=j[k];return k+': '+
(v.error?'FAILED '+v.error:v.done?(v.partial?'done — PARTIAL: '+v.note:'done'):'downloading… '+v.got_mb+' MB');}).join(' · '):'None running.';
if(ks.some(k=>!j[k].done&&!j[k].error))setTimeout(pollJobs,2000);else health();}
let imgB64='';
document.getElementById('photo').onchange=e=>{let f=e.target.files[0];if(!f)return;
let rd=new FileReader();rd.onload=()=>{imgB64=rd.result.split(',')[1];};rd.readAsDataURL(f);};
async function ask(){if(document.getElementById('abmode').checked)return abAsk();
let a=document.getElementById('answer');a.style.display='block';
a.textContent='Thinking… (the model runs on this phone — first answer can take a while)';
let r=await (await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({prompt:document.getElementById('q').value,image:imgB64||null})})).json();
lastAnswer=r.answer||'';a.textContent=r.answer||('Error: '+r.error);}
async function abAsk(){let st=document.getElementById('ab-status');
st.textContent='Asking the Brain now serving…';
let r=await (await fetch('/api/ab/ask',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({prompt:document.getElementById('q').value,image:imgB64||null})})).json();
if(r.error){st.textContent='Error: '+r.error;return;}
if(r.stage==='first'){st.textContent='Candidate stored from the Brain now serving. Swap the served Brain (restart llama-server with the other model), then send the exact same question again.';return;}
document.getElementById('ab-a').textContent=r.a;
document.getElementById('ab-b').textContent=r.b;
document.getElementById('ab-pair').style.display='block';
st.textContent='Pick the better answer — A and B are in random order.';}
async function abPrefer(c){let r=await (await fetch('/api/ab/prefer',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:c})})).json();
document.getElementById('ab-status').textContent=r.ok?
'Preference recorded — the session log holds both answers with their sealed model identities.':
('Error: '+r.error);
if(r.ok)document.getElementById('ab-pair').style.display='none';}
async function abReset(){await fetch('/api/ab/reset',{method:'POST'});
document.getElementById('ab-pair').style.display='none';
document.getElementById('ab-status').textContent='A/B cleared.';}
function speak(){if(lastAnswer)speechSynthesis.speak(new SpeechSynthesisUtterance(lastAnswer));}
(function(){let rb=document.getElementById('ribbon');let names={0:'N',45:'NE',90:'E',135:'SE',180:'S',225:'SW',270:'W',315:'NW'};
for(let rep=0;rep<3;rep++)for(let d=0;d<360;d+=15){let sp=document.createElement('span');
sp.style.cssText='position:absolute;top:0;line-height:34px;font-size:'+(d%45?'10':'15')+'px;color:'+(d%90?'var(--mut)':'var(--txt)');
sp.style.left=(rep*720+d*2-8)+'px';sp.textContent=(d%45==0)?names[d]:d;rb.appendChild(sp);}})();
function card(h){return ['N','NE','E','SE','S','SW','W','NW'][Math.round(h/45)%8];}
let curHeading=null;
function onOrient(e){let h=null;
if(e.webkitCompassHeading!=null)h=e.webkitCompassHeading;
else if(e.absolute&&e.alpha!=null)h=360-e.alpha;
if(h==null)return;h=(h%360+360)%360;curHeading=h;
document.getElementById('compass-deg').textContent=Math.round(h)+'° '+card(h);
let off=(window.innerWidth/2-28)-(h/360)*720;
document.getElementById('ribbon').style.transform='translateX('+((off%720)-720)+'px)';}
window.addEventListener('deviceorientationabsolute',onOrient,true);
window.addEventListener('deviceorientation',onOrient,true);

/* ---- Senses: link layer + IR Watch (v0.10) ---- */
let srcKind='simulated', sessOn=false, sensesTimer=null,
phoneStream=null, phoneBusy=false, lastDets=[], sessHold='';
document.querySelectorAll('.src').forEach(b=>b.onclick=async()=>{
srcKind=b.dataset.s;
document.querySelectorAll('.src').forEach(x=>x.classList.toggle('on',x===b));
if(sessOn)await toggleSession();
document.getElementById('det-detail').style.display='none';
syncSourceUI();});
function syncSourceUI(){
let ph=srcKind==='phone';
document.getElementById('feedcv').style.display=ph?'none':'block';
document.getElementById('phonevid').style.display=ph?'block':'none';
document.getElementById('phonecv').style.display=ph?'block':'none';
document.getElementById('feed-src').textContent=ph?
'phone camera — real frames, this pipeline':
'simulated glasses — synthetic frames, real protocol';}
function startSenses(){syncSourceUI();
if(!sensesTimer)sensesTimer=setInterval(sensesTick,700);sensesTick();
autoFeed();}
function stopSenses(){if(sensesTimer){clearInterval(sensesTimer);sensesTimer=null;}
stopPhone();
if(sessOn){fetch('/api/link/session',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({action:'stop'})});sessOn=false;}}
async function autoFeed(){
if(sessOn)return;
srcKind='phone';
document.querySelectorAll('.src').forEach(x=>x.classList.toggle('on',x.dataset.s==='phone'));
syncSourceUI();
let r=await (await fetch('/api/link/session',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({action:'start',source:'phone'})})).json();
if(!r.ok){sessHold='Feed refused: '+(r.message||r.error||'unknown');
document.getElementById('sess-state').textContent=sessHold;return;}
sessOn=true;
if(!await startPhone()){
await fetch('/api/link/session',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({action:'stop'})});sessOn=false;}
pollLink();}
async function sensesTick(){pollLink();
if(sessOn&&srcKind==='simulated')pollFeed();
if(sessOn&&srcKind==='phone')grabPhoneFrame();
pollThermal();}
async function pollLink(){let s;
try{s=await (await fetch('/api/link/status')).json();}catch(e){return;}
let ss=s.session;
sessOn=(ss.state==='STREAMING'||ss.state==='AP_UP'||ss.state==='REQUESTED');
if(sessOn)sessHold='';
document.getElementById('sessbtn').textContent=sessOn?'Stop session':'Start feed session';
let line='Session: '+ss.state+(ss.ssid?' · AP '+ss.ssid:'')+' · '+ss.reason;
if(ss.passphrase)line+=' · passphrase '+ss.passphrase+'.';
if(!sessHold)document.getElementById('sess-state').textContent=line;
let t=s.sim_telemetry;
document.getElementById('sess-tele').textContent=
srcKind==='simulated'&&t&&t.build?
('Simulated glasses: '+t.build+' build, variant '+t.variant+' · battery '+
t.battery_pct+'% · heading '+t.heading_deg+'° · '+(t.camera_rail?'camera rail up':'camera rail off')):
(srcKind==='phone'?'Phone source: heading comes from this phone\'s compass (Home card).':'');
if(s.irwatch_note)document.getElementById('feed-note').textContent=s.irwatch_note;}
async function toggleSession(){
if(sessOn){await fetch('/api/link/session',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({action:'stop'})});
sessOn=false;stopPhone();
document.getElementById('sessbtn').textContent='Start feed session';}
else{let r=await (await fetch('/api/link/session',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({action:'start',source:srcKind})})).json();
if(!r.ok){sessHold='Session refused: '+(r.message||r.error||'unknown');
document.getElementById('sess-state').textContent=sessHold;return;}
if(srcKind==='phone')startPhone();}
pollLink();}
function drawBoxes(cv,dets){
let ctx=cv.getContext('2d');ctx.strokeStyle='#F80C00';ctx.lineWidth=3;
ctx.font='15px monospace';ctx.fillStyle='#F80C00';
dets.forEach(d=>{let b=d.bbox,x=b.x*cv.width,y=b.y*cv.height,
w=b.w*cv.width,h=b.h*cv.height;
ctx.strokeRect(x,y,w,h);ctx.fillText(d.label,x+2,Math.max(14,y-5));});}
let feedImg=new Image();
async function pollFeed(){let r;
try{r=await (await fetch('/api/feed/latest')).json();}catch(e){return;}
if(r.detections)renderDets(r.detections);
if(!r.frame)return;
await new Promise(res=>{feedImg.onload=res;feedImg.src=r.frame.image;});
let cv=document.getElementById('feedcv');
cv.getContext('2d').drawImage(feedImg,0,0,cv.width,cv.height);
drawBoxes(cv,r.detections||[]);}
function renderDets(dets){lastDets=dets||[];
let el=document.getElementById('dets');el.innerHTML='';
if(!lastDets.length){el.innerHTML='<div class="mut">No emitters in view.</div>';return;}
lastDets.forEach(d=>{let b=document.createElement('button');
b.style.display='block';b.style.margin='4px 0';b.style.textAlign='left';
b.textContent=d.label+(d.bearing_deg!=null?' · bearing '+Math.round(d.bearing_deg)+'°':'')+' · '+d.source;
b.onclick=()=>showDetail(d);el.appendChild(b);});}
function showDetail(d){let el=document.getElementById('det-detail');
el.style.display='block';
el.innerHTML='<b>'+d.label+'</b>'+
(d.snapshot?'<img src="'+d.snapshot+'" style="width:100%;border-radius:10px;margin:8px 0">':'')+
'<div class="mut">Classification: '+d.classification+' · confidence '+
Math.round(d.confidence*100)+'%</div>'+
'<div class="mut">Bearing: '+(d.bearing_deg!=null?Math.round(d.bearing_deg)+'° (phone heading + position in frame, ≈62° lens)':'—')+'</div>'+
'<div class="mut">First seen: '+d.first_seen+' · source: '+d.source+'</div>'+
'<div class="mut">'+d.thermal_note+'</div>'+
'<div class="mut">Logged to the session log at first sighting.</div>';}
async function pollThermal(){let r;
try{r=await (await fetch('/api/feed/thermal')).json();}catch(e){return;}
let cv=document.getElementById('thermcv'),ctx=cv.getContext('2d');
if(!r.available){document.getElementById('therm-src').textContent='no source live';return;}
document.getElementById('therm-src').textContent=
r.source+' · '+r.min_c.toFixed(1)+'–'+r.max_c.toFixed(1)+' °C · seq '+r.seq;
let cw=cv.width/r.w,ch=cv.height/r.h;
for(let y=0;y<r.h;y++)for(let x=0;x<r.w;x++){
ctx.fillStyle=iron((r.grid[y][x]-r.min_c)/Math.max(0.1,r.max_c-r.min_c));
ctx.fillRect(x*cw,y*ch,Math.ceil(cw),Math.ceil(ch));}}
let IRON=[[0,[0,0,4]],[0.28,[60,10,80]],[0.5,[160,30,90]],
[0.72,[230,90,20]],[0.88,[250,180,40]],[1,[255,255,240]]];
function iron(t){t=Math.max(0,Math.min(1,t));
for(let i=1;i<IRON.length;i++){if(t<=IRON[i][0]){
let a=IRON[i-1],b=IRON[i],f=(t-a[0])/(b[0]-a[0]);
return 'rgb('+a[1].map((v,k)=>Math.round(v+(b[1][k]-v)*f)).join(',')+')';}}
return 'rgb(255,255,240)';}
async function startPhone(){
if(phoneStream)return true;
try{phoneStream=await navigator.mediaDevices.getUserMedia(
{video:{facingMode:'environment'},audio:false});
document.getElementById('phonevid').srcObject=phoneStream;return true;}
catch(e){sessHold='Phone camera unavailable: '+e.message+
' — allow camera access for this page, then re-open Senses.';
document.getElementById('sess-state').textContent=sessHold;return false;}}
function stopPhone(){if(phoneStream){
phoneStream.getTracks().forEach(t=>t.stop());phoneStream=null;}}
async function grabPhoneFrame(){
let v=document.getElementById('phonevid');
if(phoneBusy||!phoneStream||v.readyState<2)return;
phoneBusy=true;
try{let c=document.createElement('canvas');c.width=640;c.height=480;
c.getContext('2d').drawImage(v,0,0,640,480);
let r=await (await fetch('/api/feed/frame',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({image:c.toDataURL('image/jpeg',0.72).split(',')[1],
heading:curHeading})})).json();
if(r.detections){renderDets(r.detections);
let oc=document.getElementById('phonecv');
oc.getContext('2d').clearRect(0,0,oc.width,oc.height);
drawBoxes(oc,r.detections);}}
catch(e){}
phoneBusy=false;}
async function takePhoto(){
let note=document.getElementById('photo-note');
if(!sessOn){note.textContent='Start a session first.';return;}
if(srcKind==='phone'){
let v=document.getElementById('phonevid');
if(!phoneStream||v.readyState<2){note.textContent='Camera not ready.';return;}
let c=document.createElement('canvas');c.width=640;c.height=480;
c.getContext('2d').drawImage(v,0,0,640,480);
let r=await (await fetch('/api/feed/frame',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({image:c.toDataURL('image/jpeg',0.8).split(',')[1],
heading:curHeading,photo:true})})).json();
note.textContent=r.ok?'Photo taken — in the session log.':'Photo failed.';}
else{let r=await (await fetch('/api/feed/photo',{method:'POST'})).json();
note.textContent=r.ok?'Photo taken — in the session log.':(r.error||'Photo failed.');}}
health();doSearch();
</script></body></html>
"""

if __name__ == "__main__":
    os.makedirs(MODELS_DIR, exist_ok=True)
    resume_jobs()
    app.run(host="127.0.0.1", port=5000)
