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


@require_GET
def api_state(request):
    service.note_viewer()
    return JsonResponse(service.state(max_age=service.FAST_POLL))


@require_POST
def api_act(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
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
