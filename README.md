# Deadbug Local Cloud

The IMP-VISION app (v0.10.3) and its llama.cpp Brain, hosted on this VM as
always-on services. Trevor's phone is a thin client: it only needs a
browser — no Termux session, no on-phone model downloads, nothing for
Android to kill.

## Layout

```
~/local-cloud/
  app/            IMP-VISION app source (v0.10.3, copied from the glasses goal files)
  bin/
    llama-b12359/ llama.cpp nightly b12359, prebuilt ubuntu-x64 (llama-server verified)
    caddy         v2.11.9 — basic-auth gate
    cloudflared   2026.11.1 — present but UNUSABLE from this VM (see Tunnel status)
  models/brain/Qwen__Qwen2.5-1.5B-Instruct-GGUF/
    qwen2.5-1.5b-instruct-q4_k_m.gguf   1,117,320,736 bytes — matches the HF listing exactly
  logs/           llama.log, app.log, caddy.log, tunnel.log, model-download.log
  Caddyfile       gate config — contains only the bcrypt hash, never the password
  GATE.txt        gate credentials (mode 600) — the only place the password is stored
  TUNNEL.txt      tunnel state (currently: no tunnel possible — see below)
  start-all.sh    idempotent (re)start of the whole stack
```

## The stack

| Layer | Port | What |
|---|---|---|
| llama-server | 127.0.0.1:8080 | Qwen2.5-1.5B-Instruct Q4_K_M, `-c 1024 -t 2` |
| app | 127.0.0.1:5000 | Flask app, `IMP_VISION_MODELS=~/local-cloud/models` |
| caddy gate | :5001 | basic-auth reverse proxy → app. 401 without credentials |

Gate user is `trevor`; the password is in `GATE.txt`.

## Running it

Normal state: **systemd owns llama, app and gate** (installed 2026-11-21,
all `enabled`, `Restart=always`):

```
systemctl status local-cloud-llama local-cloud-app local-cloud-gate
systemctl restart local-cloud-app        # etc.
```

Fallback / recovery: `~/local-cloud/start-all.sh` — starts anything that
isn't answering (llama → app → gate → tunnel attempt), waits for real
health, and prints a status summary. Safe to re-run at any time; it
no-ops against healthy services.

Quick checks:

```
curl -s 127.0.0.1:8080/health                       # {"status":"ok"}
curl -s 127.0.0.1:5000/api/health                   # app_version + llama:true
curl -s -u trevor:<pw from GATE.txt> 127.0.0.1:5001/api/health
```

## Tunnel status — BLOCKED by this VM's egress policy (verified)

There is **no public URL**. cloudflared cannot work from this VM:

- The sandbox's only egress is an authenticated HTTP proxy
  (`hatch-egress-proxy:3128`). Direct connections are intercepted by a
  gateway (198.19.0.1) that answers TLS handshakes with plain HTTP —
  cloudflared's attempt dies with
  `tls: first record does not look like a TLS handshake`.
- Through the proxy, `CONNECT api.trycloudflare.com:443` returns
  `200 Connection Established` — tunnel *creation* would work if
  cloudflared used the proxy (it doesn't; it dials directly).
- But `CONNECT <anything>:7844` **times out**: the proxy only allows
  standard ports, and Cloudflare Tunnel edge traffic is port 7844
  (TCP/http2 or UDP/QUIC). Sandbox DNS also REFUSES the
  `_v2-origintunneld._tcp.argotunnel.com` SRV lookup.

So even with a relay for the creation call, the tunnel could never carry
a byte. The gate is therefore **not** the weak point — there is simply no
inbound path to this VM from the internet. To get Trevor's phone in,
pick one on a host with open egress (a $5 VPS, a home machine, or a
future VM image with 7844 allowed): copy `~/local-cloud/` across, run
`start-all.sh`, and the quick tunnel URL lands in `TUNNEL.txt`. A named
(non-quick) tunnel would additionally need a Cloudflare account.

Until then the stack is fully usable from inside the VM (and was
verified end-to-end through the gate: health + a real Ask answered by
the 1.5B).

## Honest limits

- **Speed:** shared vCPU. Measured on the 1.5B at `-t 2 -c 1024`:
  prompt eval ≈ 11 tok/s, generation ≈ **8 tok/s**. A one-sentence Ask
  took ~6.5 s end-to-end. Patient, not fast.
- **Context:** 1024 tokens (the load-proven ceiling from the phone work;
  raising it is unproven on this CPU/RAM budget).
- **Quick tunnels** (when egress allows): the URL is random and changes
  every time the tunnel restarts. No uptime guarantee from Cloudflare.
- **The gate is basic-auth over plain HTTP on :5001** — it is only safe
  behind an https tunnel (cloudflared terminates TLS). Never expose
  :5001 directly to the internet.

## VM-replacement recovery

`~` (home) persists across VM replacements; `/etc` does not.

Survives: everything in `~/local-cloud/` (binaries, model, app, gate
config + credentials, logs, this file) and `~/workspace/cadvenv`
(the Python the app runs on).

Does not survive: the systemd units in `/etc/systemd/system/`
(`local-cloud-{llama,app,gate}.service`) and any running processes.

Recovery procedure after a replacement:

1. Run `~/local-cloud/start-all.sh` — the stack is back (nohup-managed).
2. Optionally reinstall the systemd units from the copies of their
   definitions in the build report / git, then
   `systemctl daemon-reload && systemctl enable --now local-cloud-llama local-cloud-app local-cloud-gate`
   — and stop the nohup instances first, or the units will fail to bind
   the ports.
3. The tunnel step will fail again on this VM image (see above);
   `TUNNEL.txt` explains why.
