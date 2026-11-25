"""Background worker: keeps the fireplace snapshot fresh.

Every ``service.FAST_POLL`` seconds while someone has the page open, every
``service.SLOW_POLL`` seconds otherwise (the homepage status still needs to
know whether a fireplace was left on).
"""
import logging
import os
import sys
import threading

from . import cloud, service

log = logging.getLogger(__name__)

#: Let the server finish starting before the first cloud call.
START_DELAY = 10

_wake = threading.Event()
_started = False
_start_lock = threading.Lock()


def _is_runtime(path):
    """``python -m bdh_sdk.runtime`` (the app's container) runs ``bdh_sdk/runtime/__main__.py``."""
    parts = path.replace("\\", "/").split("/")
    return parts[-3:] == ["bdh_sdk", "runtime", "__main__.py"]


def should_start(argv=None):
    """Start only in the server process (the app's container, Daphne, or runserver's reloaded child)."""
    if os.environ.get("BDH_FIREPLACE_WORKER", "").lower() in ("0", "false", "off", "no"):
        return False
    argv = sys.argv if argv is None else argv
    if argv and _is_runtime(argv[0]):
        return True
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py"):
        if argv[1] == "runserver":
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
        return False
    return False


def tick():
    """Poll if the snapshot is due. Errors are kept in the snapshot, never raised."""
    if not cloud.configured():
        return
    a = service.age()
    if a is not None and a < service.poll_interval() - 1:
        return
    try:
        service.poll()
    except (cloud.CloudError, cloud.NotConfigured) as exc:
        log.info("Fireplace poll failed: %s", exc)
    except Exception:  # noqa: BLE001
        log.exception("Fireplace poll failed")


def _loop():
    _wake.wait(START_DELAY)
    while True:
        _wake.clear()
        tick()
        a = service.age() or 0
        _wake.wait(max(1.0, service.poll_interval() - a))


def poke():
    _wake.set()


def start():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="fireplace-worker", daemon=True).start()
