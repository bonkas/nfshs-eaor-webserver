"""
nps_client.py - Minimal EA "Network Play System" (NPS) protocol client
for the community NFS: High Stakes online server (18.194.54.106).

*** RESOLVED 2026-09-12: passive query found, join-based path deprecated ***
get_online_players() now uses query_user_list_passive() (NPS_GET_USER_LIST,
opcode 0x0101 -> 0x0211), a single stateless request/response with NO
identify, NO room-join, and NO footprint in the server's room state -
confirmed live: the real game client shows no phantom entry from repeated
queries. This replaces the old join-based query_lobby() path, which was
paused after confirming it made a synthetic identity visible in the real
lobby UI (see project doc history for the incident). query_lobby() and the
TEMPLATE_LOBBY_JOIN/TEMPLATE_ROOM_JOIN-based join flow are kept in this
file for reference / pit-room investigation only - do not use them for
polling; use get_online_players() (passive) instead.

Reverse-engineered from a live packet capture of the real game client,
cross-referenced against the rustymotors/server open-source NPS
reimplementation (github.com/rustymotors/server) which documents
NPS_GET_USER_LIST (0x101 req -> 0x211 resp) - confirmed against the real
community server: it ignores the request's commId field entirely and
just returns whoever is currently in MAIN, but the request/response
shape and opcodes matched their docs, which is what led us to try it.

--- Protocol notes ---

Framing for every message on every port (login :8225, lobby :7777,
room :7778+):

    [opcode: 2 bytes][total_length: 2 bytes BIG-ENDIAN][body: total_length-4 bytes]

Observed opcodes:
    0x0501 -> C->S  login request
    0x0601 -> S->C  login response (customer_id at body[0:4] BE)
    0x0502 -> C->S  persona/game-login request
    0x0613 -> S->C  ack (sometimes sent before the real reply)
    0x0607 -> S->C  persona response (game_user_id at body[0:4] BE)
    0x0101 -> C->S  NPS_GET_USER_LIST - PASSIVE query, 4-byte commId body
                    (ignored by this server - always returns MAIN's
                    roster). No identify/join needed. THIS IS THE
                    RECOMMENDED QUERY METHOD.
    0x0211 -> S->C  NPS_USER_LIST - response to 0x0101 OR a batch dump of
                    all current room members after a real room-join.
                    Body: commId echo (4B) + userCount (4B) + concatenated
                    user records.
    0x0100 -> C->S  lobby "identify" (join lobby server) - JOIN-BASED,
                    deprecated for polling, see warning above
    0x0120 -> S->C  identify ack/echo
    0x020d -> S->C  channel redirect (channel name, host, port)
    0x0302 -> C->S  empty ack (triggers the channel/room broadcast)
    0x0401 -> S->C  channel metadata broadcast (room name, generic counters)
    0x0106 -> C->S  join/create room request - JOIN-BASED, deprecated
    0x0214 -> S->C  ack (room port confirmation)
    0x0204 -> S->C  full user record for ONE player (broadcast per player)
    0x0219 -> S->C  room roster summary (room name + live user count)
    0x0206 -> S->C  ack

--- IMPORTANT: no login/authentication is required to read lobby data ---

Confirmed live on 2026-09-12: the LOBBY server (:7777) never validates
the identity claimed in the 0x0100 "identify" packet against the login
server (:8225) at all. Connecting straight to :7777 with a self-asserted
game_user_id/username (no prior login-server round trip) still returns
the real, live room roster and player records - verified against an
actual playing session (car selection, real hostname, real IP all came
back correctly for the live game client, which was NEVER told about this
separate query connection).

Practical implication: get_online_players() below does NOT call login()
by default. Reading "who's online" needs no credentials at all. login()
is kept as a separate, optional function in case a future feature (e.g.
actually joining a race, not just observing) turns out to need it.

The identify packet's claimed identity is essentially self-asserted at
the lobby level - fine for read-only queries, but worth knowing this
protocol has no real authentication for the lobby/room-list surface.

--- Login-server authentication caveat (only matters if you use login()) ---

The login packet (0x0501) IS credential-validated by the login server
specifically - it's not plaintext "here's my name". Swapping just the
username bytes in an otherwise-valid captured packet gets rejected with
opcode 0x0602 (auth failure). The non-username bytes in the packet are
likely a hash/checksum tied to the account (password-derived or
session-derived - not yet understood). For now, login() works by
REPLAYING a real, previously captured login packet for one account. To
support a dedicated account here, capture a fresh login packet (Wireshark
filtered on the server IP) from a real login with that account and drop
the hex into TEMPLATE_LOGIN below.
"""

