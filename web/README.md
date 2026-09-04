# Gauntlet: Ecology web UI

The UI is a static frontend served by the existing FastAPI process. It makes same-origin,
read-only requests to `/leaderboard`, `/generations`, `/generations/{id}`, `/matches`, and
`/matches/{id}`; it never contains sample standings or match data.

## Run on the LAN

Start Postgres and set `DATABASE_URL` as described in the repository root README, then run:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Open `http://<LAN-host-IP>:8000/` from a device on the LAN. The same process also serves the
API and its docs at `/docs`. This command does not create a public route, change DNS, or
configure Cloudflare.

For local-only development, replace `0.0.0.0` with `127.0.0.1`.
