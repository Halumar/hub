"""Kids mode: pure rule engine (services/kids_rules.py).

Validation, quiet hours (midnight, time zones, DST, chains), layers
profile -> area -> entity, and every rule building block.
"""

from datetime import datetime, timedelta, timezone

import pytest
from services import kids_rules as kr

UTC = timezone.utc
ZH = "Europe/Zurich"

WEEKNIGHTS = [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "20:00", "end": "06:30"}
]


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def light(state="off", **attrs):
    return {"state": state, "attributes": attrs}


def ev(profile=None, areas=None, entity=None, **kw):
    kw.setdefault("entity_id", "light.kids_room")
    kw.setdefault("service", "turn_on")
    kw.setdefault("data", {})
    kw.setdefault("state", light())
    kw.setdefault("now", utc(2026, 11, 18, 12, 0))  # Wed 13:00 Zurich
    kw.setdefault("tz_name", ZH)
    return kr.evaluate(profile or {}, areas if areas is not None else [], entity, **kw)


# ── validation ──────────────────────────────────────────────────────────────


class TestValidate:
    def test_default_rules_are_valid(self):
        assert (
            kr.validate_rules(kr.DEFAULT_CHILD_RULES, kr.LEVEL_PROFILE)
            == kr.DEFAULT_CHILD_RULES
        )

    def test_none_and_empty(self):
        assert kr.validate_rules(None, kr.LEVEL_AREA) == {}
        assert kr.validate_rules({}, kr.LEVEL_ENTITY) == {}

    @pytest.mark.parametrize(
        "rules,reason",
        [
            ({"nope": 1}, "rules.nope"),
            ({"blocked": "yes"}, "rules.blocked"),
            ({"color_lock": 1}, "rules.color_lock"),
            ({"blocked_types": ["Lock"]}, "rules.blocked_types"),
            ({"blocked_types": "lock"}, "rules.blocked_types"),
            ({"blocked_types": ["a"] * 31}, "rules.blocked_types"),
            ({"volume_max": 101}, "rules.volume_max"),
            ({"volume_max": True}, "rules.volume_max"),
            (
                {
                    "rate_limit": {
                        "max_actions": 0,
                        "window_seconds": 60,
                        "cooldown_seconds": 0,
                    }
                },
                "rules.rate_limit",
            ),
            (
                {
                    "rate_limit": {
                        "max_actions": 5,
                        "window_seconds": 4,
                        "cooldown_seconds": 0,
                    }
                },
                "rules.rate_limit",
            ),
            (
                {"rate_limit": {"max_actions": 5, "window_seconds": 60}},
                "rules.rate_limit",
            ),
            (
                {
                    "rate_limit": {
                        "max_actions": 5,
                        "window_seconds": 60,
                        "cooldown_seconds": 1,
                        "x": 1,
                    }
                },
                "rules.rate_limit",
            ),
            (
                {"quiet_hours": [{"days": [], "start": "20:00", "end": "06:00"}]},
                "rules.quiet_hours",
            ),
            (
                {"quiet_hours": [{"days": ["mo"], "start": "20:00", "end": "06:00"}]},
                "rules.quiet_hours",
            ),
            (
                {"quiet_hours": [{"days": ["mon"], "start": "24:00", "end": "06:00"}]},
                "rules.quiet_hours",
            ),
            (
                {"quiet_hours": [{"days": ["mon"], "start": "8:00", "end": "06:00"}]},
                "rules.quiet_hours",
            ),
            (
                {
                    "quiet_hours": [
                        {"days": ["mon", "mon"], "start": "20:00", "end": "06:00"}
                    ]
                },
                "rules.quiet_hours",
            ),
            (
                {"quiet_hours": [{"days": ["mon"], "start": "20:00"}]},
                "rules.quiet_hours",
            ),
            ({"quiet_hours": [WEEKNIGHTS[0]] * 15}, "rules.quiet_hours"),
        ],
    )
    def test_invalid(self, rules, reason):
        with pytest.raises(kr.RuleError) as e:
            kr.validate_rules(rules, kr.LEVEL_PROFILE)
        assert e.value.reason == reason

    def test_entity_level_has_no_blocked_types(self):
        with pytest.raises(kr.RuleError):
            kr.validate_rules({"blocked_types": ["lock"]}, kr.LEVEL_ENTITY)

    def test_canonical_days_order_and_dedup_types(self):
        out = kr.validate_rules(
            {
                "quiet_hours": [
                    {"days": ["sun", "mon"], "start": "21:00", "end": "07:00"}
                ],
                "blocked_types": ["lock", "lock", "cover:garage"],
                "rate_limit": None,
                "volume_max": None,
            },
            kr.LEVEL_AREA,
        )
        assert out["quiet_hours"][0]["days"] == ["mon", "sun"]
        assert out["blocked_types"] == ["lock", "cover:garage"]
        assert out["rate_limit"] is None and out["volume_max"] is None


