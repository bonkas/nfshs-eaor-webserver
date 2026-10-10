#!/usr/bin/env python3
"""
run.py - start the NFSHS Online web server. One file, Windows and Linux.

    python run.py                    # defaults below (0.0.0.0:8123)
    python run.py 127.0.0.1          # this machine only
    python run.py 9000               # different port
    python run.py 0.0.0.0:9000       # both
    python run.py --reload           # dev: restart when .py files change

On Linux use python3, or ./run.py once it's executable (chmod +x run.py).
On Windows you can also double-click it.

Only needs a system Python 3.10+. On first run it creates a virtual
environment next to this file, installs requirements.txt into it, then
re-launches itself inside that venv and starts uvicorn. Later runs go
straight to starting the server; requirements are only reinstalled when
requirements.txt changes, and the venv is rebuilt automatically if the
system Python version changes (e.g. a CachyOS upgrade).

Why the venv folder is per-OS (.venv-win32, .venv-linux): this project
lives in a Nextcloud-synced folder, and a venv only works on the OS that
created it. Add ".venv-*" to the Nextcloud client's ignored files list
so each machine's venv stays local.
"""

# --- Defaults: edit these to change what a plain "python run.py" does ---
DEFAULT_HOST = "0.0.0.0"     # all interfaces; "127.0.0.1" = this machine only
DEFAULT_PORT = 8123

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
REQUIREMENTS = DEPLOY_DIR / "requirements.txt"
VENV_DIR = DEPLOY_DIR / f".venv-{sys.platform}"
# Written inside the venv after a successful install; holds the hash of
# the requirements.txt that was installed, so edits trigger a reinstall.
STAMP_FILE = VENV_DIR / "requirements.sha256"
MIN_PYTHON = (3, 10)  # backend.py uses "X | None" type syntax


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def in_our_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def venv_python_version():
    """The X.Y Python version the existing venv was built with, or None."""
    try:
        for line in (VENV_DIR / "pyvenv.cfg").read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("version", "version_info"):
                return ".".join(value.strip().split(".")[:2])
    except OSError:
        pass
    return None


def venv_has_pip() -> bool:
    try:
        subprocess.run([str(venv_python()), "-c", "import pip"], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def missing_venv_module_hint() -> None:
    if os.name == "nt":
        return
    print(f"[run] On Debian/Ubuntu install it first:  "
          f"sudo apt install python3.{sys.version_info[1]}-venv")
    print(f"[run] (if that exact package name doesn't exist, try the "
          f"generic  sudo apt install python3-venv  instead)")


def ensure_venv() -> None:
    # A venv is tied to the exact Python X.Y that built it - rebuild it if
    # the system Python has been upgraded (common on Arch/CachyOS) or if a
    # different machine synced in a venv built by another version.
    current = f"{sys.version_info[0]}.{sys.version_info[1]}"
    built_with = venv_python_version()
    if built_with and built_with != current:
        print(f"[run] {VENV_DIR.name} was built with Python {built_with}, "
              f"this is {current} - rebuilding it ...")
        try:
            shutil.rmtree(VENV_DIR)
        except OSError as e:
            print(f"[run] Could not remove it automatically ({e}).")
            print(f"[run] Delete this folder by hand and run again: {VENV_DIR}")
            sys.exit(1)

    # A venv that exists but has no pip means a previous creation attempt
    # failed partway - typically the python3-venv system package being
    # missing, which makes CPython's own ensurepip step fail. venv.create()
    # does NOT raise for this (it just prints its own warning and leaves a
    # pip-less venv behind), so without this check a broken venv looks
    # "already there" forever and every later run just fails confusingly
    # at the "pip install requirements" step instead. Treat it the same as
    # "doesn't exist yet" so it gets rebuilt - and once the real system
    # dependency is actually installed, this also makes the service
    # self-heal on its next restart instead of needing the venv folder
    # deleted by hand.
    if venv_python().exists() and not venv_has_pip():
        print(f"[run] {VENV_DIR.name} exists but has no pip in it (the venv "
              f"creation step likely failed partway last time) - removing it "
              f"so it can be rebuilt ...")
        try:
            shutil.rmtree(VENV_DIR)
        except OSError as e:
            print(f"[run] Could not remove it automatically ({e}).")
            print(f"[run] Delete this folder by hand and run again: {VENV_DIR}")
            sys.exit(1)

    if not venv_python().exists():
        print(f"[run] Creating virtual environment in {VENV_DIR.name} ...")
        try:
            venv.create(VENV_DIR, with_pip=True)
        except Exception as e:
            print(f"[run] Could not create the venv: {e}")
            missing_venv_module_hint()
            sys.exit(1)
        if not venv_has_pip():
            # venv.create() "succeeded" (no exception) but ensurepip still
            # failed - CPython prints its own explanation above this.
            print(f"[run] The virtual environment was created but has no "
                  f"pip - see the ensurepip error above.")
            missing_venv_module_hint()
            sys.exit(1)

    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    installed = STAMP_FILE.read_text().strip() if STAMP_FILE.exists() else ""
    if wanted != installed:
        print("[run] Installing requirements ...")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install",
                               "--disable-pip-version-check", "-q",
                               "-r", str(REQUIREMENTS)])
        STAMP_FILE.write_text(wanted)


