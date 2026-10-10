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

Copy this folder to the host, then:

```
docker compose up -d --build      # start (and after every update)
docker compose logs -f            # logs
docker compose down                # stop (data volume is kept)
```

Host port: `NFSHS_PORT=9000 docker compose up -d`. State lives in the
`nfshs-data` volume. Proxmox: a small Debian VM with Docker is the
simplest host; an LXC works too with "nesting" enabled.

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
