"""IR Source Watch v0 — frame-side emitter-cluster detection.

Implements app-design.md §3 on the phone (the phone does all
thinking, FW-GEN-01): find bright IR emitter clusters in a feed
frame, classify by geometry, label emitter-first — "IR emitter ·
camera-type pattern · 82%" — never a bare "camera found". A camera
with its IR off emits nothing and is invisible to this; the UI
says so, and so does every empty result here.

Method: on a no-IR-cut sensor, 850 nm arrays render as blown-out
saturated clusters. Detection thresholds near-blown-out pixels,
merges them with a small dilation (sensor bloom joins an array's
dots into one cluster), labels connected components, then reads
geometry:

- ring of bright lobes around a darker centre  -> camera-type
- lone filled bright panel (high fill ratio)   -> illuminator
- anything else compact                        -> single-point

A co-timed thermal grid (the Lepton 40x30 preview, FW-THM-01)
cross-checks each cluster: a warm body at the emitter supports
the classification, per §3. Detection runs on whatever source fed
the frame — glasses feed, simulated glasses, or the phone's own
camera. On phone cameras the near-IR response is whatever the
lens filter lets through (best-effort; see the UI caveat).
"""
import io, math, time

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

ANALYSIS_W = 320          # detection runs on a downscale; boxes are
                          # reported normalized, so resolution is free
HOT_MIN = 238             # blown-out threshold (min channel)
MIN_AREA = 7              # analysis-scale pixels
FOV_DEG = 62.0            # phone-camera horizontal FOV assumption;
                          # the glasses lens gets its own constant
                          # when hardware lands


def _dilate(mask, w, h, r):
    """Square dilation by radius r, as two separable passes. Only
    hot pixels stamp, and hot pixels are sparse, so this is cheap.
    Models sensor bloom: the saturated halation around a real
    emitter merges an LED array's separate dots into one cluster,
    which is what the geometry classification reads."""
    tmp = [[False] * w for _ in range(h)]
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if row[x]:
                for xx in range(max(0, x - r), min(w, x + r + 1)):
                    tmp[y][xx] = True
    out = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if tmp[y][x]:
                for yy in range(max(0, y - r), min(h, y + r + 1)):
                    out[yy][x] = True
    return out


def _components(mask, w, h):
    """Two-pass connected-component labeling. Yields (pixels, bbox)."""
    labels = [[0] * w for _ in range(h)]
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nxt = 0
    for y in range(h):
        for x in range(w):
            if not mask[y][x]:
                continue
            left = labels[y][x - 1] if x else 0
            up = labels[y - 1][x] if y else 0
            if left and up and left != up:
                union(left, up)
                labels[y][x] = find(left)
            elif left or up:
                labels[y][x] = find(left or up)
            else:
                nxt += 1
                parent.append(nxt)
                labels[y][x] = nxt
    comps = {}
    for y in range(h):
        for x in range(w):
            lab = labels[y][x]
            if lab:
                comps.setdefault(find(lab), []).append((x, y))
    out = []
    for pts in comps.values():
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append((pts, (min(xs), min(ys), max(xs), max(ys))))
    return out


def _classify(lum, pts, bbox, scale_w, scale_h):
    """Geometry -> (classification, confidence 0..1, features)."""
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    area = len(pts)
    fill = area / float(bw * bh)
    cx = sum(p[0] for p in pts) / area
    cy = sum(p[1] for p in pts) / area
    r_in = 0.22 * min(bw, bh)
    r_out = 0.52 * min(bw, bh)
    inner, ring = [], []
    for yy in range(y0, y1 + 1):
        for xx in range(x0, x1 + 1):
            d = math.hypot(xx - cx, yy - cy)
            if d <= r_in:
                inner.append(lum[yy][xx])
            elif d <= r_out:
                ring.append(lum[yy][xx])
    centre = sum(inner) / len(inner) if inner else 0.0
    annulus = sum(ring) / len(ring) if ring else 0.0
    # bright lobes around the ring: sample 16 angles at ring radius
    lobes, prev = 0, False
    r_mid = 0.38 * min(bw, bh)
    for k in range(16):
        a = 2 * math.pi * k / 16
        sx = int(round(cx + r_mid * math.cos(a)))
        sy = int(round(cy + r_mid * math.sin(a)))
        bright = (0 <= sx < scale_w and 0 <= sy < scale_h
                  and lum[sy][sx] >= HOT_MIN)
        if bright and not prev:
            lobes += 1
        prev = bright
    contrast = annulus - centre
    if contrast > 22 and lobes >= 3:
        conf = 0.58 + min(0.22, contrast / 500.0) + min(0.13, lobes * 0.018)
        cls = "camera-type pattern"
    elif max(bw, bh) / float(scale_w) < 0.045:
        # too small to be a panel, whatever its fill: a remote's
        # lone LED, a status light — a single point of IR
        conf = 0.5 + min(0.2, area / 1200.0)
        cls = "single-point emitter"
    elif fill > 0.55 and centre > 185:
        conf = 0.55 + min(0.3, (fill - 0.55) * 0.6) + min(0.08, area / 9000.0)
        cls = "illuminator pattern"
    else:
        conf = 0.45 + min(0.2, area / 1200.0)
        cls = "single-point emitter"
    feats = {"fill": round(fill, 2), "centre": round(centre),
             "annulus": round(annulus), "lobes": lobes, "area": area}
    return cls, max(0.05, min(0.95, conf)), feats