# ── quiet hours ─────────────────────────────────────────────────────────────


class TestQuietHours:
    def test_outside(self):
        # Wed 18.11.2026 13:00 Zurich (UTC+1)
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 11, 18, 12, 0), ZH) is None

    def test_evening_same_day(self):
        # Wed 21:00 Zurich -> Thu 06:30 Zurich = 05:30 UTC
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 11, 18, 20, 0), ZH) == utc(
            2026, 11, 19, 5, 30
        )

    def test_early_morning_belongs_to_previous_day(self):
        # Tue 05:00 Zurich: in the window that started Mon 20:00
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 11, 17, 4, 0), ZH) == utc(
            2026, 11, 17, 5, 30
        )

    def test_saturday_morning_from_friday_window(self):
        # Fri window ends Sat 06:30; Sat 05:00 Zurich is still quiet
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 11, 21, 4, 0), ZH) == utc(
            2026, 11, 21, 5, 30
        )

    def test_sunday_morning_not_from_saturday(self):
        # No window on Saturday: Sun 05:00 is free
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 11, 22, 4, 0), ZH) is None

    def test_boundaries(self):
        start = utc(2026, 11, 18, 19, 0)  # Wed 20:00 Zurich
        assert kr.quiet_until(WEEKNIGHTS, start, ZH) is not None
        assert kr.quiet_until(WEEKNIGHTS, start - timedelta(seconds=1), ZH) is None
        end = utc(2026, 11, 19, 5, 30)
        assert kr.quiet_until(WEEKNIGHTS, end, ZH) is None

    def test_time_zone_matters(self):
        now = utc(2026, 11, 18, 19, 30)  # 20:30 Zurich, 19:30 UTC
        assert kr.quiet_until(WEEKNIGHTS, now, ZH) is not None
        assert kr.quiet_until(WEEKNIGHTS, now, "UTC") is None
        assert kr.quiet_until(WEEKNIGHTS, now, None) is None  # UTC fallback
        assert kr.quiet_until(WEEKNIGHTS, now, "Not/AZone") is None

    def test_summer_offset(self):
        # Wed 17.06.2026 20:30 Zurich = 18:30 UTC (UTC+2)
        assert kr.quiet_until(WEEKNIGHTS, utc(2026, 6, 17, 18, 30), ZH) == utc(
            2026, 6, 18, 4, 30
        )

    def test_dst_spring_forward(self):
        # Sun 29.03.2026: 02:00 -> 03:00. Window Sat 22:00 -> Sun 06:30 is 7.5 h.
        w = [{"days": ["sat"], "start": "22:00", "end": "06:30"}]
        assert kr.quiet_until(w, utc(2026, 3, 29, 1, 30), ZH) == utc(2026, 3, 29, 4, 30)

    def test_dst_fall_back(self):
        # Sun 25.10.2026: 03:00 -> 02:00. 06:30 local = 05:30 UTC.
        w = [{"days": ["sat"], "start": "22:00", "end": "06:30"}]
        assert kr.quiet_until(w, utc(2026, 10, 25, 1, 30), ZH) == utc(
            2026, 10, 25, 5, 30
        )

    def test_whole_day(self):
        w = [{"days": ["wed"], "start": "00:00", "end": "00:00"}]
        assert kr.quiet_until(w, utc(2026, 11, 18, 12, 0), ZH) == utc(
            2026, 11, 18, 23, 0
        )

    def test_chained_windows(self):
        w = [
            {"days": ["wed"], "start": "19:00", "end": "22:00"},
            {"days": ["wed"], "start": "21:00", "end": "07:00"},
        ]
        assert kr.quiet_until(w, utc(2026, 11, 18, 18, 30), ZH) == utc(
            2026, 11, 19, 6, 0
        )

    def test_empty(self):
        assert kr.quiet_until([], utc(2026, 11, 18, 20, 0), ZH) is None


