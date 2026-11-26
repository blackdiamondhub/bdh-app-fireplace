"""Fireplace state and commands.

The worker (and the page, when the worker is late) polls the Napoleon cloud
into a snapshot; the page and the homepage read that snapshot. Every write is
remembered for ``PENDING_S`` seconds so a poll that still carries the old
value (the cloud lags a few seconds) doesn't snap the UI back.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time

from . import cloud
from .cloud import AppError

APP = cloud.APP
log = logging.getLogger(__name__)

#: How long a write overrides what the cloud reports.
PENDING_S = 12.0
#: Poll this often while someone has the page open...
FAST_POLL = 12.0
#: ...and this often otherwise (the homepage status still needs to know).
SLOW_POLL = 300.0
#: The page counts as open this long after its last state request.
VIEWER_WINDOW = 120.0

# Same limits as hass-napoleon (climate 18..23 °C, flame 1..5 / 0..4).
MIN_T, MAX_T, STEP_T = 18, 23, 1
FLAME_SPEED = (1, 5)
FLAME_COLOUR = (0, 4)
#: pynapoleon accepts ember_bed_brightness 0..4.
EMBER_MAX = 4
HEATER_VALUES = (0, 1, 2)
FAVOURITES = (
    ("partytime", "Party Time"),
    ("campfirewarmth", "Campfire"),
    ("summerday", "Summer Day"),
    ("glowingsunset", "Sunset"),
)
FAVOURITE_IDS = tuple(f for f, _ in FAVOURITES)

FLAGS = {
    "eco": ("eco_mode", "set_eco_mode"),
    "boost": ("boost_mode", "set_boost_mode"),
    "ember_cycle": ("ember_bed_cycling", "set_ember_bed_cycling"),
    "top_cycle": ("top_light_cycling", "set_top_light_cycling"),
}
LEVELS = {
    "flame_speed": ("flame_speed", "set_flame_speed", FLAME_SPEED),
    "orange": ("orange_flame", "set_orange_flame", FLAME_COLOUR),
    "yellow": ("yellow_flame", "set_yellow_flame", FLAME_COLOUR),
}
ACTIONS = ("power", "heater", "temp", "ember_bri", "ember_rgb", "top_on", "top_rgb", "favourite", "look",
           *LEVELS, *FLAGS)

#: A saved look: the flame and lights, never the heater. Model key -> FireplaceState field.
LOOK_FIELDS = {
    "flame_speed": "flame_speed",
    "orange": "orange_flame",
    "yellow": "yellow_flame",
    # Cycling first: a colour is only kept once cycling is off.
    "ember_cycle": "ember_bed_cycling",
    "ember_rgb": "ember_bed_rgb",
    "ember_bri": "ember_bed_brightness",
    "top_cycle": "top_light_cycling",
    "top_rgb": "top_light_rgb",
}
LOOK_SETTERS = {k: "set_" + f for k, f in LOOK_FIELDS.items()}
LOOKS_KEY = "looks"  # app_state: [{"id", "name", "look": {...}}]
#: Guests' presets live apart from the house's and go after GUEST_LOOK_DAYS.
GUEST_LOOKS_KEY = "guest_looks"  # app_state: [{"id", "name", "look", "saved": unix seconds}]
GUEST_LOOK_DAYS = 14
MAX_LOOKS = 8
MAX_LOOK_NAME = 24
#: Wall clock for guest preset expiry (cloud.clock is monotonic).
wall = time.time

OFF_MESSAGE = "Turn the fireplace on first"


class Invalid(AppError):
    """A bad request (unknown action, value out of range)."""


class NotFound(AppError):
    """No fireplace with that id on the account."""


class IsOff(AppError):
    """The fireplace is off; it ignores this write until it's on."""


@dataclasses.dataclass
class Entry:
    dsn: str
    name: str
    online: bool
    state: object  # pynapoleon FireplaceState


_lock = threading.RLock()
_poll_lock = threading.Lock()
_entries: dict[str, Entry] = {}
_polled_at: float | None = None  # last attempt, ok or not
_error: str | None = None
_pending: dict[str, dict[str, tuple[object, float]]] = {}
_last_viewer: float | None = None

# Owned by the cloud loop thread: pynapoleon Fireplace objects by dsn.
_fps: dict[str, object] = {}
_fps_gen = -1
_not_fireplaces: set[str] = set()


def reset() -> None:
    global _polled_at, _error, _last_viewer, _fps_gen
    with _lock:
        _entries.clear()
        _pending.clear()
        _polled_at = _error = _last_viewer = None
    _fps.clear()
    _not_fireplaces.clear()
    _fps_gen = -1