import socket
import struct
import sys
import time

HOST = "18.194.54.106"
LOGIN_PORT = 8225
LOBBY_PORT = 7777

# --- Captured templates (see docstring above) ---
# These are raw NPS packets captured from a real, authenticated client
# session. They live in local-dev/local_secrets.py (gitignored, and
# physically outside deploy/ entirely) since they contain a working
# login-replay packet plus a real username/hostname/local IP. See
# local-dev/local_secrets.py's own docstring for how to regenerate them
# from a fresh capture if that file is missing. This import is wrapped
# in a path-insert + try/except specifically so this file (nps_client.py)
# can be copied into deploy/ alone with zero local-dev/ dependency -
# get_online_players() (the recommended, passive, deployed path) never
# needs these at all; only login() and the deprecated join-based query
# functions below do.
import os as _os
_LOCAL_DEV_DIR = _os.path.join(_os.path.dirname(__file__), "..", "local-dev")
if _os.path.isdir(_LOCAL_DEV_DIR):
    sys.path.insert(0, _LOCAL_DEV_DIR)
try:
    from local_secrets import (
        TEMPLATE_LOGIN,
        TEMPLATE_PERSONA,
        TEMPLATE_LOBBY_JOIN,
        TEMPLATE_ROOM_JOIN,
    )
except ImportError:
    TEMPLATE_LOGIN = TEMPLATE_PERSONA = TEMPLATE_LOBBY_JOIN = TEMPLATE_ROOM_JOIN = None

# --- Rank query (0x0522) ---
# Captured live on 2026-09-12 (pcap captures/session3_twoplayers.pcapng,
# frame 16) - this is the periodic heartbeat/rank check the real game
# client sends to the LOGIN server (:8225, NOT the lobby port). Confirmed
# unauthenticated: works with no prior login() call, and works for ANY
# game_user_id, not just the querying account's own id (tested with both
# bonkas=1085 and BruShane=1093 in the same session, got back different,
# correct-looking rank values for each). This is intentional per project
# scope decision 2026-09-12: rank is only ever looked up for players
# already visible in the public MAIN lobby list (see get_online_players),
# never used to enumerate arbitrary/unknown ids.
RANK_QUERY_BODY_TEMPLATE = bytes.fromhex(
    "000000004e46532048696768205374616b6573000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000070ea000a0001"
    "ca1200000000"
)
# The above has a placeholder game_user_id (all zero) at offset 0:4 and
# the literal game-name string "NFS High Stakes" starting at offset 4 -
# build_rank_query() patches in the real game_user_id at query time.


def build_rank_query(game_user_id: int) -> bytes:
    body = bytearray(RANK_QUERY_BODY_TEMPLATE)
    body[0:4] = struct.pack(">I", game_user_id)
    return make_packet(b"\x05\x22", bytes(body))


def query_rank(game_user_id: int):
    """Look up a player's rank via the LOGIN server (opcode 0x0522).

    Unauthenticated, works for any game_user_id (see note above on why
    this is scoped to only be used for players already visible in the
    public MAIN lobby). Returns an int rank, or None if the response
    doesn't look like a valid rank reply.
    """
    sock = socket.create_connection((HOST, LOGIN_PORT), timeout=5)
    try:
        sock.sendall(build_rank_query(game_user_id))
        opcode, resp = recv_packet(sock, timeout=3.0)
    finally:
        sock.close()

    if opcode != b"\x05\x22" or not resp or len(resp) < 1:
        return None
    # Rank is the single trailing byte of the response (confirmed live:
    # bonkas's real rank 20 = 0x14, BruShane's 28 = 0x1c both matched
    # this exact position across repeated queries).
    return resp[-1]

