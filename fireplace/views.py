import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import cloud, service

log = logging.getLogger(__name__)

NOT_CONFIGURED = "Fireplace isn't set up yet: an admin needs to enter the Napoleon account in Settings"


def _error(msg, status=400):
    return JsonResponse({"error": str(msg)}, status=status)


@require_GET
def index(request):
    return render(request, "fireplace/index.html", {})


def _is_guest(request):
    """Owners (and admins) manage the house presets; everyone else is a guest.
    No identity (a request that skipped the hub) counts as a guest."""
    user = getattr(request, "user", None)
    return getattr(request, "bdh_mode", "guest") != "owner" and not getattr(user, "is_staff", False)


@require_GET
def api_state(request):
    service.note_viewer()
    return JsonResponse(service.state(max_age=service.FAST_POLL, guest=_is_guest(request)))


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@require_POST
def api_looks(request):
    """Save the current look of fireplace ``id`` as ``name``, or delete preset ``look``."""
    data = _body(request)
    if data is None:
        return _error("Expected a JSON object")
    guest = _is_guest(request)
    try:
        if data.get("op") == "save":
            saved = service.save_look(data.get("id"), data.get("name"), guest=guest)
        elif data.get("op") == "delete":
            saved = service.delete_look(data.get("look"), guest=guest)
        else:
            return _error("Unknown op")
    except service.Invalid as exc:
        return _error(exc, 400)
    except service.NotFound as exc:
        return _error(exc, 404)
    except service.IsOff as exc:
        return _error(exc, 409)
    return JsonResponse({"looks": saved})


@require_POST
def api_act(request):
    data = _body(request)
    if data is None:
        return _error("Expected a JSON object")
    service.note_viewer()
    try:
        fp = service.act(data.get("id"), data.get("action"), data.get("value"))
    except (service.Invalid, cloud.Invalid) as exc:
        return _error(exc, 400)
    except service.NotFound as exc:
        return _error(exc, 404)
    except service.IsOff as exc:
        return _error(exc, 409)
    except cloud.NotConfigured:
        return _error(NOT_CONFIGURED, 409)
    except cloud.AuthFailed:
        return _error(cloud.AUTH_MESSAGE, 502)
    except cloud.CloudError as exc:
        log.warning("Napoleon cloud error: %s", exc)
        return _error(exc, 502)
    return JsonResponse({"fireplace": fp})