def parse_bind(value: str):
    """Turn "HOST", "PORT" or "HOST:PORT" into (host or None, port or None)."""
    if value.isdigit():
        return None, int(value)
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit() and host.count(":") == 0:
        return host, int(port)
    return value, None


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Start the NFSHS Online web server.",
        epilog="examples:  run.py 0.0.0.0   run.py 9000   run.py 0.0.0.0:9000",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bind", nargs="?", default="",
                   help=f"HOST, PORT or HOST:PORT (default {DEFAULT_HOST}:{DEFAULT_PORT})")
    p.add_argument("--host", help="interface to listen on (same as the HOST part of bind)")
    p.add_argument("--port", type=int, help="port to listen on (same as the PORT part of bind)")
    p.add_argument("--reload", action="store_true",
                   help="restart automatically when .py files change (development)")
    p.add_argument("--setup-only", action="store_true",
                   help="create/update the venv and exit without starting the server")
    args = p.parse_args(argv)

    bind_host, bind_port = parse_bind(args.bind) if args.bind else (None, None)
    args.host = args.host or bind_host or DEFAULT_HOST
    args.port = args.port or bind_port or int(os.environ.get("PORT", DEFAULT_PORT))
    return args


def serve(args) -> None:
    import uvicorn

    everywhere = args.host == "0.0.0.0"
    shown = "127.0.0.1" if everywhere else args.host
    print(f"[run] Serving on http://{shown}:{args.port}"
          f"{'  (listening on all interfaces)' if everywhere else ''}  - Ctrl+C to stop",
          flush=True)
    uvicorn.run(
        "backend:app",
        app_dir=str(DEPLOY_DIR),
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(DEPLOY_DIR)] if args.reload else None,
        # uvicorn only treats an exclude as a directory when it's a real
        # path (a ".venv-*" glob doesn't stop it descending into them).
        reload_excludes=[str(p) for p in DEPLOY_DIR.glob(".venv*")] if args.reload else None,
    )


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)

    if sys.version_info < MIN_PYTHON:
        print(f"[run] Python {'.'.join(map(str, MIN_PYTHON))}+ is required "
              f"(this is {sys.version.split()[0]}).")
        return 1

    if not in_our_venv():
        ensure_venv()
        if args.setup_only:
            print(f"[run] Environment ready: {VENV_DIR.name}")
            return 0
        try:
            return subprocess.call([str(venv_python()), str(Path(__file__).resolve()), *argv])
        except KeyboardInterrupt:
            return 0

    try:
        serve(args)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        print(f"[run] Error: {e}")
        code = 1
    # When double-clicked on Windows the console closes on exit - keep it
    # open on failure so the error can actually be read.
    if code and os.name == "nt" and sys.stdin and sys.stdin.isatty() and not in_our_venv():
        input("Press Enter to close ...")
    sys.exit(code)
