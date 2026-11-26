"""Assistant tools: what the hub's voice/chat assistant can ask or do here.

They mirror the page, which every mode can use: power, heat, flames, ember bed,
top light and presets. Only saving differs by mode, as on the page: owners save
house presets, everyone else saves guest presets (kept GUEST_LOOK_DAYS days).
Answers are short sentences the assistant can read out.
"""
from __future__ import annotations

import logging

from bdh_sdk import ALL_MODES
from bdh_sdk.plugins import OWNER
from bdh_sdk.tools import MAX_ENUM, Tool, ToolError

from . import cloud, service
from .homepage import STATUS_MAX_AGE

log = logging.getLogger(__name__)

GET_STATUS = "get_status"
SET_POWER = "set_power"
SET_HEAT = "set_heat"
SET_FLAMES = "set_flames"
SET_EMBER_BED = "set_ember_bed"
SET_TOP_LIGHT = "set_top_light"
APPLY_PRESET = "apply_preset"
SAVE_PRESET = "save_preset"

NOT_CONFIGURED = "The fireplace isn't set up yet: an admin needs to enter the Napoleon account in Settings."
NO_FIREPLACES = "There are no fireplaces on the Napoleon account."

HEATER = {"off": 0, "low": 1, "high": 2}
HEATER_NAMES = {v: k for k, v in HEATER.items()}
#: The page's colour swatches (fireplace-core.js SWATCHES), by name.
COLOURS = {
    "orange": (255, 90, 30),
    "red": (255, 42, 0),
    "amber": (255, 176, 32),
    "white": (255, 255, 255),
    "blue": (58, 123, 255),
    "cyan": (0, 200, 255),
    "purple": (176, 64, 255),
    "green": (48, 224, 112),
}
FAVOURITE_NAMES = dict(service.FAVOURITES)


def _is_guest(mode) -> bool:
    """Like the page: anything but owner mode (or no mode at all) is a guest."""
    return mode != OWNER


# ── tool list ──

def _fireplace_param(fireplaces):
    names = list(dict.fromkeys(f["name"] for f in fireplaces))
    param = {"type": "string",
             "description": "Which fireplace, by name. Leave it out to mean all of them for "
                            "get_status and turning off; otherwise ask the guest which one."}
    if 0 < len(names) <= MAX_ENUM:
        param["enum"] = names
    return param


def _preset_names(guest: bool) -> list[str]:
    names = [name for _, name in service.FAVOURITES] + [s["name"] for s in service.looks(guest)]
    return list(dict.fromkeys(names))


def _params(props, fp_param, required=()):
    props = dict(props)
    if fp_param is not None:
        props["fireplace"] = fp_param
    return {"type": "object", "properties": props, "required": list(required)}


