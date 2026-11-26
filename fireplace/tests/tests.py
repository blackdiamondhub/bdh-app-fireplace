import dataclasses
import json
import pathlib
import re
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from pynapoleon import NapoleonAuthError
from pynapoleon.models import FireplaceState

from bdh_sdk import INFO, ON, WARN, secrets
from bdh_sdk import settings as sdk_settings
from bdh_sdk.testing import FakeBroker, assert_plugin_importable
from fireplace import cloud, homepage, service, worker

ROOT = pathlib.Path(__file__).resolve().parent.parent

LIVING = "AC000W000000001"
DEN = "AC000W000000002"

SETTERS = {
    "set_power": "power",
    "set_flame_speed": "flame_speed",
    "set_orange_flame": "orange_flame",
    "set_yellow_flame": "yellow_flame",
    "set_heater": "heater",
    "set_setpoint_c": "setpoint_c",
    "set_eco_mode": "eco_mode",
    "set_boost_mode": "boost_mode",
    "set_ember_bed_rgb": "ember_bed_rgb",
    "set_ember_bed_brightness": "ember_bed_brightness",
    "set_ember_bed_cycling": "ember_bed_cycling",
    "set_top_light_rgb": "top_light_rgb",
    "set_top_light_cycling": "top_light_cycling",
    "apply_favourite": "current_favourite",
}


def lit_state(**kw):
    base = dict(power=True, flame_speed=3, orange_flame=2, yellow_flame=2, heater=0, setpoint_c=21,
                eco_mode=False, boost_mode=False, ember_bed_rgb=(255, 90, 30), ember_bed_brightness=3,
                ember_bed_cycling=False, top_light_rgb=(0, 0, 0), top_light_cycling=False,
                current_favourite="campfirewarmth")
    base.update(kw)
    return FireplaceState(**base)


class FakeFireplace:
    """A pynapoleon Fireplace: ``cloud`` is what the Napoleon cloud reports."""

    is_online = None

    def __init__(self, dsn, name, **state):
        self.dsn, self.name = dsn, name
        self.cloud = lit_state(**state)
        self.state = dataclasses.replace(self.cloud)
        self.calls = []
        self.lag = False  # writes don't show up in reads yet
        self.refresh_error = None

    async def refresh(self):
        if self.refresh_error:
            raise self.refresh_error
        self.state = dataclasses.replace(self.cloud)
        return self.state

    def __getattr__(self, name):
        if name not in SETTERS:
            raise AttributeError(name)

        async def setter(value):
            self.calls.append((name, value))
            if not self.lag:
                setattr(self.cloud, SETTERS[name], value)
        return setter


class FakeAyla:
    def __init__(self, client):
        self.client = client
        self.fail_auth = 0

    async def async_list_devices(self):
        if self.fail_auth:
            self.fail_auth -= 1
            raise NapoleonAuthError("token refresh failed")
        return [{"dsn": fp.dsn, "connection_status": self.client.status.get(fp.dsn, "Online")}
                for fp in self.client.fps]

    async def async_refresh_auth(self):
        pass


class FakeClient:
    def __init__(self, fps):
        self.fps = fps
        self.status = {}
        self.lists = 0
        self.closed = False
        self.ayla_api = FakeAyla(self)

    async def fireplaces(self):
        self.lists += 1
        for fp in self.fps:
            await fp.refresh()
        return list(self.fps)

    async def close(self):
        self.closed = True


class Fresh:
    """Signed-in fake account with two fireplaces: the Living Room is on, the Den is off."""

    def setUp(self):
        self.broker = FakeBroker(install=True, app_id="fireplace").start()
        self.addCleanup(self.broker.stop)
        self.broker.settings[("fireplace", "napoleon_email")] = "me@example.com"
        self.broker.secrets[("fireplace", "napoleon_password")] = "pw"
        self.living = FakeFireplace(LIVING, "Living Room")
        self.den = FakeFireplace(DEN, "Den", power=False, top_light_rgb=(255, 255, 255))
        self.client = FakeClient([self.living, self.den])
        self.logins = []
        self.bad_password = False
        self.now = 10_000.0

        async def factory(email, password, europe):
            self.logins.append((email, europe))
            if self.bad_password:
                raise NapoleonAuthError("invalid credentials")
            return self.client

        patch.object(cloud, "client_factory", factory).start()
        patch.object(cloud, "clock", lambda: self.now).start()
        self.addCleanup(patch.stopall)
        cloud.reset()
        service.reset()
        self.addCleanup(service.reset)
        self.addCleanup(cloud.reset)

    def tick(self, seconds):
        self.now += seconds

    def fp(self, dsn=LIVING):
        return next(f for f in service.state(max_age=1e9)["fireplaces"] if f["id"] == dsn)