EMPTY_ACK = bytes.fromhex("03020004")

# --- Synthetic query identity ---
# Used by query_lobby()/get_online_players() so our own polling connection
# never masquerades as a real player. See "IMPORTANT" note above: the
# lobby server accepts ANY self-asserted identity, so we use an obviously
# non-human one and filter it out of results by game_user_id. Chosen to be
# unmistakably not a real NFS4 account (those are small sequential ints
# starting around 1000).
QUERY_GAME_USER_ID = 999999999
QUERY_USERNAME = "_nfshs_query_bot"
QUERY_HOSTNAME = "query-server"
QUERY_LOCAL_IP = "0.0.0.0"
QUERY_CLIENT_VERSION = "4.1.0.0"


class NPSError(Exception):
    pass


# ---------------------------------------------------------------------
# Packet building helpers
# ---------------------------------------------------------------------

def pstr(s: str) -> bytes:
    """Encode a Pascal-style NPS string: 4-byte BE length + bytes incl NUL."""
    raw = s.encode("ascii") + b"\x00"
    return struct.pack(">I", len(raw)) + raw


def make_packet(opcode: bytes, body: bytes) -> bytes:
    total_len = len(body) + 4
    return opcode + struct.pack(">H", total_len) + body


def build_lobby_identify(game_user_id: int, username: str, hostname: str,
                          local_ip: str = QUERY_LOCAL_IP,
                          client_version: str = QUERY_CLIENT_VERSION) -> bytes:
    """Build a 0x0100 lobby "identify" packet for an arbitrary identity.
    Matches the shape of TEMPLATE_LOBBY_JOIN below, built fresh instead of
    replaying captured bytes - lets get_online_players() use a synthetic
    identity instead of impersonating whichever real account was captured."""
    body = struct.pack(">I", game_user_id)
    body += pstr(username)
    body += pstr(hostname)
    body += b"\x00" * 32
    body += pstr(local_ip)
    body += pstr(client_version)
    return make_packet(b"\x01\x00", body)


# ---------------------------------------------------------------------
# Low-level framing helpers
# ---------------------------------------------------------------------

def recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise NPSError("connection closed while reading")
        data += chunk
    return data


def recv_packet(sock: socket.socket, timeout: float = 5.0):
    """Read one [opcode(2)][len(2 BE)][body] frame. Returns (opcode, body)."""
    header = recv_exact(sock, 4, timeout=timeout)
    opcode = header[0:2]
    (total_len,) = struct.unpack(">H", header[2:4])
    body_len = total_len - 4
    body = recv_exact(sock, body_len, timeout=timeout) if body_len > 0 else b""
    return opcode, body


def try_recv(sock: socket.socket, timeout: float = 1.0):
    """Like recv_packet but returns (None, None) on timeout or a closed
    connection instead of raising."""
    try:
        return recv_packet(sock, timeout=timeout)
    except (socket.timeout, NPSError, OSError):
        return None, None


def drain(sock: socket.socket, seconds: float, collected: list):
    """Collect whatever packets show up for `seconds`, appending to `collected`.
    Stops early (without raising) if the connection is closed by the peer."""
    end = time.time() + seconds
    while time.time() < end:
        opcode, body = try_recv(sock, timeout=1.0)
        if opcode is None:
            # Either a timeout (keep waiting) or the socket was closed.
            # A closed socket returns instantly rather than blocking for
            # `timeout`, so add a small sleep to avoid busy-looping.
            time.sleep(0.05)
            continue
        collected.append((opcode, body))


# ---------------------------------------------------------------------
# Field parsing helpers
# ---------------------------------------------------------------------

def read_pstr(buf: bytes, offset: int):
    """Decode a Pascal-style NPS string: 4-byte BE length + bytes incl NUL.
    Returns (string, next_offset). Returns ("", offset) on failure."""
    if offset + 4 > len(buf):
        return "", offset
    (length,) = struct.unpack(">I", buf[offset:offset + 4])
    offset += 4
    if length == 0 or offset + length > len(buf):
        return "", offset
    raw = buf[offset:offset + length]
    s = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return s, offset + length