# ── layers and access ───────────────────────────────────────────────────────


class TestLayers:
    def test_blocked_type(self):
        d = ev(
            {"blocked_types": ["lock"]}, entity_id="lock.front_door", service="unlock"
        )
        assert not d.allowed and d.reason == "blocked" and d.level == "profile"

    def test_device_class_type(self):
        garage = {"state": "closed", "attributes": {"device_class": "garage"}}
        d = ev(
            {"blocked_types": ["cover:garage"]},
            entity_id="cover.garage_door",
            service="open_cover",
            state=garage,
        )
        assert d.reason == "blocked"
        window = {"state": "closed", "attributes": {"device_class": "window"}}
        assert ev(
            {"blocked_types": ["cover:garage"]},
            entity_id="cover.w",
            service="open_cover",
            state=window,
        ).allowed

    def test_entity_allow_beats_type(self):
        d = ev(
            {"blocked_types": ["cover:garage"]},
            entity={"blocked": False},
            entity_id="cover.bike",
            service="open_cover",
            state={"state": "closed", "attributes": {"device_class": "garage"}},
        )
        assert d.allowed

    def test_area_types_replace_profile_types(self):
        d = ev(
            {"blocked_types": ["lock"]},
            [{"blocked_types": ["media_player"]}],
            entity_id="lock.x",
            service="lock",
        )
        assert d.allowed
        d = ev(
            {"blocked_types": ["lock"]},
            [{"blocked_types": ["media_player"]}],
            entity_id="media_player.tv",
            service="turn_on",
        )
        assert d.reason == "blocked" and d.level == "area"

    def test_profile_blocked_all_with_area_allowed(self):
        assert ev({"blocked": True}).reason == "blocked"
        assert ev({"blocked": True}, [{"blocked": False}]).allowed
        assert (
            ev({"blocked": True}, [{"blocked": False}], {"blocked": True}).level
            == "entity"
        )

    def test_area_quiet_exception_and_entity_exception(self):
        profile = {"quiet_hours": WEEKNIGHTS}
        at = utc(2026, 11, 18, 19, 15)  # Wed 20:15 Zurich
        assert ev(profile, now=at).reason == "quiet_hours"
        kids_room = {
            "quiet_hours": [
                {
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "20:30",
                    "end": "06:30",
                }
            ]
        }
        assert ev(profile, [kids_room], now=at).allowed
        assert ev(profile, [kids_room], now=utc(2026, 11, 18, 19, 45)).level == "area"
        assert ev(
            profile, [kids_room], {"quiet_hours": []}, now=utc(2026, 11, 18, 23, 0)
        ).allowed

    def test_strictest_area_wins(self):
        at = utc(2026, 11, 18, 19, 15)
        free = {"quiet_hours": []}
        strict = {"quiet_hours": WEEKNIGHTS}
        d = ev({}, [free, strict], now=at)
        assert not d.allowed and d.level == "area"
        assert ev({}, [free, {"blocked": True}], now=at).reason == "blocked"

    def test_no_area(self):
        assert (
            ev({"quiet_hours": WEEKNIGHTS}, [], now=utc(2026, 11, 18, 19, 15)).level
            == "profile"
        )


# ── building blocks ─────────────────────────────────────────────────────────