class CloudTests(Fresh, SimpleTestCase):
    def test_credentials_come_from_app_settings(self):
        self.assertEqual(cloud.credentials(), ("me@example.com", "pw", False))
        self.broker.settings[("fireplace", "napoleon_europe")] = True
        getattr(sdk_settings._store, "clear", lambda: None)()  # the SDK caches settings for 5 s
        self.assertTrue(cloud.credentials()[2])
        service.state()
        self.assertEqual(self.logins, [("me@example.com", True)])

    def test_missing_credentials(self):
        secrets.delete("napoleon_password", app="fireplace")
        self.assertFalse(cloud.configured())
        self.assertEqual(service.state(), {"configured": False, "error": None, "fireplaces": [], "looks": [],
                                           "guest": False})
        with self.assertRaises(cloud.NotConfigured):
            service.act(LIVING, "power", False)
        self.assertEqual(self.logins, [])

    def test_one_client_is_shared(self):
        service.state(max_age=0)
        service.state(max_age=0)
        service.act(LIVING, "flame_speed", 4)
        self.assertEqual(len(self.logins), 1)
        # The second poll only re-reads the fireplaces it already has.
        self.assertEqual(self.client.lists, 1)

    def test_wrong_password_is_latched_and_backs_off(self):
        self.bad_password = True
        data = service.state(max_age=0)
        self.assertEqual(data["error"], cloud.AUTH_MESSAGE)
        self.assertEqual(data["fireplaces"], [])
        attempts = [self.now]
        # Poll every 5 s for an hour: sign-ins back off 60, 120, 240, 480, 900, 900...
        for _ in range(720):
            self.tick(5)
            n = len(self.logins)
            self.assertEqual(service.state(max_age=0)["error"], cloud.AUTH_MESSAGE)
            if len(self.logins) > n:
                attempts.append(self.now)
        gaps = [round(b - a) for a, b in zip(attempts, attempts[1:])]
        self.assertEqual(gaps[:6], [60, 120, 240, 480, 900, 900])
        self.assertTrue(all(g >= 60 for g in gaps))

    def test_fixed_password_signs_in_right_away(self):
        self.bad_password = True
        service.state(max_age=0)
        self.bad_password = False
        secrets.set("napoleon_password", "new", app="fireplace")
        data = service.state(max_age=0)
        self.assertIsNone(data["error"])
        self.assertEqual(len(data["fireplaces"]), 2)

    def test_rejected_session_signs_in_again_but_not_too_often(self):
        service.state(max_age=0)
        self.tick(61)
        self.client.ayla_api.fail_auth = 1
        self.assertIsNone(service.state(max_age=0)["error"])
        self.assertEqual(len(self.logins), 2)
        # Within a minute of the last sign-in: report it, don't sign in again.
        self.tick(5)
        self.client.ayla_api.fail_auth = 1
        self.assertEqual(service.state(max_age=0)["error"], cloud.UNREACHABLE_MESSAGE)
        self.assertEqual(len(self.logins), 2)

    def test_cloud_errors_keep_the_last_snapshot(self):
        service.state(max_age=0)
        self.living.refresh_error = OSError("boom")
        data = service.state(max_age=0)
        self.assertEqual(data["error"], cloud.UNREACHABLE_MESSAGE)
        self.assertEqual(len(data["fireplaces"]), 2)


