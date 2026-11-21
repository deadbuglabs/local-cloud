"""IMP-VISION glasses link layer — phone side (app v0.10).

Implements the phone half of the temple firmware protocol
(designs/temple-f/FIRMWARE.md §13):

- Wi-Fi feed channel: one TCP connection carrying length-prefixed
  frames — 16-byte header + payload. Header layout (big-endian):

      offset 0  magic   2s  b"IV"
      offset 2  type    u8  FEED=1 PHOTO=2 THUMB=3 THERM=4
      offset 3  flags   u8  bit0 illuminators on, bit1 FFC stall
      offset 4  seq     u32 frame sequence (per type)
      offset 8  ts_ms   u32 capture timestamp, ms since session start
      offset 12 length  u32 payload length in bytes

  FEED/PHOTO/THUMB payloads are OV2640 hardware JPEG, never
  transcoded (FW-FEED-01). THERM payloads are Lepton radiometric
  frames: full 160x120 uint16 centi-Kelvin in Wi-Fi sessions
  (38,400 bytes), or the 40x30 uint16 preview (2,400 bytes) that
  also rides BLE (FW-THM-01).

- BLE control channel: modelled here as LinkSession — the session
  lifecycle of FW-WIFI-03 (IDLE -> REQUESTED -> AP_UP -> STREAMING
  -> TEARDOWN), per-session credentials generated on request and
  held in RAM only (FW-WIFI-02), the 20% battery gate
  (FW-WIFI-06) and the 15 s idle teardown (FW-WIFI-05).

No glasses hardware exists yet, so sources are pluggable:

- SimulatedGlasses — a synthetic glasses generated in-process:
  FEED frames (a dark room with IR emitter geometry IR Watch can
  classify), THERM preview frames (40x30 radiometric), telemetry.
  Everything it emits travels the exact codec above.
- TCPFeedSource — the real client for a glasses soft-AP feed
  session. Complete code, nothing to connect to until hardware.
- The phone camera is a third source; it lives in the browser and
  posts frames to the app server, which feeds them through the
  same pipeline (see app.py /api/feed/frame).
"""
import io, math, secrets, string, struct, threading, time

try:
    from PIL import Image, ImageDraw
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

MAGIC = b"IV"
HEADER = struct.Struct(">2sBBIII")
FEED, PHOTO, THUMB, THERM = 1, 2, 3, 4
TYPE_NAMES = {FEED: "FEED", PHOTO: "PHOTO", THUMB: "THUMB", THERM: "THERM"}

THERM_PREVIEW_W, THERM_PREVIEW_H = 40, 30     # FW-THM-01 BLE preview
THERM_FULL_W, THERM_FULL_H = 160, 120         # Wi-Fi sessions only


def pack_frame(ftype, seq, ts_ms, payload, flags=0):
    return HEADER.pack(MAGIC, ftype, flags, seq & 0xFFFFFFFF,
                       ts_ms & 0xFFFFFFFF, len(payload)) + payload


def unpack_header(buf):
    magic, ftype, flags, seq, ts_ms, length = HEADER.unpack(buf[:16])
    if magic != MAGIC:
        raise ValueError("bad frame magic")
    return {"type": ftype, "flags": flags, "seq": seq,
            "ts_ms": ts_ms, "length": length}


