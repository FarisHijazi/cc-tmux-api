"""Isolated push persistence and policy tests; never contact push providers."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException

from cc_tmux_api import notifications
from cc_tmux_api.notifications import PushService


@pytest.fixture(autouse=True)
def sender(monkeypatch):
    mock = Mock(return_value=SimpleNamespace(status_code=201))
    monkeypatch.setattr(notifications, "webpush", mock)
    return mock


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(notifications.time, "time", lambda: now[0])
    return now


@pytest.fixture
def subscription():
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"endpoint": "https://fcm.googleapis.com/fcm/send/browser-token",
            "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}


@pytest.fixture
def service(tmp_path, subscription):
    service = PushService(tmp_path / "push", "mailto:user@example.com")
    service.subscribe(subscription)
    return service


def snapshot(status="working", event_id="first", **fields):
    return {"sessions": [{"id": "host/session with#special", "title": "Example session",
                           "host_label": "work", "status": status, "event_id": event_id,
                           "updated_at": "2026-01-01T00:00:00Z", **fields}]}


def reopen(service):
    return PushService(service._path.parent, "mailto:user@example.com")


def test_persistent_vapid_permissions_and_preferences(service):
    config = service.config()
    assert config["available"]
    raw = base64.urlsafe_b64decode(config["public_key"] + "==")
    assert len(raw) == 65 and raw[0] == 4
    assert service._path.parent.stat().st_mode & 0o777 == 0o700
    assert service._path.stat().st_mode & 0o777 == 0o600
    assert service._key.stat().st_mode & 0o777 == 0o600
    service.set_muted("session", True)
    restarted = reopen(service)
    assert restarted.config() == config
    assert restarted.preferences() == {"muted": ["session"]}
    assert restarted.set_muted("session", False) == {"muted": []}


def test_restart_and_replayed_events_do_not_duplicate(service, sender):
    service.observe(snapshot("waiting"))
    sender.assert_not_called()
    service.observe(snapshot("working", "second"))
    service.observe(snapshot("waiting", "third"))
    assert sender.call_count == 1
    restarted = reopen(service)
    restarted.observe(snapshot("waiting", "third"))
    restarted.observe(snapshot("waiting", "first"))
    assert sender.call_count == 1
    restarted.observe(snapshot("done", "fourth"))
    assert sender.call_count == 2


def test_empty_offline_and_unknown_never_reset_baseline(service, sender):
    service.observe({})
    service.observe({"sessions": []})
    service.observe(snapshot("waiting"))
    sender.assert_not_called()
    service.observe({"sessions": []})
    service.observe(snapshot("unknown", "offline"))
    service.observe(snapshot("done", "stale", stale=True))
    service.observe(snapshot("done", "offline", online=False))
    service.observe({**snapshot("done", "offline"), "online": False})
    service.observe(snapshot("waiting"))
    sender.assert_not_called()
    service.observe(snapshot("done", "fresh"))
    sender.assert_called_once()


def test_fallback_ignores_poll_timestamps_but_detects_new_transitions(service, sender):
    service.observe(snapshot(event_id=None))
    service.observe(snapshot("waiting", None))
    service.observe(snapshot("waiting", None, updated_at="later"))
    assert sender.call_count == 1
    service.observe(snapshot("working", None))
    service.observe(snapshot("waiting", None))
    assert sender.call_count == 2


def test_repeated_question_after_working_is_a_new_occurrence(service, sender):
    question = snapshot("waiting", "1200:identical-menu", event="menu")
    service.observe(snapshot())
    service.observe(question)
    assert sender.call_count == 1
    service.observe(question)
    service.observe(snapshot("working", "1200", notification_eligible=False))
    # The definite working observation and dedup history both survive restart.
    restarted = reopen(service)
    restarted.observe(question)
    assert sender.call_count == 2
    restarted.observe(question)
    reopen(service).observe(question)
    assert sender.call_count == 2


def test_reconnected_question_without_working_is_not_a_new_occurrence(service, sender):
    question = snapshot("waiting", "1200:identical-menu", event="menu")
    service.observe(snapshot())
    service.observe(question)
    service.observe({"sessions": []})
    service.observe(snapshot("unknown", "1200"))
    service.observe(snapshot("working", "1200", online=False))
    service.observe(snapshot("working", "1200", stale=True))
    restarted = reopen(service)
    restarted.observe(question)
    assert sender.call_count == 1


@pytest.mark.parametrize("fields", [
    {"notification_eligible": False}, {"status_emoji": "🥱", "notification_eligible": True},
])
def test_normalized_suppression(service, sender, fields):
    service.observe(snapshot())
    service.observe(snapshot("waiting", "next", **fields))
    sender.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"event": "menu"}, {"event": "notification"}, {"event": "outcome"},
    {"notification_eligible": True},
])
def test_normalized_events_in_idle_status(service, sender, fields):
    service.observe(snapshot())
    service.observe(snapshot("idle", "next", **fields))
    sender.assert_called_once()


def test_mute_discards_pending_and_does_not_replay_on_unmute(service, sender, clock):
    sid = snapshot()["sessions"][0]["id"]
    service.observe(snapshot())
    sender.side_effect = OSError("offline")
    service.observe(snapshot("waiting", "next"))
    assert sender.call_count == 1
    service.set_muted(sid, True)
    clock[0] += 31
    restarted = reopen(service)
    restarted.observe(snapshot("done", "muted"))
    restarted.set_muted(sid, False)
    sender.side_effect = None
    restarted.observe(snapshot("done", "muted"))
    assert sender.call_count == 1
    restarted.observe(snapshot("waiting", "new"))
    assert sender.call_count == 2


def test_failed_delivery_retries_after_restart_per_subscriber(service, subscription, sender, clock):
    second = {**subscription, "endpoint": "https://web.push.apple.com/browser-token"}
    service.subscribe(second)
    sender.side_effect = [SimpleNamespace(status_code=201), OSError("offline")]
    service.observe(snapshot())
    service.observe(snapshot("waiting", "next"))
    assert sender.call_count == 2
    failed_endpoint = sender.call_args.kwargs["subscription_info"]["endpoint"]
    restarted = reopen(service)
    restarted.observe(snapshot("waiting", "next"))
    assert sender.call_count == 2
    clock[0] += 31
    sender.side_effect = None
    restarted.observe({"sessions": []})
    assert sender.call_count == 3
    assert sender.call_args.kwargs["subscription_info"]["endpoint"] == failed_endpoint
    clock[0] += 100
    restarted.observe(snapshot("waiting", "next"))
    assert sender.call_count == 3


@pytest.mark.parametrize("status", [404, 410])
def test_expired_subscriptions_removed_on_exception(service, sender, status):
    sender.side_effect = WebPushException("expired", response=SimpleNamespace(status_code=status))
    assert service.test() == {"sent": 0, "failed": 1, "pending": 0}
    sender.side_effect = None
    assert reopen(service).test() == {"sent": 0, "failed": 0, "pending": 0}
    assert sender.call_count == 1


@pytest.mark.parametrize("expiry", [False, True])
def test_retry_limit_and_expiry(service, sender, clock, expiry):
    sender.side_effect = OSError("offline")
    service.test()
    if expiry:
        clock[0] += 3601
        service.observe({})
        assert sender.call_count == 1
    else:
        for _ in range(6):
            clock[0] += 500
            service.observe({})
        assert sender.call_count == 5
    with service._db() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/send/a", "https://127.0.0.1/send/a",
    "https://localhost/send/a", "https://fcm.googleapis.com.evil.example/send/a",
    "https://evilpush.apple.com/send/a", "https://push.apple.com.evil.example/send/a",
    "https://user@fcm.googleapis.com/send/a", "https://fcm.googleapis.com:444/send/a",
    "https://fcm.googleapis.com/send/a#fragment", "https://fcm.googleapis.com\\@localhost/a",
    "https://fcm.googleapis.com\n/send/a", "https://169.254.169.254/a",
    "https://fcm.googleapis.com./a", "https://updates.push.services.mozilla.com.evil.example/a",
])
def test_reject_ssrf_endpoints(service, subscription, sender, endpoint):
    with pytest.raises(ValueError):
        service.subscribe({**subscription, "endpoint": endpoint})
    sender.assert_not_called()


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/send/a", "https://updates.push.services.mozilla.com/wpush/v2/a",
    "https://web.push.apple.com/a", "https://one.two.push.apple.com/a",
])
def test_accept_browser_provider_endpoints(service, subscription, endpoint):
    assert service.subscribe({**subscription, "endpoint": endpoint}) == {"ok": True}


@pytest.mark.parametrize("value", [None, {}, {"endpoint": "https://fcm.googleapis.com/a"},
    {"endpoint": "https://fcm.googleapis.com/a", "keys": {"auth": "bad", "p256dh": "bad"}},
])
def test_invalid_subscription_keys(service, value):
    with pytest.raises(ValueError):
        service.subscribe(value)


@pytest.mark.parametrize("label,expected", [
    ("Task failed", "Task failed"),
    ("Blocked", "Blocked"),
    ("  Waiting\n for\t instructions  ", "Waiting for instructions"),
    ("x" * 200, "x" * 160),
    (None, "Session update"),
    (" \n\t", "Session update"),
    ({"unexpected": "value"}, "Session update"),
])
def test_push_body_uses_bounded_canonical_label(service, sender, label, expected):
    service.observe(snapshot())
    service.observe(snapshot("idle", "outcome", notification_eligible=True, status_label=label))
    sender.assert_called_once()
    assert json.loads(sender.call_args.kwargs["data"])["body"] == expected


def test_short_payload_and_escaped_deep_link(service, sender):
    service.observe(snapshot())
    service.observe(snapshot("waiting", "next", title="x" * 200, transcript="private transcript"))
    arguments = sender.call_args.kwargs
    payload = json.loads(arguments["data"])
    assert len(payload["title"]) == 80
    assert payload["body"] == "Needs your attention"
    assert payload["url"] == "/#session=host%2Fsession%20with%23special"
    assert "private transcript" not in arguments["data"]
    assert arguments["timeout"] == 10
    assert arguments["vapid_claims"] == {"sub": "mailto:user@example.com"}


def test_explicit_test_does_not_need_baseline_and_remove_is_durable(service, subscription, sender):
    assert service.test() == {"sent": 1, "failed": 0, "pending": 0}
    assert json.loads(sender.call_args.kwargs["data"])["body"] == "Test notification"
    assert service.remove(subscription["endpoint"]) == {"ok": True}
    assert reopen(service).test() == {"sent": 0, "failed": 0, "pending": 0}


def test_observation_only_queues_until_independent_delivery(service, sender):
    """The monitor can persist transitions without performing provider I/O."""
    service.observe(snapshot("working", "baseline"), deliver=False)
    service.observe(snapshot("waiting", "question"), deliver=False)
    sender.assert_not_called()

    assert service.deliver_pending() == {"sent": 1, "failed": 0, "pending": 0}
    sender.assert_called_once()
    assert json.loads(sender.call_args.kwargs["data"])["body"] == "Needs your attention"
    assert service.deliver_pending() == {"sent": 0, "failed": 0, "pending": 0}
    sender.assert_called_once()


def test_parallel_observers_do_not_duplicate(service, sender):
    service.observe(snapshot())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.observe(snapshot("waiting", "next")), range(8)))
    sender.assert_called_once()


@pytest.mark.parametrize("response_status", [201, 503])
def test_slow_send_does_not_block_preferences_or_mute(service, subscription, sender, response_status):
    service.subscribe({**subscription, "endpoint": "https://web.push.apple.com/second"})
    service.observe(snapshot())
    started, release = Event(), Event()

    def slow_send(**kwargs):
        started.set()
        assert release.wait(5), "Test failed to release the mocked provider"
        return SimpleNamespace(status_code=response_status)

    sender.side_effect = slow_send
    sid = snapshot()["sessions"][0]["id"]

    def preferences_while_sending():
        assert service.preferences() == {"muted": []}
        assert service.set_muted(sid, True) == {"muted": [sid]}
        service.subscribe(subscription)
        service.observe({})  # An already-active drain must not wait or send twice.

    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(service.observe, snapshot("waiting", "next"))
        try:
            assert started.wait(2)
            pool.submit(preferences_while_sending).result(timeout=2)
        finally:
            release.set()
        delivery.result(timeout=2)
    service.observe({})
    # Mute cancels both queued deliveries, but cannot recall the in-flight send.
    assert sender.call_count == 1
    with service._db() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_disabled_subject_does_not_send(tmp_path, subscription, sender):
    service = PushService(tmp_path, "")
    assert not service.config()["available"]
    assert service.config()["reason"]
    service.subscribe(subscription)
    assert service.test()["pending"] == 1
    sender.assert_not_called()


def test_transport_refuses_redirects(monkeypatch):
    request = Mock(return_value=SimpleNamespace(status_code=302))
    monkeypatch.setattr(notifications.requests.Session, "request", request)
    with notifications._NoRedirects() as transport:
        transport.post("https://fcm.googleapis.com/a", allow_redirects=True)
    assert request.call_args.kwargs["allow_redirects"] is False


def test_cached_sessions_on_unavailable_hosts_are_not_new_events(service, sender):
    service.observe(snapshot())
    service.observe({**snapshot("done", "cached", host="work"),
                     "hosts": [{"id": "work", "available": False}]})
    sender.assert_not_called()
    service.observe(snapshot("done", "fresh", host="work"))
    sender.assert_called_once()


@pytest.mark.parametrize("status", [302, 429, 500, 503])
def test_http_failure_remains_pending_with_stable_tag(service, sender, clock, status):
    sender.return_value = SimpleNamespace(status_code=status)
    assert service.test() == {"sent": 0, "failed": 1, "pending": 1}
    original = json.loads(sender.call_args.kwargs["data"])
    clock[0] += 31
    sender.return_value = SimpleNamespace(status_code=201)
    reopen(service).observe({})
    assert sender.call_count == 2
    assert json.loads(sender.call_args.kwargs["data"]) == original


def test_resubscribe_updates_keys_without_deleting_pending(service, subscription, sender, clock):
    sender.side_effect = OSError("offline")
    service.test()
    replacement = {**subscription, "keys": {**subscription["keys"],
                   "auth": base64.urlsafe_b64encode(b"b" * 16).decode().rstrip("=")}}
    service.subscribe(replacement)
    clock[0] += 31
    sender.side_effect = None
    reopen(service).observe({})
    assert sender.call_args.kwargs["subscription_info"] == replacement


def test_newly_discovered_session_is_seeded_silently(service, sender):
    service.observe(snapshot())
    service.observe(snapshot("waiting", "discovered", id="another-session"))
    sender.assert_not_called()
    service.observe(snapshot("done", "next", id="another-session"))
    sender.assert_called_once()