class TestQuietActions:
    at = utc(2026, 11, 18, 21, 0)

    def test_turn_on_blocked_with_until(self):
        d = ev({"quiet_hours": WEEKNIGHTS}, now=self.at)
        assert d.reason == "quiet_hours" and d.until == utc(2026, 11, 19, 5, 30)
        assert d.retry_after(self.at) == int((d.until - self.at).total_seconds())

    @pytest.mark.parametrize(
        "service",
        [
            "turn_off",
            "media_pause",
            "media_stop",
            "volume_down",
            "close_cover",
            "stop_cover",
        ],
    )
    def test_reducing_allowed(self, service):
        assert ev({"quiet_hours": WEEKNIGHTS}, service=service, now=self.at).allowed

    def test_toggle_only_when_on(self):
        assert ev(
            {"quiet_hours": WEEKNIGHTS},
            service="toggle",
            state=light("on"),
            now=self.at,
        ).allowed
        assert not ev(
            {"quiet_hours": WEEKNIGHTS},
            service="toggle",
            state=light("off"),
            now=self.at,
        ).allowed

    def test_mute_but_not_unmute(self):
        p = {"quiet_hours": WEEKNIGHTS}
        assert ev(
            p,
            entity_id="media_player.tv",
            service="volume_mute",
            data={"is_volume_muted": True},
            now=self.at,
        ).allowed
        assert not ev(
            p,
            entity_id="media_player.tv",
            service="volume_mute",
            data={"is_volume_muted": False},
            now=self.at,
        ).allowed

    def test_override_suspends(self):
        d = ev(
            {"quiet_hours": WEEKNIGHTS},
            now=self.at,
            override_until=self.at + timedelta(minutes=15),
        )
        assert d.allowed
        d = ev(
            {"quiet_hours": WEEKNIGHTS},
            now=self.at,
            override_until=self.at - timedelta(minutes=1),
        )
        assert not d.allowed

    def test_override_keeps_blocked_types(self):
        d = ev(
            {"blocked_types": ["lock"]},
            entity_id="lock.x",
            service="unlock",
            override_until=utc(2027, 1, 1),
        )
        assert d.reason == "blocked"


class TestRateLimit:
    rate = {
        "rate_limit": {"max_actions": 3, "window_seconds": 60, "cooldown_seconds": 300}
    }
    now = utc(2026, 11, 18, 12, 0)

    def test_under_limit(self):
        recent = [self.now - timedelta(seconds=s) for s in (10, 20)]
        assert ev(self.rate, recent=recent, now=self.now).allowed

    def test_old_actions_do_not_count(self):
        recent = [self.now - timedelta(seconds=s) for s in (10, 70, 80)]
        assert ev(self.rate, recent=recent, now=self.now).allowed

    def test_limit_starts_cooldown(self):
        recent = [self.now - timedelta(seconds=s) for s in (5, 10, 20)]
        d = ev(self.rate, recent=recent, now=self.now)
        assert d.reason == "rate_limited" and d.until == self.now + timedelta(minutes=5)
        assert d.start_cooldown_until == d.until
        assert d.limit == {"max_actions": 3, "window_seconds": 60}

    def test_cooldown(self):
        until = self.now + timedelta(minutes=2)
        d = ev(self.rate, cooldown_until=until, now=self.now)
        assert d.reason == "cooldown" and d.until == until
        assert ev(
            self.rate, cooldown_until=self.now - timedelta(seconds=1), now=self.now
        ).allowed

    def test_no_cooldown_frees_with_window(self):
        rate = {
            "rate_limit": {
                "max_actions": 2,
                "window_seconds": 60,
                "cooldown_seconds": 0,
            }
        }
        recent = [self.now - timedelta(seconds=50), self.now - timedelta(seconds=10)]
        d = ev(rate, recent=recent, now=self.now)
        assert d.reason == "rate_limited" and d.until == self.now + timedelta(
            seconds=10
        )
        assert d.start_cooldown_until is None

    def test_entity_switches_off(self):
        recent = [self.now - timedelta(seconds=s) for s in (5, 10, 20)]
        assert ev(
            self.rate, entity={"rate_limit": None}, recent=recent, now=self.now
        ).allowed

    def test_override_suspends_cooldown(self):
        d = ev(
            self.rate,
            cooldown_until=self.now + timedelta(minutes=2),
            now=self.now,
            override_until=self.now + timedelta(minutes=10),
        )
        assert d.allowed


