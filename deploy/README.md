# NFSHS Online - running and deploying

Everything in this folder is what a server needs. Runtime state
(`history.jsonl`, `hit_counter.txt`) is kept in `data/` (or the Docker
volume), so updating the code never loses it.

## Local testing (Windows, macOS, or Linux)

```
python run.py                 # Linux/macOS: python3 run.py
```

Opens on http://127.0.0.1:8123 (listening on all interfaces). Builds its
own virtual environment on first run, no other setup. Options:
`run.py 127.0.0.1`, `run.py 9000`, `run.py 0.0.0.0:9000`, `--reload`.
Defaults are `DEFAULT_HOST`/`DEFAULT_PORT` at the top of `run.py`.

<details>
<summary><strong>Never run a Python project before? Full step-by-step here.</strong></summary>

No coding experience needed - just follow these in order.

### 1. Install Python (skip if `python3 --version` already shows 3.10+)

- **Windows**: go to [python.org/downloads](https://www.python.org/downloads/),
  download the latest version, and run the installer. On the very
  first install screen, **tick "Add python.exe to PATH"** at the
  bottom before clicking Install - this is the step almost everyone
  misses, and without it Windows won't be able to find `python` later.
- **macOS**: also from [python.org/downloads](https://www.python.org/downloads/)
  (the version macOS ships with is often too old or restricted). Once
  installed, use `python3` instead of `python` in the commands below.
- **Linux**: already installed on almost every distro. If `python3
  --version` doesn't work, Debian/Ubuntu: `sudo apt install python3
  python3-venv`.

### 2. Download this project

No `git` needed:
1. On the [GitHub page for this project](https://github.com/bonkas/nfshs-eaor-webserver),
   click the green **Code** button → **Download ZIP**.
2. Extract the ZIP somewhere easy to find, e.g. your Desktop.

(If you do have `git`: `git clone https://github.com/bonkas/nfshs-eaor-webserver.git`
does the same thing.)

### 3. Open a terminal in the `deploy` folder

- **Windows**: open the extracted folder in File Explorer, go into the
  `deploy` subfolder, click once in the address bar (where the path is
  shown), type `cmd`, and press Enter - a command prompt opens
  already pointed at that folder.
- **macOS**: open the `deploy` subfolder in Finder, right-click inside
  it, and choose **New Terminal at Folder** (if that option is
  missing, open Terminal normally and type `cd ` followed by dragging
  the `deploy` folder onto the Terminal window, then press Enter).
- **Linux**: most file managers have **Open Terminal Here** on
  right-click inside the `deploy` folder; otherwise open a terminal and
  `cd` to wherever you extracted it, then `cd deploy`.

### 4. Run it

In that terminal:
```
python run.py          (Windows)
python3 run.py          (macOS/Linux)
```
The first run prints messages about creating a virtual environment and
installing dependencies - that's normal, only happens once, and can
take a minute or two depending on your internet connection. Every run
after that starts in under a second.

### 5. Test it

Once the terminal shows something like `Serving on
http://127.0.0.1:8123`, open a web browser and go to
**http://127.0.0.1:8123**. You should see the live lobby page. If
nobody happens to be online on the community server right now that's
completely normal (it'll just show an empty lobby) - try `/health` in
the address bar too (`http://127.0.0.1:8123/health`), which should show
`{"status":"ok"}` regardless of whether anyone's online.

### 6. Stop it

Click back into that terminal window and press `Ctrl+C`.

### Troubleshooting

- **`'python' is not recognized...`** (Windows) or **`command not
  found: python`** (macOS/Linux) - Python isn't installed, or wasn't
  added to PATH. Re-run the installer and make sure the PATH checkbox
  is ticked (Windows), or use `python3` instead of `python`
  (macOS/Linux).
- **Something about `ensurepip` or `python3-venv`** (Linux) - run
  `sudo apt install python3-venv` (Debian/Ubuntu) and try again.
- **Port already in use / address already in use** - something else on
  your machine is using port 8123. Run `python run.py 9100` instead,
  then visit `http://127.0.0.1:9100`.
</details>

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
