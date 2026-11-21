#!/data/data/com.termux/files/usr/bin/sh
# IMP-VISION launcher v0.1 — run by the home-screen app (or by hand).
# Starts the UI server if it is down, and llama-server with the best
# COMPLETE downloaded Brain model if it is down. Candidate order
# (candidates.py): the Balanced set's Brain first, then the other
# sets', then the rest by size. If a model fails to load, the next
# candidate is tried — a corrupt file no longer kills Ask for the
# night. Partial downloads are never candidates. What is actually
# served is logged to llama.log and serving.txt. Safe to re-run.
cd "$HOME/imp-vision-app" || exit 1
MODELS="${IMP_VISION_MODELS:-$HOME/imp-vision-models}"
LOG="$HOME/imp-vision-app/llama.log"
SERVED="$HOME/imp-vision-app/serving.txt"
TMP="$HOME/imp-vision-app/.candidates.$$"

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$1$2" 2>/dev/null; }
up() { [ "$(code "$1" "$2")" = "200" ]; }

if ! up 5000 /api/health; then
  nohup python app.py > "$HOME/imp-vision-app/app.log" 2>&1 &
fi

if up 8080 /health; then
  exit 0
fi

LB=$(command -v llama-server 2>/dev/null)
[ -z "$LB" ] && LB=$(ls "$HOME"/llama.cpp/build/bin/llama-server 2>/dev/null | head -1)
[ -z "$LB" ] && exit 0

python candidates.py > "$TMP" 2>/dev/null
if [ ! -s "$TMP" ]; then
  # Fallback (candidates.py missing/failed): v0 behaviour — any
  # Brain .gguf on disk, alphabetical. Still better than nothing.
  for M in $(ls "$MODELS"/brain/*/*.gguf 2>/dev/null); do
    case "$M" in *mmproj*) continue ;; esac
    printf 'unknown\t%s\tunverified\n' "$M" >> "$TMP"
  done
fi

while IFS="$(printf '\t')" read -r REPO M ST; do
  [ -n "$M" ] && [ -f "$M" ] || continue
  echo "imp-vision: trying brain $REPO ($M)" >> "$LOG"
  # -c 1024 -t 4 are LOAD-BEARING on the A53 (proven 2026-11-20):
  # llama-server at defaults (full context, 8 threads) dies silently
  # mid-load — killed during allocation, no error, empty serving.txt.
  # With these limits the same model serves. Do not raise them
  # without an on-device proof at the new values.
  nohup "$LB" -m "$M" -c 1024 -t 4 --port 8080 >> "$LOG" 2>&1 &
  PID=$!
  OK=0; i=0
  while [ $i -lt 60 ]; do
    if up 8080 /health; then OK=1; break; fi
    kill -0 "$PID" 2>/dev/null || break
    sleep 1; i=$((i + 1))
  done
  if [ "$OK" = "1" ]; then
    printf '%s\t%s\n' "$REPO" "$M" >> "$SERVED"
    echo "imp-vision: serving brain $REPO ($M)" >> "$LOG"
    break
  fi
  kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null
  echo "imp-vision: $REPO failed to load - next candidate" >> "$LOG"
done < "$TMP"
rm -f "$TMP"
exit 0