def read_cstr(buf: bytes, offset: int, max_len: int = 64):
    """Decode a plain NUL-terminated ASCII string (no length prefix)."""
    end = buf.find(b"\x00", offset, offset + max_len)
    if end == -1:
        end = min(offset + max_len, len(buf))
    raw = buf[offset:end]
    try:
        s = raw.decode("ascii", errors="replace")
    except Exception:
        s = ""
    return s, end + 1


def _parse_user_record_with_end(body: bytes):
    """Like parse_user_record(), but also returns the offset (within
    `body`) where this record's bytes end - i.e. where the next record in
    a concatenated multi-user blob (see query_user_list_passive()) would
    start. Returns (None, None) if no record could be parsed.

    If the IP pstr (the last field we know how to locate) isn't found,
    falls back to treating the rest of `body` as consumed - correct for
    the common single-record case, and a reasonable "give up cleanly"
    behavior for a multi-record blob where a later record is malformed.
    """
    if len(body) < 16:
        return None, None

    for header_len in (4, 12):
        try:
            game_user_id = struct.unpack(">I", body[header_len - 4:header_len])[0]
            if game_user_id == 0:
                # 0 is never a real account id - seen in the wild as a
                # parsing artifact when this offset is wrong (the
                # "real" id then shows up correctly at the other
                # offset). Reject and try the next header_len instead
                # of returning a bogus record.
                continue
            username, off = read_pstr(body, header_len)
            if not username or not username.isprintable() or not (2 <= len(username) <= 32):
                continue
            hostname, off = read_pstr(body, off)
            car, off = read_cstr(body, off)

            # The IP address is the last Pascal-string in the packet; scan
            # forward for a length-prefixed run of printable dotted-decimal
            # bytes rather than hardcoding an offset (field count after the
            # car name varies between packet variants). Where it ends is
            # also where this record ends, confirmed 2026-10-02 against a
            # real 2-player passive-query response: record 0 (bonkas) ran
            # bytes [0:115), record 1 (BruShane) started exactly at 115
            # and ran to the end of the blob - no gap, no overlap.
            ip = ""
            end = len(body)
            for candidate_off in range(off, len(body) - 4):
                length = struct.unpack(">I", body[candidate_off:candidate_off + 4])[0]
                if 4 <= length <= 20 and candidate_off + 4 + length <= len(body):
                    raw = body[candidate_off + 4:candidate_off + 4 + length]
                    text = raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
                    if text.count(".") == 3 and all(p.isdigit() for p in text.split(".")):
                        ip = text
                        end = candidate_off + 4 + length
                        break

            return {
                "game_user_id": game_user_id,
                "username": username,
                "hostname": hostname,
                "car": car,
                "ip": ip,
            }, end
        except (struct.error, UnicodeDecodeError, IndexError):
            continue

    return None, None


def parse_user_record(body: bytes):
    """Parse a user record (opcodes 0x0211 / 0x0204 / 0x0103 have all been
    observed carrying this same underlying record type).

    Two header variants have been observed in the wild:
        A) [0:4] game_user_id directly (0x0204, 0x0103 style)
        B) [0:8] extra header/flags, [8:12] game_user_id (0x0211 style)

    Rather than hardcode which opcode uses which variant (both showed up
    for opcode 0x0211 in different packets during testing - it may depend
    on session state, not opcode), try both offsets and keep whichever
    yields a plausible-looking username (short, printable ASCII).

    Full layout after the game_user_id, for variant used:
        [+0:+4]  game_user_id (u32 BE)
        [+4:..]  pstr username
        [..]     pstr hostname
        [..]     NUL-terminated car name string (may be empty/absent if
                  the player hasn't joined a race room with a car yet)
        [..]     unknown numeric/telemetry fields (looks like client
                  memory addresses/timestamps, not meaningful game state)
        [..]     pstr IP address (near the end)

    Thin wrapper around _parse_user_record_with_end() for callers that
    only care about the fields, not where the record ends in the buffer
    (that's only needed for walking a multi-record blob - see
    query_user_list_passive()).
    """
    record, _end = _parse_user_record_with_end(body)
    return record


