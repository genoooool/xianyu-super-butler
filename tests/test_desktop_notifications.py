import ast
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.desktop_notifications import DesktopNotifications
from app.routers.desktop_notifications import create_desktop_notifications_router


class DesktopNotificationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.hub = DesktopNotifications(clock=lambda: self.now, limit=16)
        self.hub.configure("owner-session", 1, True)

    def publish(self, **overrides):
        event = dict(user_id=1, account_id="seller", sender_id="buyer", own_id="seller-id",
                     chat_id="chat", timestamp_ms=self.now * 1000, message_id="message-1", content="private buyer text")
        event.update(overrides)
        return self.hub.publish(**event)

    def poll(self, after=0):
        return self.hub.poll(after, lambda token, owner: token == "owner-session" and owner == 1)

    def test_only_incoming_unfiltered_private_messages(self):
        for overrides in [dict(sender_id="seller-id"), dict(sender_id=""), dict(session_type="30"),
                          dict(filtered=True), dict(content="发来一条消息"), dict(content=""), dict(user_id=2)]:
            with self.subTest(overrides=overrides):
                self.assertFalse(self.publish(**overrides))
        self.assertTrue(self.publish())
        self.assertEqual(self.poll()["count"], 1)

    def test_dedup_and_account_scoping(self):
        self.assertTrue(self.publish())
        self.assertFalse(self.publish())
        self.assertTrue(self.publish(account_id="other-seller"))
        batch = self.poll()
        self.assertEqual(batch["count"], 2)
        self.assertEqual(self.poll(batch["cursor"])["count"], 0)
        self.assertEqual(self.poll(), batch)  # retry before native cursor advances

    def test_fallback_hash_does_not_store_content(self):
        self.assertTrue(self.publish(message_id=None))
        self.assertFalse(self.publish(message_id=None))
        self.assertNotIn("private buyer text", repr(vars(self.hub)))
        self.assertNotIn("buyer", repr(self.poll()))

    def test_old_missing_future_and_expired_events(self):
        for timestamp in [999000, 0, None, "invalid", 1061000, float('nan'), float('inf')]:
            self.assertFalse(self.publish(timestamp_ms=timestamp))
        self.assertTrue(self.publish())
        self.now += 91
        self.assertEqual(self.poll()["count"], 0)
        self.assertFalse(self.publish(timestamp_ms=1000000))

    def test_disable_no_replay_and_other_session_cannot_disable(self):
        self.publish()
        self.hub.configure("other", 2, False)
        self.assertEqual(self.poll()["count"], 1)
        self.hub.configure("owner-session", 1, False)
        self.assertFalse(self.publish())
        self.hub.configure("owner-session", 1, True)
        self.assertEqual(self.poll()["count"], 0)

    def test_refresh_keeps_queue_but_switch_clears_it(self):
        self.publish()
        self.hub.configure("owner-session", 1, True)
        self.assertEqual(self.poll()["count"], 1)
        self.hub.configure("other-session", 2, True)
        self.assertFalse(self.publish())
        self.assertEqual(self.hub.poll(0, lambda *_: True)["count"], 0)

    def test_revoked_or_expired_login_stops_delivery(self):
        self.publish()
        self.assertEqual(self.hub.poll(0, lambda *_: False)["count"], 0)
        self.assertFalse(self.publish())

    def test_test_event_is_generic_and_rate_limited(self):
        self.assertFalse(self.hub.test("other-session"))
        self.assertTrue(self.hub.test("owner-session"))
        self.assertFalse(self.hub.test("owner-session"))
        self.assertEqual(self.poll(), {"cursor": 1, "count": 0, "test_count": 1, "handoff_count": 0, "sound": False})

    def test_handoff_has_separate_generic_alert_and_respects_preferences(self):
        self.publish()
        self.assertTrue(self.hub.publish_handoff(user_id=1, identity=('seller', 'chat', 1)))
        self.assertFalse(self.hub.publish_handoff(user_id=1, identity=('seller', 'chat', 1)))
        self.assertFalse(self.hub.publish_handoff(user_id=2, identity=('seller', 'chat', 2)))
        self.assertEqual(self.poll()['handoff_count'], 1)
        self.assertEqual(self.poll()['count'], 1)
        self.assertEqual(self.poll()['test_count'], 0)
        self.assertNotIn('chat', repr(self.hub.events))
        self.hub.configure('owner-session', 1, False)
        self.assertFalse(self.hub.publish_handoff(user_id=1, identity=('seller', 'chat', 2)))

    def test_sound_toggle_preserves_queue_and_cursor(self):
        self.publish()
        self.hub.configure("owner-session", 1, True, sound=True)
        batch = self.poll()
        self.assertTrue(batch["sound"])
        self.assertEqual(batch["count"], 1)
        self.hub.configure("owner-session", 1, True, sound=False)
        self.assertEqual(self.poll(), {**batch, "sound": False})
        self.assertEqual(self.poll(batch["cursor"])["count"], 0)

    def test_sound_session_isolation_disable_and_revocation(self):
        self.hub.configure("owner-session", 1, True, sound=True)
        self.hub.configure("other", 2, False, sound=False)
        self.assertTrue(self.poll()["sound"])
        self.hub.configure("owner-session", 1, False, sound=True)
        self.assertFalse(self.poll()["sound"])
        self.hub.configure("owner-session", 1, True, sound=True)
        self.assertFalse(self.hub.poll(0, lambda *_: False)["sound"])
        self.hub.configure("owner-session", 1, True, sound=True)
        self.hub.configure("other", 2, True)  # Older clients remain silent.
        self.assertFalse(self.hub.poll(0, lambda *_: True)["sound"])

    def test_queue_and_dedup_are_bounded(self):
        for index in range(100):
            self.publish(message_id=str(index))
        self.assertEqual(len(self.hub.events), 16)
        self.assertEqual(len(self.hub.seen), 16)
        self.assertEqual(self.poll()["cursor"], 100)

    def test_concurrent_replays_emit_once(self):
        threads = [threading.Thread(target=self.publish) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.poll()["count"], 1)


class DesktopNotificationRouteTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {"a": {"user_id": 1}, "b": {"user_id": 2}}
        self.hub = DesktopNotifications()
        self.app = FastAPI()
        self.app.include_router(create_desktop_notifications_router(
            self.hub, "launch-secret", self.verify))
        self.client = TestClient(self.app)

    def verify(self, credentials):
        return self.tokens.get(credentials.credentials) if credentials else None

    def configure(self, token="a", enabled=True):
        return self.client.post("/desktop/notifications/session", json={"enabled": enabled},
                                headers={"Authorization": f"Bearer {token}"})

    def test_login_required(self):
        self.assertEqual(self.client.get("/desktop/notifications/status").status_code, 401)
        self.assertEqual(self.configure("bad").status_code, 401)

    def test_poll_requires_native_secret_and_revocation_is_checked(self):
        self.assertEqual(self.configure().status_code, 200)
        self.hub.test("a")
        endpoint = "/desktop/notifications/poll"
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.get(endpoint, headers={"Authorization": "Bearer a"}).status_code, 403)
        native = {"X-Xianyu-Desktop-Token": "launch-secret"}
        self.assertEqual(self.client.get(endpoint, headers=native).json()["test_count"], 1)
        self.tokens.pop("a")
        self.assertEqual(self.client.get(endpoint, headers=native).json()["test_count"], 0)

    def test_other_user_cannot_test_or_disable_active_session(self):
        self.configure()
        self.assertEqual(self.client.post("/desktop/notifications/test", headers={"Authorization": "Bearer b"}).status_code, 409)
        self.configure("b", False)
        self.assertTrue(self.hub.status("a")["active"])

    def test_server_mode_unavailable(self):
        app = FastAPI()
        app.include_router(create_desktop_notifications_router(self.hub, "", self.verify))
        client = TestClient(app)
        self.assertFalse(client.get("/desktop/notifications/status", headers={"Authorization": "Bearer a"}).json()["available"])
        self.assertEqual(client.get("/desktop/notifications/poll").status_code, 404)

    def test_sound_preference_roundtrip_and_capability(self):
        with patch("app.routers.desktop_notifications.sys.platform", "darwin"):
            self.assertTrue(self.client.get("/desktop/notifications/status", headers={"Authorization": "Bearer a"}).json()["sound_available"])
        with patch("app.routers.desktop_notifications.sys.platform", "win32"):
            self.assertFalse(self.client.get("/desktop/notifications/status", headers={"Authorization": "Bearer a"}).json()["sound_available"])
        response = self.client.post("/desktop/notifications/session", json={"enabled": True, "sound": True},
                                    headers={"Authorization": "Bearer a"})
        self.assertEqual(response.status_code, 200)
        self.hub.test("a")
        batch = self.client.get("/desktop/notifications/poll", headers={"X-Xianyu-Desktop-Token": "launch-secret"}).json()
        self.assertTrue(batch["sound"])
        self.assertEqual(batch["test_count"], 1)
        self.configure(enabled=False)
        self.assertFalse(self.hub.poll(0, lambda *_: True)["sound"])

    def test_saved_preferences_survive_new_router_and_are_user_scoped(self):
        saved = {}
        store = Mock()
        store.get_user_setting.side_effect = lambda owner, key: saved.get((owner, key))
        def save(owner, key, value, description):
            saved[(owner, key)] = {"value": value}
            return True
        store.set_user_setting.side_effect = save
        def new_client():
            app = FastAPI()
            app.include_router(create_desktop_notifications_router(DesktopNotifications(), "secret", self.verify, store))
            return TestClient(app)
        client = new_client()
        auth = {"Authorization": "Bearer a"}
        endpoint = "/desktop/notifications/session"
        client.post(endpoint, headers=auth, json={"enabled": True, "sound": True})
        store.set_user_setting.assert_not_called()  # Heartbeats do not write.
        self.assertEqual(client.post(endpoint, headers=auth,
                                    json={"enabled": True, "sound": False, "save": True}).status_code, 200)
        restarted = new_client()
        self.assertEqual(restarted.get("/desktop/notifications/status", headers=auth).json()["preference"],
                         {"enabled": True, "sound": False})
        self.assertIsNone(restarted.get("/desktop/notifications/status", headers={"Authorization": "Bearer b"}).json()["preference"])
        store.set_user_setting.return_value = False
        store.set_user_setting.side_effect = None
        failed = client.post(endpoint, headers=auth, json={"enabled": False, "sound": False, "save": True})
        self.assertEqual(failed.status_code, 500)
        self.assertTrue(client.get("/desktop/notifications/status", headers=auth).json()["active"])
        saved[(1, "desktop_notification_preferences")] = {"value": "not-json"}
        self.assertIsNone(client.get("/desktop/notifications/status", headers=auth).json()["preference"])


class StartupImportTests(unittest.TestCase):
    def test_heavy_optional_modules_not_eagerly_imported(self):
        source = Path(__file__).resolve().parents[1] / "app/reply_server.py"
        tree = ast.parse(source.read_text())
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        modules = [name.name for node in imports if isinstance(node, ast.Import) for name in node.names]
        modules += [node.module for node in imports if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("pandas", modules)
        self.assertNotIn("app.ai_reply_engine", modules)


if __name__ == "__main__":
    unittest.main()