class StateTests(Fresh, SimpleTestCase):
    def test_state_shape(self):
        data = service.state()
        self.assertEqual(data["configured"], True)
        self.assertIsNone(data["error"])
        self.assertEqual([(f["id"], f["name"], f["online"]) for f in data["fireplaces"]],
                         [(LIVING, "Living Room", True), (DEN, "Den", True)])
        m = data["fireplaces"][0]["model"]
        self.assertEqual(m["available"], True)
        self.assertEqual((m["power"], m["flame_speed"], m["orange"], m["yellow"]), (True, 3, 2, 2))
        self.assertEqual((m["setpoint"], m["minT"], m["maxT"], m["stepT"]), (21, 18, 23, 1))
        self.assertEqual((m["ember_on"], m["ember_bri"], m["ember_max"], m["ember_rgb"]),
                         (True, 3, 4, [255, 90, 30]))
        self.assertEqual((m["top_on"], m["top_rgb"]), (False, [0, 0, 0]))
        self.assertEqual(m["favourite"], "campfirewarmth")
        self.assertEqual([f["id"] for f in m["favourites"]],
                         ["partytime", "campfirewarmth", "summerday", "glowingsunset"])
        json.dumps(data)

    def test_off_fireplace_shows_its_lights_off(self):
        m = self.fp(DEN)["model"]
        self.assertEqual((m["power"], m["heater"], m["ember_on"], m["ember_bri"], m["top_on"]),
                         (False, 0, False, 0, False))

    def test_unknown_favourite_and_missing_values(self):
        m = service.model(FireplaceState(power=True, current_favourite="custom3"))
        self.assertIsNone(m["favourite"])
        self.assertEqual((m["flame_speed"], m["ember_rgb"], m["setpoint"]), (3, None, None))
        self.assertFalse(service.model(FireplaceState())["available"])

    def test_offline_fireplace_is_unavailable(self):
        self.client.status[DEN] = "Offline"
        den = self.fp(DEN)
        self.assertFalse(den["online"])
        self.assertFalse(den["model"]["available"])

    def test_zero_fireplaces(self):
        self.client.fps.clear()
        self.assertEqual(service.state(), {"configured": True, "error": None, "fireplaces": [], "looks": [],
                                           "guest": False})
        self.assertIsNone(homepage.status())
        self.assertEqual(homepage.shortcuts(), [])

    def test_new_fireplace_on_the_account_is_picked_up(self):
        service.state()
        self.client.fps.append(FakeFireplace("AC000W000000003", "Patio"))
        self.tick(30)
        self.assertEqual(len(service.state()["fireplaces"]), 3)
        self.assertEqual(self.client.lists, 2)

    def test_poll_cadence_follows_viewers(self):
        self.assertEqual(service.poll_interval(), service.SLOW_POLL)
        service.note_viewer()
        self.assertEqual(service.poll_interval(), service.FAST_POLL)
        self.tick(service.VIEWER_WINDOW + 1)
        self.assertEqual(service.poll_interval(), service.SLOW_POLL)


class ActionTests(Fresh, SimpleTestCase):
    def setUp(self):
        super().setUp()
        service.state()

    def act(self, action, value, fp=None):
        fp = fp or self.living
        fp.calls.clear()
        service.act(fp.dsn, action, value)
        return fp.calls

    def test_every_action_calls_pynapoleon(self):
        cases = [
            ("heater", 2, [("set_heater", 2)]),
            ("temp", 23, [("set_setpoint_c", 23)]),
            ("flame_speed", 5, [("set_flame_speed", 5)]),
            ("orange", 0, [("set_orange_flame", 0)]),
            ("yellow", 4, [("set_yellow_flame", 4)]),
            ("ember_bri", 1, [("set_ember_bed_brightness", 1)]),
            ("ember_rgb", [10, 20, 30], [("set_ember_bed_rgb", (10, 20, 30))]),
            ("top_rgb", [0, 200, 255], [("set_top_light_rgb", (0, 200, 255))]),
            ("top_on", False, [("set_top_light_rgb", (0, 0, 0))]),
            ("top_on", False, []),
            ("top_on", True, [("set_top_light_rgb", (255, 255, 255))]),
            ("eco", True, [("set_eco_mode", True)]),
            ("boost", True, [("set_boost_mode", True)]),
            ("ember_cycle", True, [("set_ember_bed_cycling", True)]),
            ("top_cycle", True, [("set_top_light_cycling", True)]),
            ("favourite", "summerday", [("apply_favourite", "summerday")]),
            ("power", False, [("set_power", False)]),
            ("power", True, [("set_power", True)]),
        ]
        for action, value, calls in cases:
            with self.subTest(action=action, value=value):
                self.assertEqual(self.act(action, value), calls)

    def test_result_is_the_shaped_fireplace(self):
        fp = service.act(LIVING, "flame_speed", 5)
        self.assertEqual((fp["id"], fp["model"]["flame_speed"]), (LIVING, 5))

    def test_no_op_writes_are_skipped(self):
        self.assertEqual(self.act("temp", 21), [])
        self.assertEqual(self.act("heater", 0), [])
        self.assertEqual(self.act("top_on", False), [])

    def test_turning_something_on_powers_the_fireplace_first(self):
        self.assertEqual(self.act("heater", 1, self.den), [("set_power", True), ("set_heater", 1)])
        self.den.cloud.power = False
        self.tick(service.PENDING_S + 1)
        service.state(max_age=0)
        self.assertEqual(self.act("favourite", "partytime", self.den),
                         [("set_power", True), ("apply_favourite", "partytime")])

    def test_colour_for_a_dark_ember_bed_lights_it(self):
        self.living.cloud.ember_bed_brightness = 0
        self.tick(service.PENDING_S + 1)
        service.state(max_age=0)
        self.assertEqual(self.act("ember_rgb", [1, 2, 3]),
                         [("set_ember_bed_rgb", (1, 2, 3)), ("set_ember_bed_brightness", service.EMBER_MAX)])

    def test_settings_need_the_fireplace_on(self):
        for action, value in (("temp", 22), ("flame_speed", 4), ("eco", True), ("ember_cycle", True)):
            with self.subTest(action=action):
                with self.assertRaises(service.IsOff):
                    service.act(DEN, action, value)
        self.assertEqual(self.den.calls, [])
        # Turning things off on an off fireplace is a no-op, not an error.
        self.assertEqual(self.act("heater", 0, self.den), [])
        self.assertEqual(self.act("ember_bri", 0, self.den), [])
        self.assertEqual(self.act("top_on", False, self.den), [])

    def test_validation(self):
        bad = [
            ("power", "yes"), ("power", 1), ("heater", 3), ("heater", -1), ("temp", 17), ("temp", 24),
            ("temp", 21.5), ("temp", "22"), ("flame_speed", 0), ("flame_speed", 6), ("flame_speed", True),
            ("orange", 5), ("yellow", -1), ("ember_bri", 5), ("ember_rgb", [1, 2]), ("ember_rgb", [0, 0, 256]),
            ("ember_rgb", [0, -1, 0]), ("top_rgb", "red"), ("top_rgb", None), ("favourite", "disco"),
            ("eco", None), ("nope", 1), (None, None),
        ]
        for action, value in bad:
            with self.subTest(action=action, value=value):
                with self.assertRaises(service.Invalid):
                    service.act(LIVING, action, value)
        self.assertEqual(self.living.calls, [])
        with self.assertRaises(service.Invalid):
            service.act("", "power", True)

    def test_unknown_fireplace(self):
        with self.assertRaises(service.NotFound):
            service.act("AC000W999999999", "power", True)

    def test_pending_window_hides_stale_polls(self):
        self.living.lag = True
        self.assertEqual(service.act(LIVING, "flame_speed", 5)["model"]["flame_speed"], 5)
        self.tick(5)
        service.state(max_age=0)
        self.assertEqual(self.fp()["model"]["flame_speed"], 5)
        # The write never landed: after the window the cloud's value wins.
        self.tick(service.PENDING_S)
        service.state(max_age=0)
        self.assertEqual(self.fp()["model"]["flame_speed"], 3)

    def test_pending_clears_once_the_cloud_agrees(self):
        service.act(LIVING, "orange", 4)
        self.assertEqual(self.fp()["model"]["orange"], 4)
        self.living.cloud.orange_flame = 1  # changed on the remote afterwards
        self.tick(1)
        service.state(max_age=0)
        self.assertEqual(self.fp()["model"]["orange"], 1)

    def test_failed_refresh_after_a_write_still_succeeds(self):
        self.living.refresh_error = OSError("boom")
        fp = service.act(LIVING, "flame_speed", 1)
        self.assertEqual(fp["model"]["flame_speed"], 1)

    def test_pynapoleon_value_errors_are_invalid(self):
        async def bad(value):
            raise ValueError("flame speed must be 1..5")
        self.living.set_flame_speed = bad
        with self.assertRaises(cloud.Invalid):
            service.act(LIVING, "flame_speed", 2)