def tools(mode=None):
    if not cloud.configured():
        return []
    guest = _is_guest(mode)
    data = service.state(max_age=STATUS_MAX_AGE, guest=guest)
    fireplaces = data["fireplaces"]
    if not fireplaces and not data["error"]:
        return []
    # One fireplace needs no name; with none known yet (cloud down) take any name.
    fp = None if len(fireplaces) == 1 else _fireplace_param(fireplaces)
    level = lambda lo, hi, what: {"type": "integer", "minimum": lo, "maximum": hi, "description": what}
    colour = lambda what: {"type": "string", "enum": list(COLOURS), "description": what}
    presets = _preset_names(guest)
    keep = (f"Guests' presets are kept for {service.GUEST_LOOK_DAYS} days." if guest
            else "It's saved as a house preset every guest can use.")
    return [
        Tool(GET_STATUS, "Fireplace status",
             "Whether each fireplace is on, and its heater, set temperature, flame height and "
             "colours, ember bed, top light, preset and eco/boost. Use it to answer questions about "
             "the fireplace before changing anything.",
             params=_params({}, fp), modes=ALL_MODES, readonly=True),
        Tool(SET_POWER, "Fireplace on/off",
             "Turn a fireplace on or off. Turning on lights the flames as they were last set. "
             "Without a fireplace name, off turns off every lit fireplace.",
             params=_params({"on": {"type": "boolean", "description": "true to turn on, false to turn off"}},
                            fp, required=["on"]),
             modes=ALL_MODES),
        Tool(SET_HEAT, "Fireplace heat",
             "Set the fireplace heater (off, low or high), its target room temperature in °C, and eco "
             "or boost mode. Setting the heater to low or high turns the fireplace on; the temperature, "
             "eco and boost need it on already.",
             params=_params({
                 "heater": {"type": "string", "enum": list(HEATER), "description": "Heater fan level"},
                 "temperature": level(service.MIN_T, service.MAX_T,
                                      "Target temperature in whole °C; convert from °F if the guest uses it"),
                 "eco": {"type": "boolean", "description": "Eco mode: the heater cycles to save energy"},
                 "boost": {"type": "boolean", "description": "Boost mode: heat up faster"},
             }, fp),
             modes=ALL_MODES),
        Tool(SET_FLAMES, "Fireplace flames",
             "Change the flames: their height (1 low to 5 high) and how much orange and yellow they have "
             "(0 none to 4 most). Give only what should change. The fireplace must be on.",
             params=_params({
                 "height": level(*service.FLAME_SPEED, "Flame height, 1 (low) to 5 (high)"),
                 "orange": level(*service.FLAME_COLOUR, "Orange flame level, 0 to 4"),
                 "yellow": level(*service.FLAME_COLOUR, "Yellow flame level, 0 to 4"),
             }, fp),
             modes=ALL_MODES),
        Tool(SET_EMBER_BED, "Fireplace ember bed",
             "Light the ember bed under the flames: brightness 1 to 4 (0 turns it off), a colour, or "
             "slow colour cycling. Lighting it turns the fireplace on.",
             params=_params({
                 "brightness": level(0, service.EMBER_MAX, "0 off, 1 dim to 4 brightest"),
                 "colour": colour("Ember bed colour"),
                 "cycle": {"type": "boolean", "description": "Cycle slowly through colours"},
             }, fp),
             modes=ALL_MODES),
        Tool(SET_TOP_LIGHT, "Fireplace top light",
             "Turn the light along the top of the firebox on or off, pick its colour, or make it cycle "
             "through colours. Turning it on turns the fireplace on.",
             params=_params({
                 "on": {"type": "boolean", "description": "true to light it, false for off"},
                 "colour": colour("Top light colour"),
                 "cycle": {"type": "boolean", "description": "Cycle slowly through colours"},
             }, fp),
             modes=ALL_MODES),
        Tool(APPLY_PRESET, "Fireplace preset",
             "Show a preset look: the built-in Party Time, Campfire, Summer Day or Sunset, or a saved "
             "one. It sets the flames, ember bed and top light (not the heater) and turns the fireplace on.",
             params=_params({"preset": {"type": "string", "enum": presets, "description": "Preset name"}},
                            fp, required=["preset"]),
             modes=ALL_MODES),
        Tool(SAVE_PRESET, "Save fireplace preset",
             "Save the fireplace's current flames, ember bed and top light (not the heater) as a named "
             f"preset; the same name replaces it. {keep} The fireplace must be on.",
             params=_params({"name": {"type": "string", "minLength": 1, "maxLength": service.MAX_LOOK_NAME,
                                      "description": "Preset name"}},
                            fp, required=["name"]),
             modes=ALL_MODES),
    ]


# ── speech ──

def _label(f, many: bool) -> str:
    if not many:
        return "the fireplace"
    name = f["name"]
    return f"the {name}" if name.casefold().endswith("fireplace") else f"the {name} fireplace"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _colour_name(rgb) -> str:
    if not rgb:
        return "unknown"
    return min(COLOURS, key=lambda n: sum((a - b) ** 2 for a, b in zip(COLOURS[n], rgb)))


def _ember_text(m) -> str:
    if not m["ember_on"]:
        return "ember bed off"
    look = "colour cycling" if m["ember_cycle"] else _colour_name(m["ember_rgb"])
    return f"ember bed {look}, brightness {m['ember_bri']} of {m['ember_max']}"


def _top_text(m) -> str:
    if m["top_cycle"] and m["power"]:
        return "top light colour cycling"
    if not m["top_on"]:
        return "top light off"
    return f"top light {_colour_name(m['top_rgb'])}"


def _heat_text(m) -> str:
    setpoint = "" if m["setpoint"] is None else f" set to {m['setpoint']}°C"
    text = f"heater {HEATER_NAMES.get(m['heater'], m['heater'])}" + (setpoint if m["heater"] else "")
    if not m["heater"] and setpoint:
        text += f" (thermostat{setpoint})"
    modes = [name for name in ("eco", "boost") if m[name]]
    return text + "".join(f", {name} on" for name in modes)