# Track index -> display name, in the order nfs4.exe's own track-folder
# table uses (Hills, Germany, Coastal, Park, France, Uk, Snowy, Gt1-3,
# Hometown, Redrock, Atlantic, RockyPas, Country, LostCany, Aquatica,
# Summit, Empire - found in nfs4.exe's .data). Display names come from
# NFS4SilentScanner.asi's folder->name table. The game rejects any pit
# settings blob with a track index > 18 (0x12), matching these 19 entries.
TRACK_NAMES = [
    "Celtic Ruins", "Landstrasse", "Dolphin Cove", "Kindiak Park",
    "Route Adonf", "Durham Road", "Snowy Ridge", "Raceway", "Raceway 2",
    "Raceway 3", "Hometown", "Redrock Ridge", "Atlantica", "Rocky Pass",
    "Country Woods", "Lost Canyons", "Aquatica", "Summit", "Empire City",
]

# Offset of the room's channel-data field, counted from the end of the
# room name in a 0x0219 roster. Plain chat rooms carry ASCII here
# ("2|0|0|0|8|0:UNINITIALIZED ROOM CHANNEL DATA"); race pits carry the
# binary settings blob decoded by decode_pit_settings(). Matches the
# 0x0106 room-create packet, where the same field sits at the same
# relative offset for both the LOBBY and a pit (session4 frames 123/153).
CHANNEL_DATA_OFFSET = 51
PIT_SETTINGS_LEN = 40   # nfs4.exe copies exactly 0x28 bytes (from 0x853260)


def decode_pit_settings(blob: bytes):
    """Decode a race pit's settings blob (the room channel data).

    Layout taken from nfs4.exe's own pack/unpack routines (0x516C00 packs
    from the race-settings globals, the function right after it unpacks
    and validates). Field names follow the same globals
    NFS4SilentScanner.asi reads:
        [0]     format tag - must be 4 (game rejects anything else)
        [1]     flags: bit0 traffic, bit1 mirror, bit2 weather,
                bit3 night, bit4 backward direction, bit6 collision
        [2]     mode (0x7C4BF4, scanner "mode_b"; game requires <= 4)
        [3]     track index (game requires <= 18, see TRACK_NAMES)
        [4]     unknown (0x7C4D38)
        [5]     laps (game requires <= 8)
        [6]     unknown (0x7C4D04)
        [8]     unknown (0x7C4D00; game defaults it to 7)
        [16]    difficulty (0x7C4C9C)

    Returns None if the blob doesn't look like pit settings (e.g. a chat
    room's ASCII channel data) or fails the game's own range checks.
    """
    if len(blob) < 17 or blob[0] != 4:
        return None
    flags, mode, track, laps = blob[1], blob[2], blob[3], blob[5]
    if laps > 8 or track > 18 or mode > 4:
        return None
    return {
        "track_id": track,
        "track": TRACK_NAMES[track],
        "laps": laps,
        "mode_raw": mode,
        "difficulty_raw": blob[16],
        "traffic": bool(flags & 0x01),
        "mirror": bool(flags & 0x02),
        "weather": bool(flags & 0x04),
        "night": bool(flags & 0x08),
        "backward": bool(flags & 0x10),
        "collision": bool(flags & 0x40),
    }