class LookTests(Fresh, SimpleTestCase):
    """Your own presets: the flame and lights, saved and re-applied, never the heater."""

    def setUp(self):
        super().setUp()
        service.state()
        self.living.cloud = lit_state(flame_speed=5, orange_flame=4, yellow_flame=1, heater=2, setpoint_c=23,
                                      eco_mode=True, ember_bed_rgb=(0, 200, 255), ember_bed_brightness=2,
                                      top_light_rgb=(255, 0, 128), top_light_cycling=True)
        service.state(max_age=0)

    def apply(self, name, fp=None):
        fp = fp or self.living
        look = next(s for s in service.looks(guest=True) if s["name"] == name)
        fp.calls.clear()
        service.act(fp.dsn, "look", look["id"])
        return fp.calls

    def test_save_keeps_the_flame_and_lights_but_not_the_heater(self):
        saved = service.save_look(LIVING, "  Cozy   blue ")
        self.assertEqual([s["name"] for s in saved], ["Cozy blue"])
        self.assertEqual(saved[0]["look"], {
            "flame_speed": 5, "orange": 4, "yellow": 1, "ember_cycle": False, "ember_rgb": [0, 200, 255],
            "ember_bri": 2, "top_cycle": True, "top_rgb": [255, 0, 128]})
        self.assertEqual(service.state()["looks"], saved)  # every screen gets them with the state

    def test_applying_sets_only_what_differs_and_never_the_heater(self):
        service.save_look(LIVING, "Cozy")
        self.living.cloud = lit_state(heater=1, setpoint_c=19, flame_speed=5, ember_bed_cycling=True)
        self.tick(service.PENDING_S + 1)
        service.state(max_age=0)
        self.assertEqual(self.apply("Cozy"), [
            ("set_orange_flame", 4), ("set_yellow_flame", 1), ("set_ember_bed_cycling", False),
            ("set_ember_bed_rgb", (0, 200, 255)), ("set_ember_bed_brightness", 2),
            ("set_top_light_cycling", True), ("set_top_light_rgb", (255, 0, 128))])
        m = self.fp()["model"]
        self.assertEqual((m["heater"], m["setpoint"], m["orange"], m["top_rgb"]), (1, 19, 4, [255, 0, 128]))
        self.assertEqual(self.apply("Cozy"), [])  # already showing: no cloud calls

    def test_applying_to_an_off_fireplace_turns_it_on_first(self):
        service.save_look(LIVING, "Cozy")
        calls = self.apply("Cozy", self.den)
        self.assertEqual(calls[0], ("set_power", True))
        self.assertNotIn("set_heater", [c for c, _ in calls])

    def test_a_dark_top_light_is_saved_as_off(self):
        self.living.cloud.top_light_rgb = None
        self.tick(service.PENDING_S + 1)
        service.state(max_age=0)
        self.assertEqual(service.save_look(LIVING, "Dim")[0]["look"]["top_rgb"], [0, 0, 0])

    def test_same_name_replaces_and_there_is_a_limit(self):
        first = service.save_look(LIVING, "Cozy")[0]
        self.living.cloud.flame_speed = 1
        self.tick(service.PENDING_S + 1)
        service.state(max_age=0)
        again = service.save_look(LIVING, "COZY")
        self.assertEqual([(s["id"], s["name"], s["look"]["flame_speed"]) for s in again], [(first["id"], "COZY", 1)])
        for n in range(service.MAX_LOOKS - 1):
            service.save_look(LIVING, f"Look {n}")
        with self.assertRaises(service.Invalid):
            service.save_look(LIVING, "One too many")
        service.save_look(LIVING, "Look 0")  # replacing still works when full

    def test_save_rules(self):
        for name in ("", "   ", None, 5, "x" * (service.MAX_LOOK_NAME + 1), "party time"):
            with self.subTest(name=name), self.assertRaises(service.Invalid):
                service.save_look(LIVING, name)
        with self.assertRaises(service.IsOff):
            service.save_look(DEN, "Off")
        with self.assertRaises(service.NotFound):
            service.save_look("AC000W999999999", "Gone")
        self.assertEqual(service.looks(), [])

    def test_delete(self):
        look = service.save_look(LIVING, "Cozy")[0]
        self.assertEqual(service.delete_look(look["id"]), [])
        with self.assertRaises(service.NotFound):
            service.delete_look(look["id"])
        with self.assertRaises(service.Invalid):
            service.act(LIVING, "look", look["id"])  # deleted on another screen meanwhile

    def test_malformed_stored_looks_are_skipped(self):
        service.save_look(LIVING, "Cozy")
        self.broker.state[("fireplace", service.LOOKS_KEY)].append({"id": "x", "name": "Bad", "look": {"flame_speed": 9}})
        self.assertEqual([s["name"] for s in service.looks()], ["Cozy"])
        with self.assertRaises(service.Invalid):
            service.act(LIVING, "look", "x")

    def test_guests_save_apart_and_see_the_house_presets_read_only(self):
        house = service.save_look(LIVING, "Cozy")[0]
        self.assertTrue(house["mine"])
        saved = service.save_look(LIVING, "Ours", guest=True)
        self.assertEqual([(s["name"], s["mine"]) for s in saved], [("Cozy", False), ("Ours", True)])
        self.assertEqual(saved[1]["expires"], int(service.wall()) + service.GUEST_LOOK_DAYS * 86400)
        self.assertEqual(len(self.broker.state[("fireplace", service.GUEST_LOOKS_KEY)]), 1)
        self.assertEqual([s["name"] for s in service.looks()], ["Cozy"])  # owners don't see them
        self.assertEqual(service.state(guest=True)["looks"], saved)
        self.assertTrue(service.state(guest=True)["guest"])
        with self.assertRaises(service.Invalid):
            service.save_look(LIVING, "cozy", guest=True)  # a house name
        with self.assertRaises(service.NotFound):
            service.delete_look(house["id"], guest=True)  # guests can't delete house presets
        with self.assertRaises(service.NotFound):
            service.delete_look(saved[1]["id"])  # nor owners guest ones (they expire)
        self.assertEqual(self.apply("Ours", self.den)[0], ("set_power", True))  # any screen can use them
        self.assertEqual(service.delete_look(saved[1]["id"], guest=True), service.looks(True)[:1])

    def test_guest_presets_go_after_two_weeks(self):
        now = [1_800_000_000.0]
        with patch.object(service, "wall", lambda: now[0]):
            old = service.save_look(LIVING, "Old", guest=True)[-1]
            now[0] += 86400
            service.save_look(LIVING, "Newer", guest=True)
            now[0] += service.GUEST_LOOK_DAYS * 86400 - 86400 + 1
            self.assertEqual([s["name"] for s in service.looks(True)], ["Newer"])
            with self.assertRaises(service.Invalid):
                service.act(LIVING, "look", old["id"])
            now[0] += 86400
            service.prune_guest_looks()
            self.assertEqual(self.broker.state[("fireplace", service.GUEST_LOOKS_KEY)], [])

    def test_resaving_a_guest_preset_keeps_it_longer(self):
        now = [1_800_000_000.0]
        with patch.object(service, "wall", lambda: now[0]):
            first = service.save_look(LIVING, "Ours", guest=True)[0]
            now[0] += 10 * 86400
            again = service.save_look(LIVING, "Ours", guest=True)[0]
            self.assertEqual((again["id"], again["expires"]), (first["id"], first["expires"] + 10 * 86400))

    def test_the_worker_prunes_guest_presets_hourly(self):
        with patch.object(service, "prune_guest_looks") as prune, patch.object(worker, "_pruned_at", None):
            worker.prune()
            worker.prune()
            self.tick(worker.PRUNE_EVERY)
            worker.prune()
        self.assertEqual(prune.call_count, 2)