class FrameReader:
    """Incremental parser for the framed stream (FW-FEED-01)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        """Yield (header, payload) for every complete frame in data."""
        self.buf += data
        while len(self.buf) >= 16:
            hdr = unpack_header(bytes(self.buf[:16]))
            end = 16 + hdr["length"]
            if len(self.buf) < end:
                return
            payload = bytes(self.buf[16:end])
            del self.buf[:end]
            yield hdr, payload


class FrameSource:
    """Transport interface: a source of protocol frames + telemetry."""

    name = "base"
    kind = "base"          # "simulated" | "tcp" | "phone"
    simulated = False

    def start(self):
        pass

    def stop(self):
        pass

    def latest(self, ftype):
        """(header, payload) of the newest frame of a type, or None."""
        return None

    def telemetry(self):
        return {}


# ---------------- simulated glasses ----------------

class SimulatedGlasses(FrameSource):
    """A synthetic FULL-build glasses (variant R) in-process.

    FEED frames are produced only while a feed session is streaming
    (the camera-rail discipline of FW-FEED-06); the THERM preview
    flows whenever the source is started, as it would over BLE
    (FW-THM-01). The scene holds two classifiable IR emitters: a
    ring of LEDs around a dim centre (camera-type) and a lone
    filled panel (illuminator), plus a small blinking remote dot.
    """

    name = "Simulated glasses (FULL build, variant R)"
    kind = "simulated"
    simulated = True
    W, H = 640, 480

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = {}            # ftype -> (header, payload)
        self._seq = {FEED: 0, THERM: 0, PHOTO: 0}
        self._run = False
        self._streaming = False
        self._t0 = time.time()
        self._soc = 87.0
        self._thread = None

    # session hooks (called by LinkSession)
    def set_streaming(self, on):
        self._streaming = bool(on)

    def start(self):
        if not HAVE_PIL:
            return False
        with self._lock:
            if self._run:
                return True
            self._run = True
            self._t0 = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self._lock:
            self._run = False

    def latest(self, ftype):
        with self._lock:
            return self._frames.get(ftype)

    def telemetry(self):
        t = time.time() - self._t0
        return {"build": "FULL", "variant": "R", "simulated": True,
                "battery_pct": round(self._soc, 1),
                "charging": False,
                "heading_deg": round((t * 3.0) % 360, 1),
                "illuminators": self._streaming,
                "camera_rail": self._streaming,
                "thermal": "lepton-preview",
                "link": "BLE (modelled) + Wi-Fi session (modelled)"}

    # -- scene generation --
    def _emitter_pos(self, t):
        cx = 205 + 26 * math.sin(t * 0.35)
        cy = 150 + 10 * math.sin(t * 0.22 + 1.0)
        px = 492 + 8 * math.sin(t * 0.15 + 2.0)
        py = 118.0
        return (cx, cy), (px, py)

    def _feed_jpeg(self, t):
        img = Image.new("RGB", (self.W, self.H), (10, 11, 14))
        d = ImageDraw.Draw(img)
        for y in range(self.H):                      # dim room gradient
            v = 10 + int(16 * y / self.H)
            d.line([(0, y), (self.W, y)], fill=(v, v, v + 4))
        d.rectangle([60, 90, 250, 430], outline=(52, 54, 60), width=3)   # doorway
        d.rectangle([400, 60, 590, 330], outline=(40, 42, 48), width=2)  # shelf
        d.line([(0, 432), (self.W, 432)], fill=(60, 62, 68), width=2)    # floor line
        (cx, cy), (px, py) = self._emitter_pos(t)
        # illuminator: lone filled bright panel + halo
        d.ellipse([px - 44, py - 34, px + 44, py + 34], fill=(70, 70, 74))
        d.rounded_rectangle([px - 30, py - 21, px + 30, py + 21],
                            radius=6, fill=(252, 252, 252))
        # camera-type: ring of bright LEDs around a dim centre
        for k in range(8):
            a = 2 * math.pi * k / 8
            x, y = cx + 27 * math.cos(a), cy + 27 * math.sin(a)
            d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(96, 96, 100))
            d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=(255, 255, 255))
        d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=(58, 58, 62))
        # remote: small single dot, blinking
        if int(t * 2) % 6 < 4:
            d.ellipse([352, 306, 364, 318], fill=(245, 245, 245))
        d.text((8, 8), f"SIM FEED seq {self._seq[FEED]}", fill=(120, 124, 130))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=72)
        return out.getvalue()

    def _therm_payload(self, t):
        """40x30 uint16 centi-Kelvin (TLinear), FW-THM-01 preview."""
        vals = []
        sway = 3.0 * math.sin(t * 0.3)
        (cx, cy), _ = self._emitter_pos(t)
        gx, gy = cx / self.W * 40, cy / self.H * 30
        for y in range(THERM_PREVIEW_H):
            for x in range(THERM_PREVIEW_W):
                temp = 21.5 + 1.5 * y / THERM_PREVIEW_H          # room
                db = math.hypot(x - (19 + sway), y - 17)         # warm body
                temp += 13.0 * math.exp(-(db * db) / 26.0)
                dc = math.hypot(x - gx, y - gy)                  # camera body heat
                temp += 7.0 * math.exp(-(dc * dc) / 5.0)
                vals.append(int((temp + 273.15) * 100))
        return struct.pack("<%dH" % len(vals), *vals)

    def _loop(self):
        last_feed = last_therm = 0.0
        while True:
            with self._lock:
                if not self._run:
                    return
            now = time.time()
            t = now - self._t0
            if self._streaming and now - last_feed >= 0.2:       # 5 fps, FW-FEED-03 IR Watch rate
                last_feed = now
                self._seq[FEED] += 1
                payload = self._feed_jpeg(t)
                hdr = {"type": FEED, "flags": 1, "seq": self._seq[FEED],
                       "ts_ms": int(t * 1000), "length": len(payload)}
                with self._lock:
                    self._frames[FEED] = (hdr, payload)
                self._soc = max(4.0, self._soc - 0.004)          # FEED draw
            if now - last_therm >= 0.5:                          # 2 fps preview, FW-THM-01
                last_therm = now
                self._seq[THERM] += 1
                payload = self._therm_payload(t)
                hdr = {"type": THERM, "flags": 0, "seq": self._seq[THERM],
                       "ts_ms": int(t * 1000), "length": len(payload)}
                with self._lock:
                    self._frames[THERM] = (hdr, payload)
            time.sleep(0.05)


# ---------------- real TCP feed client (hardware, when it exists) ----

class TCPFeedSource(FrameSource):
    """Client for a glasses soft-AP feed session (FW §3). Speaks the
    framed protocol over one TCP connection; frames land in the
    same latest-per-type buffer the simulated source fills."""

    name = "Glasses feed (Wi-Fi session)"
    kind = "tcp"

    def __init__(self, host, port=8363):
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._frames = {}
        self._run = False
        self.error = None

    def start(self):
        import socket
        self._run = True

        def run():
            try:
                s = socket.create_connection((self.host, self.port),
                                             timeout=10)
                reader = FrameReader()
                while self._run:
                    data = s.recv(65536)
                    if not data:
                        break
                    for hdr, payload in reader.feed(data):
                        with self._lock:
                            self._frames[hdr["type"]] = (hdr, payload)
            except Exception as e:
                self.error = str(e)[:160]
            finally:
                self._run = False

        threading.Thread(target=run, daemon=True).start()
        return True

    def stop(self):
        self._run = False

    def latest(self, ftype):
        with self._lock:
            return self._frames.get(ftype)


# ---------------- BLE-side session model ----------------

class LinkSession:
    """Phone-side model of the BLE control channel + Wi-Fi feed
    session lifecycle (FW-WIFI-03). Credentials are generated per
    session from a CSPRNG, held in RAM only, and wiped at teardown
    (FW-WIFI-02) — the passphrase itself is never exposed by the
    status API, exactly as the app would treat it."""

    IDLE, REQUESTED, AP_UP, STREAMING, TEARDOWN = (
        "IDLE", "REQUESTED", "AP_UP", "STREAMING", "TEARDOWN")
    BATTERY_GATE_PCT = 20          # FW-WIFI-06
    IDLE_TIMEOUT_S = 15            # FW-WIFI-05

    def __init__(self, source):
        self.source = source
        self.state = self.IDLE
        self.source_kind = None
        self.ssid = None
        self._passphrase = None
        self.started_at = None
        self.last_frame_at = None
        self.last_reason = "no session yet"
        self._lock = threading.Lock()

    def _gen_credentials(self):
        suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits)
                         for _ in range(4))
        self.ssid = "IMP-VISION-" + suffix
        alphabet = string.ascii_letters + string.digits
        self._passphrase = "".join(secrets.choice(alphabet)
                                   for _ in range(20))

    def request(self, source_kind, soc_pct):
        """Start a feed session. Returns (ok, message)."""
        with self._lock:
            if self.state in (self.REQUESTED, self.AP_UP, self.STREAMING):
                return False, "a session is already live"
            if soc_pct is not None and soc_pct < self.BATTERY_GATE_PCT:
                self.last_reason = (f"refused: battery {soc_pct:.0f}% is "
                                    "below the 20% session gate (FW-WIFI-06)")
                return False, self.last_reason
            self.source_kind = source_kind
            if source_kind == "phone":
                # On-device camera: no AP to raise, but the session
                # model (state, logging, teardown) still applies.
                self.state = self.STREAMING
                self.ssid = None
                self._passphrase = None
                self.last_reason = "phone camera is on-device — no Wi-Fi session needed"
            else:
                self.state = self.REQUESTED
                self._gen_credentials()
                self.last_reason = "credentials generated; raising soft-AP"
            self.started_at = time.time()
            self.last_frame_at = time.time()
            return True, self.last_reason

    def advance(self):
        """Walk REQUESTED -> AP_UP -> STREAMING (simulated timing)."""
        with self._lock:
            if self.state == self.REQUESTED:
                self.state = self.AP_UP
                self.last_reason = "soft-AP up; phone joining"
            elif self.state == self.AP_UP:
                self.state = self.STREAMING
                self.last_reason = "streaming"
                if hasattr(self.source, "set_streaming"):
                    self.source.set_streaming(True)

    def note_frame(self):
        with self._lock:
            self.last_frame_at = time.time()

    def check_idle(self):
        with self._lock:
            if (self.state == self.STREAMING and self.source_kind != "phone"
                    and self.last_frame_at
                    and time.time() - self.last_frame_at > self.IDLE_TIMEOUT_S):
                self._teardown_locked("idle timeout — 15 s without frames (FW-WIFI-05)")
                return True
            return False

    def _teardown_locked(self, reason):
        self.state = self.TEARDOWN
        self.ssid = None
        self._passphrase = None          # wiped, per FW-WIFI-02
        self.last_reason = reason
        if hasattr(self.source, "set_streaming"):
            self.source.set_streaming(False)

    def teardown(self, reason="app request"):
        with self._lock:
            if self.state == self.IDLE:
                return
            self._teardown_locked(reason)
            self.state = self.IDLE

    def status(self):
        with self._lock:
            return {"state": self.state, "source": self.source_kind,
                    "ssid": self.ssid,
                    "passphrase": ("held in RAM only (20 chars), wiped at "
                                   "teardown" if self._passphrase else None),
                    "started_at": self.started_at,
                    "reason": self.last_reason}