class TestColorAndVolume:
    def test_color_lock(self):
        p = {"color_lock": True}
        assert ev(p, data={"rgb_color": [255, 0, 0]}).reason == "color_locked"
        assert ev(p, data={"effect": "rainbow"}).reason == "color_locked"
        assert ev(p, data={"brightness_pct": 50}).allowed
        assert ev(p, data={"color_temp_kelvin": 2700}).allowed
        assert ev(p, entity={"color_lock": False}, data={"hs_color": [1, 2]}).allowed
        assert ev(p, entity_id="switch.x", data={"rgb_color": [1, 2, 3]}).allowed

    def media(self, volume=None):
        attrs = {} if volume is None else {"volume_level": volume}
        return {"state": "playing", "attributes": attrs}

    def test_volume_set_clamped(self):
        d = ev(
            {"volume_max": 40},
            entity_id="media_player.tv",
            service="volume_set",
            data={"volume_level": 0.9},
            state=self.media(0.2),
        )
        assert (
            d.allowed
            and d.data == {"volume_level": 0.4}
            and d.adjusted == {"volume_level": 0.4}
        )
        d = ev(
            {"volume_max": 40},
            entity_id="media_player.tv",
            service="volume_set",
            data={"volume_level": 0.3},
            state=self.media(0.2),
        )
        assert d.allowed and d.adjusted is None and d.data == {"volume_level": 0.3}

    def test_volume_up(self):
        p = {"volume_max": 40}
        kw = {"entity_id": "media_player.tv", "service": "volume_up"}
        assert ev(p, state=self.media(0.2), **kw).service == "volume_up"
        d = ev(p, state=self.media(0.35), **kw)
        assert d.service == "volume_set" and d.data == {"volume_level": 0.4}
        d = ev(p, state=self.media(0.4), **kw)
        assert d.reason == "volume_limit" and d.limit == {"volume_max": 40}
        assert ev(p, state=self.media(None), **kw).reason == "volume_limit"

    def test_area_cap_stricter(self):
        d = ev(
            {"volume_max": 60},
            [{"volume_max": 30}, None],
            entity_id="media_player.tv",
            service="volume_set",
            data={"volume_level": 0.5},
            state=self.media(0.1),
        )
        assert d.adjusted == {"volume_level": 0.3}

    def test_volume_null_switches_off(self):
        d = ev(
            {"volume_max": 40},
            entity={"volume_max": None},
            entity_id="media_player.tv",
            service="volume_set",
            data={"volume_level": 0.9},
            state=self.media(0.1),
        )
        assert d.adjusted is None and d.data == {"volume_level": 0.9}


class TestCounters:
    def test_record_and_cooldown(self):
        c = kr.ActionCounters()
        now = utc(2026, 11, 18, 12, 0)
        c.record("a", "light.x", now)
        assert c.recent("a", "light.x") == [now]
        c.start_cooldown("a", "light.x", now + timedelta(minutes=5))
        assert c.recent("a", "light.x") == []
        assert c.cooldown("a", "light.x", now) == now + timedelta(minutes=5)
        assert c.cooldowns_of("a", now) == {"light.x": now + timedelta(minutes=5)}
        assert c.cooldown("a", "light.x", now + timedelta(minutes=6)) is None
        c.start_cooldown("a", "light.y", now + timedelta(minutes=5))
        assert c.clear_client("a") == 1
        assert c.cooldowns_of("a", now) == {}


# ── pause everything ─────────────────────────────────────────────────

LIMIT_ALL = {
    "max_actions": 3,
    "max_entities": 2,
    "window_seconds": 60,
    "cooldown_seconds": 300,
}


def test_rate_limit_all_only_when_enabled():
    assert kr.rate_limit_all(None) is None
    assert kr.rate_limit_all({**LIMIT_ALL, "enabled": False}) is None
    assert kr.rate_limit_all({**LIMIT_ALL, "enabled": True}) == LIMIT_ALL


