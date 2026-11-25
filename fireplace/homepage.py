"""Homepage status line and the "Fireplaces off" shortcut (moved from Controls)."""
import logging

from bdh_sdk import ALL_MODES, INFO, ON, WARN, Shortcut, Status

from . import cloud, service

logger = logging.getLogger(__name__)

FIREPLACES_OFF = "fireplaces_off"
#: The homepage asks about once a minute; the worker keeps the snapshot this fresh.
STATUS_MAX_AGE = service.SLOW_POLL + 60


def _fireplace_text(fireplaces):
    on = sum(1 for f in fireplaces if f["model"]["power"])
    if on == 0:
        return "Fireplaces off" if len(fireplaces) > 1 else "Fireplace off"
    if len(fireplaces) == 1:
        return "Fireplace on"
    return f"{on} of {len(fireplaces)} fireplaces on"


def status():
    if not cloud.configured():
        return Status("Not set up", WARN)
    data = service.state(max_age=STATUS_MAX_AGE)
    fireplaces = data["fireplaces"]
    if not fireplaces:
        return Status(data["error"], WARN) if data["error"] else None
    anything_on = any(f["model"]["power"] for f in fireplaces)
    return Status(_fireplace_text(fireplaces), ON if anything_on else INFO)


def shortcuts():
    if not cloud.configured():
        return []
    fireplaces = service.state(max_age=STATUS_MAX_AGE)["fireplaces"]
    if not any(f["model"]["power"] for f in fireplaces):
        return []
    return [Shortcut(
        id=FIREPLACES_OFF,
        title="Fireplaces off" if len(fireplaces) > 1 else "Fireplace off",
        icon="fa-solid fa-fire",
        order=50,
        modes=ALL_MODES,
    )]


def run_shortcut(shortcut_id):
    if shortcut_id != FIREPLACES_OFF:
        raise ValueError(shortcut_id)
    data = service.state(max_age=service.FAST_POLL)
    if data["error"] and not data["fireplaces"]:
        raise RuntimeError(data["error"])
    lit = [f["id"] for f in data["fireplaces"] if f["model"]["power"]]
    for dsn in lit:
        service.act(dsn, "power", False)
    if not lit:
        return "Fireplaces are already off"
    return None
