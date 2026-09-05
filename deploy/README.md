# LAN deployment

The FastAPI service serves both the read-only API and `web/` static site. This is simpler than
adding nginx for a LAN-only single process, and keeps the frontend same-origin with its API.

Install or update the unit on homelab-pve:

```bash
systemctl link /root/gauntlet-ecology/deploy/gauntlet-ecology-api.service
systemctl daemon-reload
systemctl enable --now gauntlet-ecology-api.service
```

The unit binds only to `192.168.8.150:8001`; it does not listen on the Tailscale address or a
public interface. Confirm it with:

```bash
systemctl status gauntlet-ecology-api.service
curl http://192.168.8.150:8001/dashboard
```

Postgres remains the existing local Docker service. Start it before the API after a host reboot:

```bash
docker compose -f /root/gauntlet-ecology/docker-compose.dev.yml up -d
```