def detect(jpeg_bytes, thermal_grid=None, thermal_size=(40, 30)):
    """Detect emitter clusters in one JPEG frame.

    thermal_grid: optional list of rows of °C (the 40x30 preview),
    co-timed with the frame. Returns a list of detection dicts with
    normalized bbox/centroid, classification, confidence, and the
    emitter-first label. Empty list means no emitters in view —
    which, per §3, says nothing about cameras with their IR off.
    """
    if not HAVE_PIL:
        return [], "detection unavailable — Pillow is not installed"
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.load()
    except Exception as e:
        return [], f"frame did not decode ({str(e)[:80]})"
    rgb = img.convert("RGB")
    W, H = rgb.size
    scale = min(1.0, ANALYSIS_W / float(W))
    aw, ah = max(1, int(W * scale)), max(1, int(H * scale))
    small = rgb.resize((aw, ah))
    px = small.load()
    lum = [[0] * aw for _ in range(ah)]
    mask = [[False] * aw for _ in range(ah)]
    for y in range(ah):
        for x in range(aw):
            r, g, b = px[x, y]
            lum[y][x] = (r + g + b) // 3
            mask[y][x] = min(r, g, b) >= HOT_MIN
    ambient = None
    if thermal_grid:
        flat = [v for row in thermal_grid for v in row]
        ambient = sum(flat) / len(flat)
    mask = _dilate(mask, aw, ah, 3)
    out = []
    for pts, bbox in _components(mask, aw, ah):
        if len(pts) < MIN_AREA:
            continue
        cls, conf, feats = _classify(lum, pts, bbox, aw, ah)
        x0, y0, x1, y1 = bbox
        cx = sum(p[0] for p in pts) / len(pts) / aw
        cy = sum(p[1] for p in pts) / len(pts) / ah
        note = "no thermal frame for cross-check"
        if thermal_grid:
            tw, th = thermal_size
            gx = min(tw - 1, max(0, int(cx * tw)))
            gy = min(th - 1, max(0, int(cy * th)))
            cell = [thermal_grid[yy][xx]
                    for yy in range(max(0, gy - 1), min(th, gy + 2))
                    for xx in range(max(0, gx - 1), min(tw, gx + 2))]
            warm = sum(cell) / len(cell)
            if warm > ambient + 2.5:
                conf = min(0.97, conf + 0.05)
                note = (f"thermal cross-check: warm body at the emitter "
                        f"({warm:.0f}°C vs {ambient:.0f}°C ambient)")
            else:
                conf = max(0.05, conf - 0.03)
                note = "thermal cross-check: no matching heat at the emitter"
        det = {"classification": cls,
               "confidence": round(conf, 2),
               "label": f"IR emitter · {cls} · {round(conf * 100)}%",
               "bbox": {"x": round(x0 / aw, 3), "y": round(y0 / ah, 3),
                        "w": round((x1 - x0 + 1) / aw, 3),
                        "h": round((y1 - y0 + 1) / ah, 3)},
               "centroid": {"x": round(cx, 3), "y": round(cy, 3)},
               "thermal_note": note, "features": feats}
        out.append(det)
    out.sort(key=lambda d: -d["confidence"])
    return out, None


class DetectionTracker:
    """Keeps detections stable across frames: a cluster that stays
    in view keeps its id and first-seen time; new clusters are
    returned separately so the caller can log them once."""

    EXPIRE_S = 10.0
    MATCH_DIST = 0.09

    def __init__(self):
        self.active = {}       # id -> detection dict (enriched)
        self._n = 0

    def update(self, detections, source, heading_deg, snapshot=None,
               fov_deg=FOV_DEG):
        now = time.time()
        fresh = []
        matched = set()
        for det in detections:
            best, best_d = None, 1e9
            for did, cur in self.active.items():
                if did in matched or cur["classification"] != det["classification"]:
                    continue
                d = math.hypot(cur["centroid"]["x"] - det["centroid"]["x"],
                               cur["centroid"]["y"] - det["centroid"]["y"])
                if d < best_d:
                    best, best_d = did, d
            bearing = None
            if heading_deg is not None:
                bearing = round((heading_deg
                                 + (det["centroid"]["x"] - 0.5) * fov_deg) % 360, 1)
            if best is not None and best_d <= self.MATCH_DIST:
                cur = self.active[best]
                cur.update(det)
                cur["last_seen"] = now
                cur["bearing_deg"] = bearing
                cur["source"] = source
                matched.add(best)
            else:
                self._n += 1
                did = f"IR-{self._n}"
                det = dict(det)
                det.update({"id": did, "source": source,
                            "bearing_deg": bearing,
                            "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                        time.localtime(now)),
                            "first_seen_ts": now, "last_seen": now,
                            "snapshot": snapshot})
                self.active[did] = det
                matched.add(did)
                fresh.append(det)
        stale = [did for did, cur in self.active.items()
                 if now - cur["last_seen"] > self.EXPIRE_S]
        for did in stale:
            del self.active[did]
        return [self.active[k] for k in sorted(self.active)], fresh

    def public(self):
        out = []
        for k in sorted(self.active):
            d = dict(self.active[k])
            d.pop("first_seen_ts", None)
            out.append(d)
        return out
