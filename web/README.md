# Gauntlet: Ecology web UI

The UI is a static frontend served by the existing FastAPI process. It makes same-origin,
read-only requests to the dashboard, leaderboard, generation, evolution, match-replay, and
strategy-source endpoints; it never contains sample standings or match data.

## Run on the LAN

Start Postgres and set `DATABASE_URL` as described in the repository root README, then run:

```bash
uvicorn api.main:app --host 192.168.8.150 --port 8001
```

Open `http://192.168.8.150:8001/` from a device on the LAN. The same process also serves the
API and its docs at `/docs`. This command does not create a public route, change DNS, or
configure Cloudflare.

For local-only development, replace `192.168.8.150` with `127.0.0.1`.