def test_evaluate_all_entities_actions_window_and_running_pause():
    now = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
    sec = timedelta(seconds=1)
    two = [(now - 10 * sec, "light.a"), (now - 5 * sec, "light.b")]
    assert kr.evaluate_all(None, entity_id="light.c", now=now, recent=two) is None
    # Same entity again: no new device, below max_actions.
    assert kr.evaluate_all(LIMIT_ALL, entity_id="light.a", now=now, recent=two) is None
    d = kr.evaluate_all(LIMIT_ALL, entity_id="light.c", now=now, recent=two)
    assert not d.allowed and d.reason == kr.REASON_PAUSED_ALL
    assert d.until == now + timedelta(seconds=300) == d.start_pause_all_until
    assert d.pause_all == {
        "trigger": "entities",
        "actions": 2,
        "entities": ["light.a", "light.b", "light.c"],
    }
    assert d.limit == {"max_actions": 3, "max_entities": 2, "window_seconds": 60}
    three = two + [(now - 2 * sec, "light.a")]
    d = kr.evaluate_all(LIMIT_ALL, entity_id="light.a", now=now, recent=three)
    assert d.pause_all["trigger"] == "actions"
    # Outside the window nothing counts.
    old = [(now - 61 * sec, "light.a"), (now - 70 * sec, "light.b")] * 2
    assert kr.evaluate_all(LIMIT_ALL, entity_id="light.c", now=now, recent=old) is None
    # A running pause rejects without starting a new one.
    d = kr.evaluate_all(
        LIMIT_ALL, entity_id="light.z", now=now, paused_until=now + 60 * sec
    )
    assert d.reason == kr.REASON_PAUSED_ALL and d.start_pause_all_until is None
    assert (
        kr.evaluate_all(LIMIT_ALL, entity_id="light.z", now=now, paused_until=now - sec)
        is None
    )


def test_counters_pause_all_and_clear():
    c = kr.ActionCounters()
    now = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
    c.record("dev", "light.a", now)
    c.record("dev", "light.b", now)
    assert c.device_recent("dev") == [(now, "light.a"), (now, "light.b")]
    c.start_pause_all("dev", now + timedelta(minutes=5))
    assert c.device_recent("dev") == []
    assert c.paused_all("dev", now) == now + timedelta(minutes=5)
    assert c.paused_all("dev", now + timedelta(minutes=6)) is None
    c.start_pause_all("dev", now + timedelta(minutes=5))
    assert c.clear_client("dev") == 1
    assert c.paused_all("dev", now) is None


# ── next bedtime ───────────────────────────────────────────────────


def _w(days, start, end):
    return {"days": days, "start": start, "end": end}


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


@pytest.mark.parametrize(
    ("windows", "now", "expected"),
    [
        # 9:23 local (CET = UTC+1) on Friday: today 20:00, not a sampled 20:08.
        (
            [_w(WEEKDAYS, "20:00", "07:00")],
            datetime(2026, 11, 20, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc),
        ),
        # Saturday without a window: Monday 20:00 (Sunday has none either).
        (
            [_w(WEEKDAYS, "20:00", "07:00")],
            datetime(2026, 11, 21, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc),
        ),
        # Several windows: the earliest start wins, minutes exact.
        (
            [_w(["fri"], "21:37", "06:00"), _w(["fri"], "13:05", "14:00")],
            datetime(2026, 11, 20, 8, 23, tzinfo=timezone.utc),
            datetime(2026, 11, 20, 12, 5, tzinfo=timezone.utc),
        ),
        # Summer time (CEST = UTC+2).
        (
            [_w(WEEKDAYS, "20:00", "07:00")],
            datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_next_quiet_start_is_exact(windows, now, expected):
    assert kr.next_quiet_start(windows, now, "Europe/Zurich") == expected


def test_next_quiet_start_inside_none_and_after_midnight():
    windows = [_w(WEEKDAYS, "20:00", "07:00")]
    # Inside the window (after midnight, Saturday 01:00 local belongs to Friday's).
    inside = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
    assert kr.quiet_until(windows, inside, "Europe/Zurich") is not None
    assert kr.next_quiet_start(windows, inside, "Europe/Zurich") is None
    assert kr.next_quiet_start([], inside, "Europe/Zurich") is None
    assert kr.next_quiet_start(None, inside, "Europe/Zurich") is None
    # A window starting exactly now is "inside", not "next".
    start = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)
    assert kr.next_quiet_start(windows, start, "Europe/Zurich") is None
    # bedtime_end still follows the next window to its end.
    assert kr.bedtime_end(
        windows, datetime(2026, 11, 20, 8, 23, tzinfo=timezone.utc), "Europe/Zurich"
    ) == datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)