# ── viewers / cadence ──

def note_viewer() -> None:
    """The page asked for state: poll fast for the next ``VIEWER_WINDOW`` seconds."""
    global _last_viewer
    idle = not viewer_active()
    _last_viewer = cloud.clock()
    if idle:
        from . import worker
        worker.poke()


def viewer_active() -> bool:
    return _last_viewer is not None and cloud.clock() - _last_viewer < VIEWER_WINDOW


def poll_interval() -> float:
    return FAST_POLL if viewer_active() else SLOW_POLL


def age() -> float | None:
    return None if _polled_at is None else cloud.clock() - _polled_at


# ── polling ──

def _ayla_errors():
    try:
        from ayla_iot_unofficial.exc import AylaAuthExpiringError, AylaNotAuthedError
    except ImportError:  # pragma: no cover
        return ()
    return (AylaAuthExpiringError, AylaNotAuthedError)


async def _connection(client) -> dict[str, str] | None:
    """``{dsn: "Online"|"Offline"...}`` from the Ayla device list (None if unavailable).

    pynapoleon 0.0.4's ``Fireplace.is_online`` is always None, but the device
    list it reads carries each device's ``connection_status``.
    """
    try:
        ayla = client.ayla_api
        lister = ayla.async_list_devices
    except AttributeError:
        return None
    try:
        devices = await lister()
    except _ayla_errors():
        await ayla.async_refresh_auth()
        devices = await lister()
    return {d.get("dsn"): d.get("connection_status") for d in devices if isinstance(d, dict)}


async def _relist(client) -> None:
    global _fps_gen
    fps = await client.fireplaces()
    _fps.clear()
    for fp in fps:
        _fps[fp.dsn] = fp
    _fps_gen = cloud.generation()


async def _fireplace(client, dsn):
    if _fps_gen != cloud.generation() or dsn not in _fps:
        await _relist(client)
    try:
        return _fps[dsn]
    except KeyError:
        raise NotFound("That fireplace isn't on the Napoleon account") from None


def _online(fp, conn) -> bool:
    if getattr(fp, "is_online", None) is not None:
        return bool(fp.is_online)
    if conn is None:
        return True
    status = conn.get(fp.dsn)
    return status is None or str(status).lower() == "online"


async def _poll(client) -> list[Entry]:
    conn = await _connection(client)
    listed = set(conn or ())
    known = set(_fps) | _not_fireplaces
    if _fps_gen != cloud.generation() or not _fps or (conn is not None and listed - known):
        # fireplaces() lists the account and reads every fireplace.
        await _relist(client)
        if conn is not None:
            _not_fireplaces.clear()
            _not_fireplaces.update(listed - set(_fps))
    else:
        for dsn in list(_fps):
            if conn is not None and dsn not in listed:
                del _fps[dsn]  # removed from the account
                continue
            await _fps[dsn].refresh()
    return [Entry(fp.dsn, fp.name, _online(fp, conn), fp.state) for fp in _fps.values()]


def poll() -> None:
    """Read every fireplace from the cloud into the snapshot."""
    global _polled_at, _error
    with _poll_lock:
        try:
            entries = cloud.call(_poll)
        except cloud.AuthFailed:
            _set_error(cloud.AUTH_MESSAGE)
            raise
        except cloud.CloudError as exc:
            _set_error(str(exc))
            raise
        with _lock:
            _entries.clear()
            for e in entries:
                _entries[e.dsn] = e
            _polled_at = cloud.clock()
            _error = None


def _set_error(msg: str) -> None:
    global _polled_at, _error
    with _lock:
        _polled_at = cloud.clock()
        _error = msg


def ensure_fresh(max_age: float) -> None:
    """Poll if the snapshot is older than ``max_age`` (errors land in the snapshot)."""
    a = age()
    if a is not None and a < max_age:
        return
    try:
        poll()
    except (cloud.CloudError, cloud.NotConfigured):
        pass


# ── state shaping ──

def _effective(entry: Entry):
    """The polled state with unexpired pending writes laid over it."""
    now = cloud.clock()
    state = entry.state
    pend = _pending.get(entry.dsn)
    if not pend:
        return state
    over = {}
    for field, (value, until) in list(pend.items()):
        if now > until or getattr(state, field, None) == value:
            del pend[field]
        else:
            over[field] = value
    return dataclasses.replace(state, **over) if over else state


def _rgb(v):
    return list(v) if v else None