class HomepageTests(Fresh, TestCase):
    def test_status(self):
        s = homepage.status()
        self.assertEqual((s.text, s.level), ("1 of 2 fireplaces on", ON))
        self.living.cloud.power = False
        self.tick(service.SLOW_POLL + 61)
        s = homepage.status()
        self.assertEqual((s.text, s.level), ("Fireplaces off", INFO))

    def test_status_for_one_fireplace(self):
        self.client.fps.remove(self.den)
        self.assertEqual(homepage.status().text, "Fireplace on")
        self.living.cloud.power = False
        self.tick(service.SLOW_POLL + 61)
        self.assertEqual(homepage.status().text, "Fireplace off")

    def test_status_when_not_set_up_or_failing(self):
        self.bad_password = True
        self.assertEqual((homepage.status().text, homepage.status().level), (cloud.AUTH_MESSAGE, WARN))
        secrets.delete("napoleon_password", app="fireplace")
        self.assertEqual((homepage.status().text, homepage.status().level), ("Not set up", WARN))
        self.assertEqual(homepage.shortcuts(), [])

    def test_fireplaces_off_offered_only_while_one_is_lit(self):
        self.assertEqual([(s.id, s.title) for s in homepage.shortcuts()], [("fireplaces_off", "Fireplaces off")])
        self.living.cloud.power = False
        self.tick(service.SLOW_POLL + 61)
        self.assertEqual(homepage.shortcuts(), [])

    def test_single_fireplace_shortcut_title(self):
        self.client.fps.remove(self.den)
        self.assertEqual([s.title for s in homepage.shortcuts()], ["Fireplace off"])

    def test_fireplaces_off_only_touches_lit_fireplaces(self):
        self.assertIsNone(homepage.run_shortcut("fireplaces_off"))
        self.assertEqual(self.living.calls, [("set_power", False)])
        self.assertEqual(self.den.calls, [])
        self.assertEqual(homepage.run_shortcut("fireplaces_off"), "Fireplaces are already off")

    def test_unknown_shortcut(self):
        with self.assertRaises(ValueError):
            homepage.run_shortcut("scene-movie")