def _flames_text(m) -> str:
    return f"flame height {m['flame_speed']} of 5, orange {m['orange']} of 4, yellow {m['yellow']} of 4"


def _status_text(f, many: bool) -> str:
    label = _cap(_label(f, many))
    m = f["model"]
    if not f["online"]:
        return f"{label} is offline."
    if not m["power"]:
        return f"{label} is off."
    parts = [_flames_text(m), _heat_text(m), _ember_text(m), _top_text(m)]
    if m["favourite"]:
        parts.append(f"preset {FAVOURITE_NAMES[m['favourite']]}")
    return f"{label} is on: " + "; ".join(parts) + "."


# ── running ──

def _state(mode):
    if not cloud.configured():
        raise ToolError(NOT_CONFIGURED)
    data = service.state(max_age=service.FAST_POLL, guest=_is_guest(mode))
    if not data["fireplaces"]:
        raise ToolError(data["error"] or NO_FIREPLACES)
    return data


def _norm(name):
    key = " ".join(name.split()).casefold().removeprefix("the ")
    for suffix in (" fireplace", " fire"):
        key = key.removesuffix(suffix)
    return key


def _match(fireplaces, wanted):
    # Normalise both sides: a device may itself be named "Living Room Fireplace".
    key = _norm(wanted)
    return next((f for f in fireplaces if _norm(f["name"]) == key), None)


def _targets(fireplaces, wanted, *, all_ok=False):
    if wanted:
        f = _match(fireplaces, wanted)
        if f is None:
            names = " or ".join(f["name"] for f in fireplaces)
            raise ToolError(f"There's no fireplace called {wanted!r}; the fireplaces are {names}.")
        return [f]
    if len(fireplaces) == 1 or all_ok:
        return list(fireplaces)
    raise ToolError("Which fireplace: " + " or ".join(f["name"] for f in fireplaces) + "?")


def _one(data, args):
    (f,) = _targets(data["fireplaces"], args.get("fireplace"))
    if not f["online"]:
        raise ToolError(f"{_cap(_label(f, len(data['fireplaces']) > 1))} is offline right now.")
    return f


def _act(f, many, action, value):
    try:
        return service.act(f["id"], action, value)
    except service.IsOff:
        raise ToolError(f"{_cap(_label(f, many))} is off; turn it on first.") from None
    except (service.Invalid, service.NotFound, cloud.Invalid) as exc:
        raise ToolError(str(exc)) from None
    except cloud.NotConfigured:
        raise ToolError(NOT_CONFIGURED) from None
    except cloud.CloudError as exc:
        log.warning("Napoleon cloud error from the assistant: %s", exc)
        raise ToolError(str(exc)) from None


def _run_steps(f, many, steps):
    """Run ``[(action, value)]`` in order; returns the fireplace's final shape."""
    if not steps:
        raise ToolError("Nothing to change: say what to set.")
    for action, value in steps:
        f = _act(f, many, action, value)
    return f


def _get_status(data, args):
    fireplaces = data["fireplaces"]
    many = len(fireplaces) > 1
    targets = _targets(fireplaces, args.get("fireplace"), all_ok=True)
    text = " ".join(_status_text(f, many) for f in targets)
    if data["error"]:
        text += f" (The last update failed: {data['error']}.)"
    return text


def _set_power(data, args):
    fireplaces = data["fireplaces"]
    many = len(fireplaces) > 1
    on = args["on"]
    if not on and not args.get("fireplace"):
        lit = [f for f in fireplaces if f["model"]["power"]]
        if not lit:
            return "The fireplaces are already off." if many else "The fireplace is already off."
        for f in lit:
            _act(f, many, "power", False)
        if len(lit) == 1:
            return f"{_cap(_label(lit[0], many))} is off now."
        return "Turned off " + " and ".join(f"the {f['name']}" for f in lit) + " fireplaces."
    f = _one(data, args)
    label = _cap(_label(f, many))
    if f["model"]["power"] == on:
        return f"{label} is already {'on' if on else 'off'}."
    _act(f, many, "power", on)
    return f"{label} is {'on' if on else 'off'} now."