def model(state, online: bool = True) -> dict:
    """The card's ``_model()`` shape for one fireplace."""
    power = bool(state.power)
    bri = state.ember_bed_brightness or 0
    top = _rgb(state.top_light_rgb)
    fav = state.current_favourite if state.current_favourite in FAVOURITE_IDS else None
    return {
        "available": bool(online) and state.power is not None,
        "power": power,
        "heater": (state.heater or 0) if power else 0,
        "setpoint": state.setpoint_c,
        "minT": MIN_T,
        "maxT": MAX_T,
        "stepT": STEP_T,
        "flame_speed": state.flame_speed if state.flame_speed is not None else 3,
        "orange": state.orange_flame if state.orange_flame is not None else 2,
        "yellow": state.yellow_flame if state.yellow_flame is not None else 2,
        "ember_on": power and bri > 0,
        "ember_bri": bri if power else 0,
        "ember_max": EMBER_MAX,
        "ember_rgb": _rgb(state.ember_bed_rgb),
        "top_on": bool(power and top and any(top)),
        "top_rgb": top,
        "favourite": fav,
        "favourites": [{"id": f, "name": n} for f, n in FAVOURITES],
        "eco": bool(state.eco_mode),
        "boost": bool(state.boost_mode),
        "ember_cycle": bool(state.ember_bed_cycling),
        "top_cycle": bool(state.top_light_cycling),
    }


def _shape(entry: Entry) -> dict:
    return {"id": entry.dsn, "name": entry.name, "online": entry.online,
            "model": model(_effective(entry), entry.online)}


def fireplaces() -> list[dict]:
    """The snapshot, shaped for the page (no cloud call)."""
    with _lock:
        return [_shape(e) for e in _entries.values()]


def state(max_age: float = FAST_POLL, guest: bool = False) -> dict:
    """``{configured, error, fireplaces, looks}``, polling first if the snapshot is stale."""
    if not cloud.configured():
        return {"configured": False, "error": None, "fireplaces": [], "looks": [], "guest": guest}
    ensure_fresh(max_age)
    with _lock:
        err = _error
    return {"configured": True, "error": err, "fireplaces": fireplaces(), "looks": looks(guest), "guest": guest}


# ── commands ──