class ViewTests(Fresh, TestCase):
    def post(self, body, client=None):
        return (client or self.client_).post(reverse("fireplace_act"), body if isinstance(body, str) else
                                             json.dumps(body), content_type="application/json")

    def setUp(self):
        super().setUp()
        self.client_ = Client()

    def test_index(self):
        r = self.client_.get(reverse("fireplace_index"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for needle in ("fp-page", "fireplace/flames.js", "fireplace/fireplace.js", reverse("fireplace_state"),
                       reverse("fireplace_act")):
            self.assertIn(needle, html)

    def test_state(self):
        r = self.client_.get(reverse("fireplace_state"))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual([f["name"] for f in data["fireplaces"]], ["Living Room", "Den"])
        self.assertTrue(service.viewer_active())

    def test_act(self):
        r = self.post({"id": LIVING, "action": "flame_speed", "value": 4})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["fireplace"]["model"]["flame_speed"], 4)
        self.assertEqual(self.living.calls, [("set_flame_speed", 4)])

    def test_act_errors(self):
        service.state()
        cases = [
            ("not json", 400),
            ([1, 2], 400),
            ({"id": LIVING, "action": "flame_speed", "value": 9}, 400),
            ({"id": "AC000W999999999", "action": "power", "value": True}, 404),
            ({"id": DEN, "action": "temp", "value": 22}, 409),
        ]
        for body, code in cases:
            with self.subTest(body=body):
                r = self.post(body)
                self.assertEqual(r.status_code, code)
                self.assertIn("error", r.json())

    def test_act_auth_error(self):
        self.bad_password = True
        r = self.post({"id": LIVING, "action": "power", "value": False})
        self.assertEqual((r.status_code, r.json()["error"]), (502, cloud.AUTH_MESSAGE))

    def test_act_not_configured(self):
        secrets.delete("napoleon_password", app="fireplace")
        r = self.post({"id": LIVING, "action": "power", "value": False})
        self.assertEqual(r.status_code, 409)

    def test_act_needs_csrf_and_post(self):
        r = self.post({"id": LIVING, "action": "power", "value": False}, Client(enforce_csrf_checks=True))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client_.get(reverse("fireplace_act")).status_code, 405)
        self.assertEqual(self.living.calls, [])

    def test_looks(self):
        service.state()
        url = reverse("fireplace_looks")
        post = lambda body, c=None: (c or self.client_).post(url, json.dumps(body), content_type="application/json")
        r = post({"op": "save", "id": LIVING, "name": "Cozy"})
        self.assertEqual(r.status_code, 200)
        look = r.json()["looks"][0]
        self.assertEqual(look["name"], "Cozy")
        self.assertEqual(self.client_.get(reverse("fireplace_state")).json()["looks"], [look])
        r = self.post({"id": DEN, "action": "look", "value": look["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["fireplace"]["model"]["power"])
        for body, code in (({"op": "save", "id": DEN, "name": ""}, 400), ({"op": "nope"}, 400),
                           ({"op": "delete", "look": "missing"}, 404)):
            with self.subTest(body=body):
                self.assertEqual(post(body).status_code, code)
        self.assertEqual(post({"op": "delete", "look": look["id"]}, Client(enforce_csrf_checks=True)).status_code, 403)
        self.assertEqual(post({"op": "delete", "look": look["id"]}).json(), {"looks": []})
        self.assertEqual(self.client_.get(url).status_code, 405)

    def test_owners_save_house_presets_and_everyone_else_guest_ones(self):
        service.state()
        url = reverse("fireplace_looks")
        post = lambda body: self.client_.post(url, json.dumps(body), content_type="application/json")
        self.assertIn("expires", post({"op": "save", "id": LIVING, "name": "Guest"}).json()["looks"][0])
        with patch("fireplace.views._is_guest", return_value=False):
            looks = post({"op": "save", "id": LIVING, "name": "House"}).json()["looks"]
            self.assertEqual([(s["name"], s["mine"]) for s in looks], [("House", True)])
            self.assertFalse(self.client_.get(reverse("fireplace_state")).json()["guest"])
        state = self.client_.get(reverse("fireplace_state")).json()
        self.assertEqual(([s["name"] for s in state["looks"]], state["guest"]), (["House", "Guest"], True))

    def test_who_counts_as_a_guest(self):
        from types import SimpleNamespace as NS

        from fireplace.views import _is_guest
        staff, plain = NS(is_staff=True), NS(is_staff=False)
        self.assertTrue(_is_guest(NS()))  # no identity at all
        self.assertTrue(_is_guest(NS(bdh_mode="guest", user=plain)))
        self.assertFalse(_is_guest(NS(bdh_mode="owner", user=plain)))
        self.assertFalse(_is_guest(NS(bdh_mode="guest", user=staff)))


class WorkerTests(Fresh, SimpleTestCase):
    def test_should_start(self):
        self.assertFalse(worker.should_start(["manage.py", "test"]))
        self.assertTrue(worker.should_start(["/usr/lib/python3/site-packages/bdh_sdk/runtime/__main__.py"]))
        with patch.dict("os.environ", {"BDH_FIREPLACE_WORKER": "0"}):
            self.assertFalse(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))

    def test_tick_polls_when_due(self):
        worker.tick()
        self.assertEqual(len(self.logins), 1)
        polled = self.client.lists
        self.living.cloud.power = False
        self.tick(60)
        worker.tick()  # nobody watching: slow cadence, not due yet
        self.assertTrue(self.fp()["model"]["power"])
        service.note_viewer()
        self.tick(service.FAST_POLL)
        worker.tick()
        self.assertFalse(self.fp()["model"]["power"])
        self.assertEqual(self.client.lists, polled)

    def test_tick_does_nothing_until_configured(self):
        secrets.delete("napoleon_password", app="fireplace")
        worker.tick()
        self.assertEqual(self.logins, [])

    def test_tick_swallows_errors(self):
        self.bad_password = True
        worker.tick()
        self.assertEqual(service.state(max_age=1e9)["error"], cloud.AUTH_MESSAGE)


class PluginTests(SimpleTestCase):
    def test_plugin_importable(self):
        assert_plugin_importable("fireplace.plugin")

    def test_manifest(self):
        from fireplace.plugin import spec
        self.assertEqual((spec.network, spec.lifecycle), ("outbound", "always"))
        self.assertEqual([s.key for s in spec.settings], ["napoleon_email", "napoleon_password", "napoleon_europe"])
        self.assertLessEqual(spec.memory_mb, 128)
        self.assertEqual([t.title for t in spec.tiles], ["Fireplace"])
        self.assertTrue((ROOT / "static" / spec.tiles[0].icon).exists())
        self.assertTrue((ROOT / "static" / spec.tiles[0].icon_on).exists())
        self.assertIn("<animate", (ROOT / "static" / spec.tiles[0].icon_on).read_text())
        self.assertNotIn("<animate", (ROOT / "static" / spec.tiles[0].icon).read_text())
        self.assertEqual(reverse("fireplace_state"), "/fireplace/api/state/")


class HubFrameLayoutTests(SimpleTestCase):
    """The hub shows apps in a full-height frame with no navbar inside it."""

    def test_no_navbar_offsets(self):
        for path in list(ROOT.glob("templates/**/*.html")) + list(ROOT.glob("static/**/*.css")):
            text = path.read_text(encoding="utf-8").replace(" ", "")
            for bad in ("top:62px", "top:64px", "118px", "100vh-6", ".navbar"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")

    def test_page_never_scrolls_on_the_kiosk(self):
        css = (ROOT / "static/fireplace/fireplace.css").read_text(encoding="utf-8").replace(" ", "")
        self.assertRegex(css, r"body\.fp-page\{[^}]*overflow:hidden")
        root = re.search(r"\n\.fp\{([^}]*)\}", css).group(1)
        for rule in ("position:fixed", "top:0", "bottom:0", "overflow:hidden"):
            self.assertIn(rule, root)
        # The firebox soaks up the leftover height instead of a fixed aspect ratio.
        box = re.search(r"\n\.fp-box\{([^}]*)\}", css).group(1)
        self.assertIn("flex:11auto", box)
        self.assertIn("min-height:", box)
        html = (ROOT / "templates/fireplace/index.html").read_text(encoding="utf-8")
        self.assertIn("{% block body_class %}fp-page{% endblock %}", html)


class FrameworkClassTests(SimpleTestCase):
    """The runtime base.html loads Bootstrap, so any class we share with it
    silently picks up its styles (the card's .row/.step/.seg would)."""

    def test_no_bootstrap_class_names(self):
        fixture = pathlib.Path(__file__).with_name("bootstrap_classes.txt").read_text(encoding="utf-8")
        bootstrap = {ln.strip() for ln in fixture.splitlines() if ln.strip() and not ln.startswith("#")}
        ours = {}
        for path in ROOT.glob("static/**/*.css"):
            text = re.sub(r"url\([^)]*\)", "", path.read_text(encoding="utf-8"))
            for name in re.findall(r"\.(-?[_a-zA-Z][\w-]*)", text):
                ours.setdefault(name, path.name)
        for path in ROOT.glob("templates/**/*.html"):
            for attr in re.findall(r'class="([^"{]*)"', path.read_text(encoding="utf-8")):
                for name in attr.split():
                    ours.setdefault(name, path.name)
        for path in ROOT.glob("static/fireplace/fireplace*.js"):
            text = path.read_text(encoding="utf-8")
            pats = (r'class="([^"$]*)"', r"""classList\.(?:add|toggle|remove)\(\s*["']([^"']*)["']""",
                    r"""className\s*=\s*["']([^"']*)["']""")
            for pat in pats:
                for attr in re.findall(pat, text):
                    for name in attr.split():
                        ours.setdefault(name, path.name)
        self.assertGreater(len(ours), 30)  # the scan found the page's classes
        # Font Awesome's own classes are fine.
        ours = {n: w for n, w in ours.items() if not (n == "fas" or n.startswith("fa-"))}
        clashes = sorted(f"{name} ({where})" for name, where in ours.items() if name in bootstrap)
        self.assertEqual(clashes, [])


class PhoneLayoutTests(SimpleTestCase):
    def test_grid_columns_can_shrink(self):
        css = list(ROOT.glob("static/**/*.css"))
        self.assertTrue(css)
        for path in css:
            for cols in re.findall(r"grid-template-columns\s*:([^;}]*)", path.read_text(encoding="utf-8")):
                bare = re.sub(r"minmax\(\s*0\s*,\s*[\d.]+fr\s*\)", "", cols)
                self.assertNotRegex(bare, r"\d+fr", f"{path.name}: {cols.strip()}")

    def test_has_phone_layout(self):
        css = (ROOT / "static/fireplace/fireplace.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:700px)", css.replace(" ", ""))
