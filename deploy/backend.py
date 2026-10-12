"""
backend.py - FastAPI backend exposing live NFS: High Stakes Online lobby
data, built on top of nps_client.py.

Privacy note: this API intentionally does NOT expose player IP addresses
or hostnames publicly. The upstream NPS server has zero authentication
and will hand back that data to anyone who asks (see NFSHS Online.md in
Obsidian for the full writeup), but re-publishing it on a public website
is a different matter - players never opted into having their real IP
shown on a public leaderboard. Only username, car, and room/lobby
membership counts are surfaced here.

Run with (Windows or Linux, no manual venv setup needed - see run.py):
    python run.py [HOST|PORT|HOST:PORT]    (python backend.py also works)

Endpoints:
    GET /              - simple auto-refreshing frontend (static HTML/JS)
    GET /api/lobby     - current MAIN lobby: room name, player count, and a
                         privacy-scrubbed list of players (username + car
                         only)
    GET /api/history   - recent join/leave events, timestamped (see
                         "History tracking" section below)
    GET /health        - basic liveness check (does not query the NPS
                         server, just confirms the API process is up)
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

if __name__ == "__main__":
    # Hand off to the launcher before importing FastAPI, so this also
    # works from a plain system Python with no venv active.
    sys.path.insert(0, str(Path(__file__).parent))
    import run
    sys.exit(run.main())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import nps_client as nps

STATIC_DIR = Path(__file__).parent / "static"

# Where runtime state (history.jsonl, hit_counter.txt) is written:
# deploy/data/ by default (local runs and systemd), overridable with
# NFSHS_DATA_DIR - the Docker image sets it to /data, a volume, so the
# files survive container rebuilds. Keeping state in its own folder
# means code updates can replace everything else without losing it.
DATA_DIR = Path(os.environ.get("NFSHS_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# One-time migration: these files used to live next to backend.py.
for _name in ("history.jsonl", "hit_counter.txt"):
    _old, _new = Path(__file__).parent / _name, DATA_DIR / _name
    if _old.exists() and not _new.exists() and _old.resolve() != _new.resolve():
        _old.replace(_new)
        print(f"[backend] moved {_name} into {DATA_DIR}")

app = FastAPI(title="NFSHS Online API", version="0.1.0")

# Serves static/img/* (currently just the NFS: High Stakes title logo,
# extracted from the game's own title screen file for local, non-public
# reference use - see the NFSHS Online.md Obsidian doc for provenance
# and the copyright-scope decision behind keeping asset use minimal).
app.mount("/img", StaticFiles(directory=STATIC_DIR / "img"), name="img")

# CORS: wide open ("*") is intentional here, not a placeholder to
# "tighten later" - this API is read-only, has no auth/cookies/session
# state to protect, and is meant to be embeddable from any site. Nothing
# here changes if a browser page requesting it is malicious.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class PublicPlayer(BaseModel):
    """Privacy-scrubbed player info - no IP, no hostname.

    rank IS included deliberately (unlike IP/hostname) - project
    decision 2026-09-12: showing rank next to a player already visible
    in this public lobby list is treated the same as their public lobby
    presence, since both come from the same unauthenticated query. Not
    used to look up rank for anyone not already in this list.

    car is intentionally omitted - it doesn't mean anything while a
    player is just sitting in MAIN chat (only relevant once in a race
    pit, which isn't queried here - see project doc "Pit/race data
    scoped OUT" section).
    """
    username: str
    rank: int | None = None


class LobbyResponse(BaseModel):
    room: str
    player_count: int | None
    players: list[PublicPlayer]
    queried_at: float  # unix timestamp, so the frontend can show "stale" state


# Simple in-process cache: the NPS query takes several seconds end-to-end
# (multiple round trips + drain() sleeps), so don't hit the game server on
# every single page load - cache briefly and serve stale-but-recent data
# if a request comes in mid-window. Tune CACHE_SECONDS based on how the
# upstream server tolerates polling frequency (see "Next steps" #3 in the
# Obsidian doc - not yet confirmed, being conservative for now).
#
# This cache also acts as basic protection for the upstream community
# game server once this site is public: every visitor within the same
# CACHE_SECONDS window shares one upstream query, regardless of how many
# people are loading the page - so public traffic can't turn into a
# proportional flood against a server we don't control. _cache_lock
# prevents two requests racing to both trigger a real query on a
# simultaneous cache miss.
CACHE_SECONDS = 15
_cache: dict = {"data": None, "fetched_at": 0.0}
_cache_lock = threading.Lock()


# --- Join/leave history tracking ---
# Records a timestamped log of players appearing/disappearing from the
# MAIN roster, so activity that happens while nobody's watching the live
# page (e.g. someone hops on for 5 minutes overnight) is still visible
# afterwards - this is the whole point of a "history" feature, since the
# frontend's join-sound alone only helps if a tab is open and audible at
# the exact moment.
#
# Persisted as an append-only JSONL file (one JSON object per line) next
# to backend.py, auto-created at runtime (not something to upload) - same
# pattern as hit_counter.txt. Kept in memory too for fast reads; capped
# at MAX_HISTORY_EVENTS and the file is rewritten (not just appended)
# whenever a trim happens, so this never grows unbounded on a
# long-running server.
#
# Only ties into REAL upstream queries (a cache miss in _fetch_lobby),
# never into cache-hit responses - this means history events are recorded
# at most once per CACHE_SECONDS regardless of how many people are
# viewing the site, so adding this feature does not increase load on the
# upstream NPS server at all.
HISTORY_FILE = DATA_DIR / "history.jsonl"
MAX_HISTORY_EVENTS = 500
_history: list[dict] = []
_history_lock = threading.Lock()
# None = not yet initialized (server just started, or history file was
# empty/missing) - the first real query after startup only seeds this set
# silently. Without this guard, every player already online at startup
# would look like a fresh "join" event, which is misleading history.
_last_known_players: set[str] | None = None


def _load_history() -> None:
    global _last_known_players
    if not HISTORY_FILE.exists():
        return
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except OSError:
        return
    events = []
    for line in lines[-MAX_HISTORY_EVENTS:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    _history.extend(events)
    # Reconstruct "who's currently known online" from the last event of
    # each username in the persisted log, so a server restart doesn't
    # cause a burst of false "join" events for players who were already
    # online before the restart and still are after it.
    last_action: dict[str, str] = {}
    for e in events:
        last_action[e["username"]] = e["action"]
    _last_known_players = {u for u, a in last_action.items() if a == "join"}


def _append_history_events(events: list[dict]) -> None:
    if not events:
        return
    with _history_lock:
        _history.extend(events)
        del _history[:-MAX_HISTORY_EVENTS]
        try:
            with HISTORY_FILE.open("a") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
        except OSError as e:
            print(f"[backend] failed to write history.jsonl: {e}")
        # Periodically rewrite the file to match the trimmed in-memory
        # list, so the on-disk file doesn't grow forever on a
        # long-running server even though we only append normally.
        if len(_history) >= MAX_HISTORY_EVENTS:
            try:
                with HISTORY_FILE.open("w") as f:
                    for e in _history:
                        f.write(json.dumps(e) + "\n")
            except OSError as e:
                print(f"[backend] failed to rewrite history.jsonl: {e}")


def _record_history(current_players: list[dict], now: float) -> None:
    """Diff current_players against the last known set and log
    join/leave events. Called once per real upstream query (not per
    cached response) from _fetch_lobby."""
    global _last_known_players
    current = {p["username"] for p in current_players if p.get("username")}
    if _last_known_players is None:
        # First real query since startup - seed silently, no events.
        _last_known_players = current
        return

    events = []
    for username in sorted(current - _last_known_players):
        events.append({"username": username, "action": "join", "timestamp": now})
    for username in sorted(_last_known_players - current):
        events.append({"username": username, "action": "leave", "timestamp": now})

    if events:
        _append_history_events(events)
    _last_known_players = current


_load_history()


class HistoryEvent(BaseModel):
    username: str
    action: str  # "join" or "leave"
    timestamp: float


class HistoryResponse(BaseModel):
    events: list[HistoryEvent]  # newest first


def _fetch_lobby() -> LobbyResponse:
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_SECONDS:
            return _cache["data"]

        try:
            players, rooms = nps.get_online_players()
        except Exception as e:
            # Log the real error server-side only; never leak internal
            # exception text (stack details, internal hostnames/paths) to
            # public callers once this is deployed publicly.
            print(f"[backend] NPS server query failed: {e}")
            raise HTTPException(status_code=502, detail="Upstream game server is unavailable")

        room = rooms[0] if rooms else {"room": "", "count": None}
        public_players = [
            PublicPlayer(username=p["username"], rank=p.get("rank"))
            for p in players
            if p.get("username")
        ]

        _record_history(players, now)

        result = LobbyResponse(
            room=room.get("room", ""),
            player_count=room.get("count"),
            players=public_players,
            queried_at=now,
        )
        _cache["data"] = result
        _cache["fetched_at"] = now
        return result


# --- Optional background polling ---
# Off by default: history is then only recorded while someone is
# polling /api/lobby (see the History tracking notes above). Set
# NFSHS_BACKGROUND_POLL=1 to also query upstream on a timer, so
# join/leave activity is captured even with no viewers. The poller goes
# through _fetch_lobby, so it shares the cache with page loads (no extra
# upstream queries while viewers are active). This is a constant query
# against a community server, hence opt-in. NFSHS_POLL_INTERVAL sets the
# seconds between polls (default 15, floored at CACHE_SECONDS).
# Single-process only: with several workers each would run a poller.
def _background_poll_loop(interval: float) -> None:
    while True:
        try:
            _fetch_lobby()
        except HTTPException:
            pass  # already logged in _fetch_lobby; retry next interval
        except Exception as e:
            print(f"[backend] background poll error: {e}", flush=True)
        time.sleep(interval)


def _start_background_poller() -> None:
    if os.environ.get("NFSHS_BACKGROUND_POLL", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        interval = float(os.environ.get("NFSHS_POLL_INTERVAL", "15"))
    except ValueError:
        interval = 15.0
    interval = max(interval, CACHE_SECONDS)
    threading.Thread(target=_background_poll_loop, args=(interval,),
                     daemon=True, name="lobby-poller").start()
    print(f"[backend] background polling enabled, every {interval:g}s", flush=True)


_start_background_poller()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/lobby", response_model=LobbyResponse)
def get_lobby():
    return _fetch_lobby()


@app.get("/api/history", response_model=HistoryResponse)
def get_history(limit: int = 100):
    """Recent join/leave events, newest first. `limit` caps how many are
    returned (default 100, hard-capped at MAX_HISTORY_EVENTS regardless
    of what's requested) - this only ever reads the in-memory/on-disk
    log, never triggers an upstream NPS query, so it's free to poll as
    often as wanted with zero impact on the game server."""
    capped_limit = max(0, min(limit, MAX_HISTORY_EVENTS))
    with _history_lock:
        recent = _history[-capped_limit:] if capped_limit else []
    events = [HistoryEvent(**e) for e in reversed(recent)]
    return HistoryResponse(events=events)


# --- Retro hit counter ---
# Simple file-backed counter, incremented once per page load (GET /).
# Nostalgic "You are visitor #00001337" touch - not meant to be an
# accurate unique-visitor count (no dedup, no cookies), just a period
# vibe. Stored as plain text so it's easy to inspect/reset by hand.
HIT_COUNTER_FILE = DATA_DIR / "hit_counter.txt"


def _increment_hit_counter() -> int:
    try:
        current = int(HIT_COUNTER_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        current = 0
    current += 1
    HIT_COUNTER_FILE.write_text(str(current))
    return current


@app.get("/api/hits")
def get_hits():
    try:
        current = int(HIT_COUNTER_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        current = 0
    return {"hits": current}


@app.get("/")
def index():
    _increment_hit_counter()
    return FileResponse(STATIC_DIR / "index.html")