def parse_room_roster(body: bytes):
    """Parse a 0x0219 style room roster summary. Returns dict with room
    name, current user count, max racers, owner, and (for race pits) the
    decoded race settings.

    Layout (name_len is a placeholder for wherever the room name pstr ends):
        [0:4]        channel id (u32 BE) - 0 for MAIN/LOBBY, the pit's
                     own id for pits (e.g. 0x5823, same id echoed in the
                     0x0214 join ack and 0x0109/0x0210 slot packets)
        [4:8]        room name length (u32 BE, NOT NUL-inclusive - the
                     name bytes are followed directly by other fields,
                     unlike the pstr() fields used elsewhere in this
                     protocol)
        [8:8+N]      room name bytes (N = name length above)
        [+0:+4]      unknown (zero in every sample so far)
        [+4:+8]      "owner"/host game_user_id (u32 BE) - matches whoever
                     created the room
        [+8:+12]     unknown (zero in every sample so far)
        [+12:+16]    unknown non-zero flag (seen as 0x21=33 in every
                     sample so far)
        [+16:+20]    unknown (zero in every sample so far)
        [+20:+22]    MAX RACERS (u16 BE) - 0 for chat rooms, 8 for pits
        [+22:+24]    CURRENT USER COUNT (u16 BE) - confirmed solo (1) and
                     2-player (2) in MAIN, and 1 in a solo pit
        [+24:+28]    another count-like field (0x32=50 in every sample)
        [+51:+91]    channel data - see CHANNEL_DATA_OFFSET above

    History: [+20:+24] was originally read as a single u32, which made
    pits look like they had 524289 (0x00080001) users. It's two u16s:
    8 max racers, 1 present.
    """
    if len(body) < 12:
        return None
    (name_len,) = struct.unpack(">I", body[4:8])
    if name_len == 0 or 8 + name_len > len(body):
        return {"room": "", "count": None, "max_players": None,
                "channel_id": None, "owner_id": None, "settings": None}
    name = body[8:8 + name_len].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    off = 8 + name_len

    owner_id = count = max_players = None
    if off + 8 <= len(body):
        owner_id = struct.unpack(">I", body[off + 4:off + 8])[0]
    if off + 24 <= len(body):
        max_players, count = struct.unpack(">HH", body[off + 20:off + 24])

    data_off = off + CHANNEL_DATA_OFFSET
    settings = decode_pit_settings(body[data_off:data_off + PIT_SETTINGS_LEN])

    return {
        "room": name,
        "count": count,
        "max_players": max_players,
        "channel_id": struct.unpack(">I", body[0:4])[0],
        "owner_id": owner_id,
        "settings": settings,
    }


# ---------------------------------------------------------------------
# High-level flow
# ---------------------------------------------------------------------

def login():
    """Perform the login-server handshake using the captured template.
    Returns (customer_id, game_user_id)."""
    sock = socket.create_connection((HOST, LOGIN_PORT), timeout=5)
    try:
        sock.sendall(TEMPLATE_LOGIN)
        opcode, resp = recv_packet(sock)
        if opcode != b"\x06\x01":
            raise NPSError(f"unexpected login response opcode {opcode.hex()}")
        customer_id = struct.unpack(">I", resp[0:4])[0]

        sock.sendall(TEMPLATE_PERSONA)
        opcode, resp = recv_packet(sock)
        if opcode == b"\x06\x13":
            opcode, resp = recv_packet(sock)
        if opcode != b"\x06\x07":
            raise NPSError(f"unexpected persona response opcode {opcode.hex()}")
        game_user_id = struct.unpack(">I", resp[0:4])[0]

        return customer_id, game_user_id
    finally:
        sock.close()


def build_room_join(game_user_id: int) -> bytes:
    """Build a 0x0106 room-join packet for the given identity by patching
    the game_user_id bytes into TEMPLATE_ROOM_JOIN. The rest of that
    template's fields (room name "MAIN", flags, etc.) aren't fully
    understood yet, so we reuse the captured structure and only swap the
    identity - this is enough to make our own polling connection show up
    under a synthetic id instead of impersonating whichever real account
    the template was captured from."""
    old_id_bytes = struct.pack(">I", 1085)  # the original template's account
    new_id_bytes = struct.pack(">I", game_user_id)
    patched = TEMPLATE_ROOM_JOIN.replace(old_id_bytes, new_id_bytes)
    return patched


