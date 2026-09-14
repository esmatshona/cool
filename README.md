# ALOO PANEL ULTIMATE v3.1.0

A professional bilingual (FA/EN) VLESS management platform built with FastAPI:
multi-server federation, plans & subscriptions (VLESS/VMess/Clash/Sing-Box),
traffic analytics, live monitoring, Telegram admin + shop bot, notifications,
AI assistant, security center (RBAC + 2FA), audit log, scheduled backups,
diagnostics, maintenance mode, and a plugin system — all dependency-free UI
with dark/light themes.

Full guides in [docs/](docs/README.md): install, config, database,
deployment, servers, telegram, backup, troubleshooting, API, architecture.

A clean, bilingual VLESS management panel built with FastAPI. The interface is fully responsive, supports Persian and English, light and dark themes, user management, traffic reporting, security controls, **Telegram bot integration, API tokens, audit log, backup/restore, bulk operations, Clash/Sing-Box subscriptions**, and Railway deployment.

## Run locally

```bash
pip install -r requirements.txt
python main.py
```

Open `http://localhost:10000` (or set `PANEL_PORT` / `PORT` to change it).

## Environment variables

- `PORT`: public web port — **injected automatically by Railway**, defaults to `8000` locally (nginx listens here)
- `PANEL_PORT`: internal panel port, defaults to `10000`
- `STANNG_DATA_DIR`: persistent data directory — defaults to `./data` locally, `/app/data` in Docker
- `PANEL_NAME`: displayed product name, defaults to `SsPanel`
- `TELEGRAM_CONTACT`: support URL, defaults to `https://t.me/ITSESMAT`
- `XRAY_BIN` / `XRAY_CONFIG_PATH`: override xray binary/config locations (auto-detected otherwise)

## Deploy on Railway (recommended)

1. Push this folder to a GitHub repo (or `railway init` + `railway up` from here).
2. In Railway: **New Project → Deploy from Repo** (it picks up `Dockerfile` + `railway.json` automatically; health check = `/health`).
3. **Add a persistent volume** (critical — otherwise `db.json` is wiped on every redeploy):
   - Service → **Volumes** → **+ New Volume** → mount path: `/app/data`
   - `STANNG_DATA_DIR` already defaults to `/app/data` in Docker, so nothing else to set.
4. Optional variables (Service → **Variables**):
   - `PANEL_NAME` (e.g. `MyPanel`)
   - `TELEGRAM_CONTACT` (e.g. `https://t.me/yourchannel`)
5. Open the Railway-provided `https://<app>.up.railway.app` → `/setup` → create admin → connect your Telegram bot from the **Telegram Bot** menu (paste BotFather token + Chat ID → Save → Send Test).

How it works on Railway: the platform terminates TLS at the edge and routes everything to `$PORT`; `nginx` (see `nginx.conf` / `entrypoint.sh`) proxies `/` to the panel and `/vl-ws`, `/vm-ws`, `/vl-xhttp` to xray. VLESS links use port `443` with `tls`, matching Railway's HTTPS endpoint.

## Deploy notes

- The included `Dockerfile` and `railway.json` are ready for Railway; `render.yaml` targets Render (Docker runtime, health check `/health`).
- Always attach persistent storage for the data directory, and set the environment variables above.
