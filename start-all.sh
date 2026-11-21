#!/bin/bash
# Deadbug Local Cloud — idempotent (re)start of the whole stack:
#   llama-server :8080  ->  app :5000  ->  caddy gate :5001  ->  cloudflared tunnel
# Safe to re-run: anything already answering is left alone.
# A tunnel (re)start produces a NEW trycloudflare URL; the current one is
# always written to ~/local-cloud/TUNNEL.txt.
set -u
LC="$HOME/local-cloud"
LLAMA_BIN="$LC/bin/llama-b12359/llama-server"
MODEL="$LC/models/brain/Qwen__Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q4_k_m.gguf"
PY="$HOME/workspace/cadvenv/bin/python"
mkdir -p "$LC/logs"

ok() { curl -s -o /dev/null --max-time 2 "$1"; }
llama_ok() { curl -s --max-time 2 http://127.0.0.1:8080/health | grep -q '"ok"'; }

# 1) llama-server (Brain) — /health returns 503 while the model loads,
# so require the real "ok" status, not just any HTTP response.
if ! llama_ok; then
  nohup "$LLAMA_BIN" -m "$MODEL" -c 1024 -t 2 --port 8080 >> "$LC/logs/llama.log" 2>&1 &
  for i in $(seq 1 60); do llama_ok && break; sleep 2; done
fi

# 2) IMP-VISION app
if ! ok http://127.0.0.1:5000/api/health; then
  (cd "$LC/app" && IMP_VISION_MODELS="$LC/models" nohup "$PY" app.py >> "$LC/logs/app.log" 2>&1 &)
  for i in $(seq 1 15); do ok http://127.0.0.1:5000/api/health && break; sleep 1; done
fi

# 3) caddy gate (401 without credentials = up)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:5001/api/health || echo 000)
if [ "$code" = "000" ]; then
  (cd "$LC" && nohup "$LC/bin/caddy" run --config "$LC/Caddyfile" >> "$LC/logs/caddy.log" 2>&1 &)
  sleep 2
fi

# 4) cloudflared quick tunnel -> gate
if ! pgrep -f "cloudflared tunnel" > /dev/null; then
  : > "$LC/logs/tunnel.log"
  nohup "$LC/bin/cloudflared" tunnel --url http://127.0.0.1:5001 --no-autoupdate > "$LC/logs/tunnel.log" 2>&1 &
  URL=""
  for i in $(seq 1 30); do
    sleep 2
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LC/logs/tunnel.log" | grep -v '^https://api\.trycloudflare\.com$' | tail -1)
    [ -n "$URL" ] && break
  done
  [ -n "$URL" ] && printf '%s\n' "$URL" > "$LC/TUNNEL.txt"
fi

echo "llama : $(curl -s --max-time 3 http://127.0.0.1:8080/health | head -c 80)"
echo "app   : $(curl -s --max-time 3 http://127.0.0.1:5000/api/health | head -c 80)"
echo "gate  : HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:5001/api/health) (401 = gate up, auth required)"
echo "tunnel: $(cat "$LC/TUNNEL.txt" 2>/dev/null || echo 'not captured - see logs/tunnel.log')"
