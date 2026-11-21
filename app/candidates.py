#!/usr/bin/env python3
"""Launcher candidates for start.sh — one line per servable Brain
file:  repo<TAB>path<TAB>status

Order: the Balanced set's Brain first, then the other sets' Brains
(in sets.json order), then any other downloaded Brain by size.
Files the app has marked partial (meta.json lists a larger size
than what's on disk) are excluded — a truncated GGUF must never be
served. Stdlib only: start.sh runs this before the app server is
necessarily up, and it must work on a bare Termux python.
"""
import json, os

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.environ.get("IMP_VISION_MODELS",
                        os.path.expanduser("~/imp-vision-models"))


def main():
    try:
        with open(os.path.join(BASE, "sets.json")) as f:
            sets = json.load(f)
    except Exception:
        sets = {}
    order = []
    for name in sets:
        try:
            repo = sets[name]["parts"]["brain"]["repo"]
        except Exception:
            continue
        if repo not in order:
            order.append(repo)
    try:
        with open(os.path.join(MODELS, "meta.json")) as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    brain_dir = os.path.join(MODELS, "brain")
    rows = []
    if os.path.isdir(brain_dir):
        for dirname in os.listdir(brain_dir):
            d = os.path.join(brain_dir, dirname)
            if not os.path.isdir(d):
                continue
            repo = dirname.replace("__", "/")
            expected = (meta.get(repo) or {}).get("files") or {}
            for name in os.listdir(d):
                if (not name.lower().endswith(".gguf")
                        or "mmproj" in name.lower()):
                    continue
                path = os.path.join(d, name)
                size = os.path.getsize(path)
                exp = expected.get(name)
                if exp is not None and size < exp:
                    continue  # partial — never serve
                status = "complete" if exp is not None else "unverified"
                rank = order.index(repo) if repo in order else 99
                rows.append((rank, -size, repo, path, status))
    rows.sort()
    for _rank, _neg, repo, path, status in rows:
        print(f"{repo}\t{path}\t{status}")


if __name__ == "__main__":
    main()