def query_lobby(game_user_id: int = QUERY_GAME_USER_ID,
                 username: str = QUERY_USERNAME,
                 hostname: str = QUERY_HOSTNAME):
    """DEPRECATED for polling - see module docstring "RESOLVED 2026-09-12".
    Connect to the lobby, join the MAIN room, and collect whatever
    player/room packets come back. Returns a list of (opcode, body).

    Kept for reference / pit-room investigation only. Confirmed live that
    even a synthetic identity shows up as a VISIBLE phantom entry in the
    real game client's lobby UI - do not use this for get_online_players()
    or any scheduled polling. Use query_user_list_passive() instead.
    """
    sock = socket.create_connection((HOST, LOBBY_PORT), timeout=5)
    packets = []
    try:
        sock.sendall(build_lobby_identify(game_user_id, username, hostname))
        drain(sock, 3, packets)          # 0x0120 identify ack, 0x020d redirect

        sock.sendall(EMPTY_ACK)
        drain(sock, 3, packets)          # 0x0401 channel metadata

        sock.sendall(build_room_join(game_user_id))
        drain(sock, 4, packets)          # 0x0211/0x0204 user records, 0x0219 roster
    finally:
        sock.close()
    return packets


def query_user_list_passive():
    """PASSIVE query - the recommended way to read who's in MAIN.

    Sends NPS_GET_USER_LIST (opcode 0x0101) with a single throwaway
    connection: no identify, no room-join, nothing added to the server's
    state. Confirmed live 2026-09-12 that repeated calls create no
    phantom entry in the real game client's lobby UI (unlike
    query_lobby(), which does).

    The request body is a 4-byte "commId" (channel id) field per the
    rustymotors/server open-source NPS reimplementation
    (github.com/rustymotors/server) - but the real community server
    ignores it and always replies with MAIN's current roster regardless
    of what's sent, so we just send 0.

    Returns a list of parsed user dicts (see parse_user_record), possibly
    empty if nobody is in MAIN.
    """
    sock = socket.create_connection((HOST, LOBBY_PORT), timeout=5)
    try:
        body = struct.pack(">I", 0)  # commId - ignored by this server
        sock.sendall(make_packet(b"\x01\x01", body))
        opcode, resp = recv_packet(sock, timeout=3.0)
        if opcode != b"\x02\x11" or resp is None:
            raise NPSError(f"unexpected NPS_GET_USER_LIST response opcode {opcode}")
    finally:
        sock.close()

    user_count = struct.unpack(">I", resp[4:8])[0]
    records_blob = resp[8:]

    players = []
    offset = 0
    for _ in range(user_count):
        # Each user record's own length isn't separately framed here, so
        # we parse from the current offset and let _parse_user_record_with_end
        # tell us how far it read, then continue from there for the next
        # record. Confirmed live 2026-10-02 against a real 2-player
        # response: records are back-to-back with no gap/padding between
        # them. If a record can't be parsed at all, stop rather than risk
        # mis-parsing garbage as a phantom player.
        rec, end = _parse_user_record_with_end(records_blob[offset:])
        if rec is None:
            break
        players.append(rec)
        offset += end

    return players


def get_online_players(with_login: bool = False, with_rank: bool = True):
    """End-to-end: return currently-online MAIN lobby players.

    Uses query_user_list_passive() (see above) - a single stateless
    request/response with no join side effects. This is safe to call on
    a schedule (Discord bot, web backend) - confirmed live 2026-09-12
    that it does not create a visible phantom entry, unlike the older
    join-based query_lobby() path.

    with_login is accepted for backwards compatibility but has no effect
    - no authentication was ever required for this query.

    with_rank (default True): also looks up each visible player's rank
    via query_rank() (also unauthenticated). Scoped deliberately -
    per project decision 2026-09-12, rank is only ever looked up for
    players already visible in this public MAIN lobby list, never used
    to enumerate arbitrary/unknown game_user_ids. Adds one extra network
    round trip per player - set False to skip if that matters for your
    poll interval.
    """
    players = query_user_list_passive()
    if with_rank:
        for p in players:
            try:
                p["rank"] = query_rank(p["game_user_id"])
            except (NPSError, OSError):
                p["rank"] = None
    room = {"room": "MAIN", "count": len(players)}
    return players, [room]


if __name__ == "__main__":
    players, rooms = get_online_players()

    print(f"Rooms seen: {len(rooms)}")
    for r in rooms:
        print(f"  - {r['room']!r} ({r['count']} player(s))")

    print(f"\nPlayers seen: {len(players)}")
    for p in players:
        print(f"  - {p['username']} (id={p['game_user_id']}, host={p['hostname']}, "
              f"car={p['car']}, ip={p['ip']})")
