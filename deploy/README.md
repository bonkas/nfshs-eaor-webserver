# NFSHS Online - running and deploying

Everything in this folder is what a server needs. Runtime state
(`history.jsonl`, `hit_counter.txt`) is kept in `data/` (or the Docker
volume), so updating the code never loses it.

## Local testing (Windows or Linux)

```
python run.py                 # Linux: python3 run.py
```

Opens on http://127.0.0.1:8123 (listening on all interfaces). Builds its
own virtual environment on first run, no other setup. Options:
`run.py 127.0.0.1`, `run.py 9000`, `run.py 0.0.0.0:9000`, `--reload`.
Defaults are `DEFAULT_HOST`/`DEFAULT_PORT` at the top of `run.py`.

## Live host, option A: Docker (recommended)

Copy this folder to the host, then either:

```
docker compose up -d --build      # build from source (start + every update)
```
or pull the image a GitHub Action already built instead of building
locally:
```
docker compose pull && docker compose up -d
```

Either way: `docker compose logs -f` for logs, `docker compose down` to
stop (the data volume is kept). Host port: `NFSHS_PORT=9000 docker
compose up -d`. State lives in the `nfshs-data` volume. Proxmox: a
small Debian VM with Docker is the simplest host; an LXC works too with
"nesting" enabled.

**Prebuilt image**: every push to `main` that touches `deploy/`
rebuilds and pushes `ghcr.io/bonkas/nfshs-eaor-webserver:latest` (see
`.github/workflows/docker-publish.yml`). The package is private by
default, so a fresh host needs to log in once before the first pull:
```
docker login ghcr.io -u bonkas    # paste a GitHub PAT with read:packages scope
```

**Auto-updating with [Watchtower](https://containrrr.dev/watchtower/)**:
only works against the pulled image (not `--build`), since Watchtower
checks the registry for a new digest on the tag you're running. Add it
alongside this service, e.g. in the same `docker-compose.yml`:
```yaml
  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 300 nfshs-online   # check every 5 min, this container only
```
Then start with `docker compose pull && docker compose up -d` (not
`--build`) so the running container is actually tracking the registry
tag.

## Live host, option B: systemd (no Docker)

Needs Python 3.10+ (Debian/Ubuntu: `sudo apt install python3 python3-venv`).

```
sudo useradd --system --home /opt/nfshs --shell /usr/sbin/nologin nfshs
sudo mkdir -p /opt/nfshs
sudo cp -r ./* /opt/nfshs/
sudo chown -R nfshs: /opt/nfshs
sudo -u nfshs python3 /opt/nfshs/run.py --setup-only     # build venv once
sudo install -m 644 /opt/nfshs/nfshs-online.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nfshs-online
journalctl -u nfshs-online -f                            # logs
```

Update: copy the new files over (leave `data/` alone), `chown` again,
then `sudo systemctl restart nfshs-online`.

## Network

Outbound only: TCP to `18.194.54.106:7777` (lobby) and `:8225` (rank
lookups). Inbound: just the web port (default 8123), normally only from
the reverse proxy (Nginx Proxy Manager), which handles HTTPS.