def _set_heat(data, args):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    steps = []
    if "heater" in args:
        steps.append(("heater", HEATER[args["heater"]]))
    if "temperature" in args:
        steps.append(("temp", args["temperature"]))
    steps += [(name, args[name]) for name in ("eco", "boost") if name in args]
    m = _run_steps(f, many, steps)["model"]
    eco_boost = ", ".join(f"{name} {'on' if m[name] else 'off'}" for name in ("eco", "boost") if name in args)
    setpoint = "" if m["setpoint"] is None else f", set to {m['setpoint']}°C"
    return (f"{_cap(_label(f, many))}: heater {HEATER_NAMES.get(m['heater'], m['heater'])}{setpoint}"
            + (f", {eco_boost}" if eco_boost else "") + ".")


def _set_flames(data, args):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    steps = [(action, args[key]) for key, action in (("height", "flame_speed"), ("orange", "orange"),
                                                      ("yellow", "yellow")) if key in args]
    m = _run_steps(f, many, steps)["model"]
    return f"{_cap(_label(f, many))}: {_flames_text(m)}."


def _set_ember_bed(data, args):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    steps = []
    if "cycle" in args:
        steps.append(("ember_cycle", args["cycle"]))
    elif "colour" in args and f["model"]["ember_cycle"]:
        steps.append(("ember_cycle", False))  # a colour only sticks once cycling is off
    if "colour" in args:
        steps.append(("ember_rgb", list(COLOURS[args["colour"]])))
    if "brightness" in args:
        steps.append(("ember_bri", args["brightness"]))
    m = _run_steps(f, many, steps)["model"]
    return f"{_cap(_label(f, many))}: {_ember_text(m)}."


def _set_top_light(data, args):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    steps = []
    if "cycle" in args:
        steps.append(("top_cycle", args["cycle"]))
    elif "colour" in args and f["model"]["top_cycle"]:
        steps.append(("top_cycle", False))
    if "colour" in args:
        steps.append(("top_rgb", list(COLOURS[args["colour"]])))
    if "on" in args and not (args["on"] and "colour" in args):
        steps.append(("top_on", args["on"]))
    m = _run_steps(f, many, steps)["model"]
    return f"{_cap(_label(f, many))}: {_top_text(m)}."


def _apply_preset(data, args, mode):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    wanted = " ".join(args["preset"].split()).casefold()
    builtin = next((fid for fid, name in service.FAVOURITES if name.casefold() == wanted), None)
    if builtin:
        _act(f, many, "favourite", builtin)
        name = FAVOURITE_NAMES[builtin]
    else:
        look = next((s for s in service.looks(_is_guest(mode)) if s["name"].casefold() == wanted), None)
        if look is None:
            raise ToolError(f"There's no preset called {args['preset']!r}; the presets are "
                            + ", ".join(_preset_names(_is_guest(mode))) + ".")
        _act(f, many, "look", look["id"])
        name = look["name"]
    return f"{_cap(_label(f, many))} is showing the {name} preset."


def _save_preset(data, args, mode):
    f = _one(data, args)
    many = len(data["fireplaces"]) > 1
    guest = _is_guest(mode)
    try:
        service.save_look(f["id"], args["name"], guest=guest)
    except service.IsOff:
        raise ToolError(f"{_cap(_label(f, many))} is off; turn it on first.") from None
    except (service.Invalid, service.NotFound) as exc:
        raise ToolError(str(exc)) from None
    name = " ".join(args["name"].split())
    kept = f" for {service.GUEST_LOOK_DAYS} days" if guest else ""
    return f"Saved the look of {_label(f, many)} as the {name} preset{kept}."


_RUNNERS = {
    GET_STATUS: _get_status,
    SET_POWER: _set_power,
    SET_HEAT: _set_heat,
    SET_FLAMES: _set_flames,
    SET_EMBER_BED: _set_ember_bed,
    SET_TOP_LIGHT: _set_top_light,
}


def run_tool(tool_id, args, mode=None):
    if tool_id not in _RUNNERS and tool_id not in (APPLY_PRESET, SAVE_PRESET):
        raise ToolError(f"Unknown tool {tool_id!r}")
    data = _state(mode)
    if tool_id == APPLY_PRESET:
        return _apply_preset(data, args, mode)
    if tool_id == SAVE_PRESET:
        return _save_preset(data, args, mode)
    return _RUNNERS[tool_id](data, args)
