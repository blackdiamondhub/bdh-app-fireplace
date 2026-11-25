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
ACTIONS = ("power", "heater", "temp", "ember_bri", "ember_rgb", "top_on", "top_rgb", "favourite",
           *LEVELS, *FLAGS)

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


def state(max_age: float = FAST_POLL) -> dict:
    """``{configured, error, fireplaces}``, polling first if the snapshot is stale."""
    if not cloud.configured():
        return {"configured": False, "error": None, "fireplaces": []}
    ensure_fresh(max_age)
    with _lock:
        err = _error
    return {"configured": True, "error": err, "fireplaces": fireplaces()}


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
    raise Invalid(f"Unknown action {action!r}")  # pragma: no cover


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