def _int(value, lo, hi, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise Invalid(f"{what} must be a whole number")
    value = int(value)
    if not lo <= value <= hi:
        raise Invalid(f"{what} must be {lo}..{hi}")
    return value


def _bool(value, what):
    if not isinstance(value, bool):
        raise Invalid(f"{what} must be true or false")
    return value


def _rgb_value(value, what):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise Invalid(f"{what} must be [r, g, b]")
    return tuple(_int(c, 0, 255, what) for c in value)


def validate(action, value):
    """Check ``value`` for ``action``; returns the normalised value."""
    if action not in ACTIONS:
        raise Invalid(f"Unknown action {action!r}")
    if action in ("power", "top_on") or action in FLAGS:
        return _bool(value, action)
    if action == "heater":
        return _int(value, 0, 2, "heater")
    if action == "temp":
        return _int(value, MIN_T, MAX_T, "Target temperature")
    if action in LEVELS:
        return _int(value, *LEVELS[action][2], action)
    if action == "ember_bri":
        return _int(value, 0, EMBER_MAX, "Ember brightness")
    if action in ("ember_rgb", "top_rgb"):
        return _rgb_value(value, action)
    if action == "favourite":
        if value not in FAVOURITE_IDS:
            raise Invalid("Unknown preset")
        return value
    if action == "look":
        found = next((s["look"] for s in looks(True) if s["id"] == value), None) if isinstance(value, str) else None
        if found is None:
            raise Invalid("That preset was deleted")
        return found
    raise Invalid(f"Unknown action {action!r}")  # pragma: no cover


# ── saved looks (your own presets) ──

def _clean_look(look) -> dict:
    """A stored look, checked field by field; raises Invalid."""
    if not isinstance(look, dict):
        raise Invalid("Bad preset")
    out = {
        "flame_speed": _int(look.get("flame_speed"), *FLAME_SPEED, "flame_speed"),
        "orange": _int(look.get("orange"), *FLAME_COLOUR, "orange"),
        "yellow": _int(look.get("yellow"), *FLAME_COLOUR, "yellow"),
        "ember_cycle": _bool(look.get("ember_cycle"), "ember_cycle"),
        "ember_rgb": None if look.get("ember_rgb") is None else list(_rgb_value(look["ember_rgb"], "ember_rgb")),
        "ember_bri": _int(look.get("ember_bri"), 0, EMBER_MAX, "ember_bri"),
        "top_cycle": _bool(look.get("top_cycle"), "top_cycle"),
        "top_rgb": list(_rgb_value(look.get("top_rgb"), "top_rgb")),
    }
    return out


def _stored(key) -> list[dict]:
    """One folder of saved looks, oldest first. Malformed entries are skipped;
    expired guest entries are skipped and dropped from storage."""
    from bdh_sdk.storage import app_state

    guest = key == GUEST_LOOKS_KEY
    cutoff = wall() - GUEST_LOOK_DAYS * 86400
    raw = app_state(APP).get(key) or []
    out = []
    expired = False
    for item in raw:
        try:
            entry = {"id": str(item["id"]), "name": str(item["name"]), "look": _clean_look(item["look"])}
            if guest:
                entry["saved"] = float(item["saved"])
                if entry["saved"] < cutoff:
                    expired = True
                    continue
        except (Invalid, KeyError, TypeError, ValueError):
            log.warning("Skipping a malformed saved look")
            continue
        out.append(entry)
    if expired:
        app_state(APP).set(key, out)
    return out


def prune_guest_looks() -> None:
    """Drop guest presets older than GUEST_LOOK_DAYS (reading the folder does it)."""
    _stored(GUEST_LOOKS_KEY)


def looks(guest: bool = False) -> list[dict]:
    """The presets a visitor sees, each with ``mine`` (they may replace or delete it).

    The house's presets come first; guests see them too, read-only, followed by
    the guest presets (which show when they go, as ``expires``)."""
    house = [{"id": s["id"], "name": s["name"], "look": s["look"], "mine": not guest} for s in _stored(LOOKS_KEY)]
    if not guest:
        return house
    ttl = GUEST_LOOK_DAYS * 86400
    return house + [{"id": s["id"], "name": s["name"], "look": s["look"], "mine": True,
                     "expires": int(s["saved"] + ttl)} for s in _stored(GUEST_LOOKS_KEY)]


def _look_of(state) -> dict:
    m = model(state)
    look = {k: m[k] for k in LOOK_FIELDS}
    look["top_rgb"] = m["top_rgb"] or [0, 0, 0]
    return _clean_look(look)


def save_look(dsn, name, guest: bool = False) -> list[dict]:
    """Save fireplace ``dsn``'s current flame and lights as ``name`` (same name: replaced).

    Guests save into their own folder, where presets go after GUEST_LOOK_DAYS;
    they can't reuse a house preset's name. Returns ``looks(guest)``."""
    import secrets as pysecrets

    from bdh_sdk.storage import app_state

    if not isinstance(name, str) or not name.strip():
        raise Invalid("Give the preset a name")
    name = " ".join(name.split())
    if len(name) > MAX_LOOK_NAME:
        raise Invalid(f"Keep the name to {MAX_LOOK_NAME} characters")
    if name.casefold() in {n.casefold() for _, n in FAVOURITES}:
        raise Invalid("That name is taken by a built-in preset")
    if not isinstance(dsn, str) or not dsn:
        raise Invalid("Missing fireplace id")
    entry = _entry(dsn)
    with _lock:
        state = _effective(entry)
    if not state.power:
        raise IsOff(OFF_MESSAGE)
    look = _look_of(state)
    key = GUEST_LOOKS_KEY if guest else LOOKS_KEY
    if guest and any(s["name"].casefold() == name.casefold() for s in _stored(LOOKS_KEY)):
        raise Invalid("That name is taken by a house preset")
    saved = _stored(key)
    same = next((s for s in saved if s["name"].casefold() == name.casefold()), None)
    if same:
        same.update(name=name, look=look)
        if guest:
            same["saved"] = wall()
    elif len(saved) >= MAX_LOOKS:
        raise Invalid(f"You can save up to {MAX_LOOKS} presets; delete one first")
    else:
        saved.append({"id": pysecrets.token_hex(4), "name": name, "look": look, **({"saved": wall()} if guest else {})})
    app_state(APP).set(key, saved)
    return looks(guest)


def delete_look(look_id, guest: bool = False) -> list[dict]:
    """Delete one of your presets (guests can only delete guest presets)."""
    from bdh_sdk.storage import app_state

    key = GUEST_LOOKS_KEY if guest else LOOKS_KEY
    saved = _stored(key)
    kept = [s for s in saved if s["id"] != look_id]
    if len(kept) == len(saved):
        raise NotFound("That preset was already deleted")
    app_state(APP).set(key, kept)
    return looks(guest)


def plan(state, action, value) -> list[tuple[str, object, str, object]]:
    """The pynapoleon calls for ``action``: ``[(setter, arg, state_field, new_value)]``.

    Mirrors hass-napoleon: power is a master switch and the fireplace drops
    other writes while it's off, so "turn something on" actions power it on
    first and plain setting changes are refused until it's on.
    """
    on = bool(state.power)
    power_on = [] if on else [("set_power", True, "power", True)]

    def need_on():
        if not on:
            raise IsOff(OFF_MESSAGE)

    if action == "power":
        return [("set_power", value, "power", value)]
    if action == "heater":
        if value == 0:
            if not on or state.heater == 0:
                return []
            return [("set_heater", 0, "heater", 0)]
        steps = list(power_on)
        if state.heater != value:
            steps.append(("set_heater", value, "heater", value))
        return steps
    if action == "temp":
        need_on()
        if state.setpoint_c == value:
            return []
        return [("set_setpoint_c", value, "setpoint_c", value)]
    if action in LEVELS:
        need_on()
        field, setter, _ = LEVELS[action]
        return [(setter, value, field, value)]
    if action in FLAGS:
        need_on()
        field, setter = FLAGS[action]
        return [(setter, value, field, value)]
    if action == "ember_bri":
        if value == 0:
            if not on or not state.ember_bed_brightness:
                return []
            return [("set_ember_bed_brightness", 0, "ember_bed_brightness", 0)]
        return power_on + [("set_ember_bed_brightness", value, "ember_bed_brightness", value)]
    if action == "ember_rgb":
        steps = power_on + [("set_ember_bed_rgb", value, "ember_bed_rgb", value)]
        if not state.ember_bed_brightness:
            # Picking a colour for a dark ember bed lights it (like HA's light.turn_on).
            steps.append(("set_ember_bed_brightness", EMBER_MAX, "ember_bed_brightness", EMBER_MAX))
        return steps
    if action == "top_on":
        lit = bool(state.top_light_rgb and any(state.top_light_rgb))
        if not value:
            if not on or not lit:
                return []
            return [("set_top_light_rgb", (0, 0, 0), "top_light_rgb", (0, 0, 0))]
        if lit:
            return power_on
        return power_on + [("set_top_light_rgb", (255, 255, 255), "top_light_rgb", (255, 255, 255))]
    if action == "top_rgb":
        return power_on + [("set_top_light_rgb", value, "top_light_rgb", value)]
    if action == "favourite":
        return power_on + [("apply_favourite", value, "current_favourite", value)]
    if action == "look":
        # Only what differs, so a look that's already showing costs no cloud calls.
        steps = list(power_on)
        for key, field in LOOK_FIELDS.items():
            want = value[key]
            if want is None:
                continue
            have = getattr(state, field, None)
            if key.endswith("_rgb"):
                want, have = tuple(want), (tuple(have) if have is not None else None)
            if have != want:
                steps.append((LOOK_SETTERS[key], want, field, want))
        return steps
    raise Invalid(f"Unknown action {action!r}")  # pragma: no cover


def _entry(dsn) -> Entry:
    with _lock:
        entry = _entries.get(dsn)
    if entry is None:
        raise NotFound("That fireplace isn't on the Napoleon account")
    return entry


def act(dsn, action, value) -> dict:
    """Run one card action on fireplace ``dsn``; returns its shaped state."""
    value = validate(action, value)
    if not isinstance(dsn, str) or not dsn:
        raise Invalid("Missing fireplace id")
    if not cloud.configured():
        raise cloud.NotConfigured("Napoleon email and password aren't set")
    with _lock:
        known = dsn in _entries
    if not known:
        poll()  # a cloud failure is reported as such, not as "not found"
    entry = _entry(dsn)
    with _lock:
        steps = plan(_effective(entry), action, value)
    if steps:
        async def run(client):
            fp = await _fireplace(client, dsn)
            for setter, arg, _, _ in steps:
                await getattr(fp, setter)(arg)
            try:
                await fp.refresh()
            except Exception as exc:  # noqa: BLE001 - the write went through; pending covers it
                log.info("Refresh after write failed: %s", exc)
            return fp.state

        new_state = cloud.call(run)
        until = cloud.clock() + PENDING_S
        with _lock:
            pend = _pending.setdefault(dsn, {})
            for _, _, field, new in steps:
                pend[field] = (new, until)
            entry = _entries.get(dsn) or entry
            entry.state = new_state
            _entries[dsn] = entry
    with _lock:
        return _shape(entry)


def lit() -> list[dict]:
    return [f for f in fireplaces() if f["model"]["power"]]
