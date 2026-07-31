from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import main
from stream_providers import (
    KickStreamProvider,
    StreamProviderError,
    TwitchStreamProvider,
    poll_creators_independently,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.request = AsyncMock(side_effect=self.responses)


KICK_ENVIRONMENT = {
    "KICK_INTEGRATION_ENABLED": "true",
    "KICK_CLIENT_ID": "fake-client-id",
    "KICK_CLIENT_SECRET": "fake-client-secret",
    "KICK_REDIRECT_URI": "https://app.invalid/api/kick/callback",
}


class KickProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_twitch_provider_delegates_without_behavior_change(self):
        expected = {"channel": "xqc", "is_live": True}
        loader = AsyncMock(return_value=expected)
        provider = TwitchStreamProvider(loader)
        result = await provider.get_live_status({"channel": "XQC"})
        self.assertIs(result, expected)
        loader.assert_awaited_once_with("xqc")

    async def test_kick_creator_resolution_uses_official_channels_endpoint(self):
        client = FakeClient([
            FakeResponse(payload={"access_token": "app-token", "expires_in": 3600}),
            FakeResponse(payload={"data": [{
                "broadcaster_user_id": 123,
                "slug": "kickcreator",
            }]}),
        ])
        provider = KickStreamProvider(client=client)
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False):
            creator = await provider.resolve_creator("@KickCreator")
        self.assertEqual(creator["platform_user_id"], "123")
        self.assertEqual(creator["platform_channel_slug"], "kickcreator")
        self.assertIn("/public/v1/channels", client.request.await_args_list[1].args[1])

    async def test_kick_live_response_normalizes_without_playback_source(self):
        client = FakeClient([
            FakeResponse(payload={"access_token": "app-token", "expires_in": 3600}),
            FakeResponse(payload={"data": [{
                "id": "fake-kick-stream",
                "broadcaster_user": {
                    "id": 123, "username": "kickcreator",
                    "profile_picture": "https://files.kick.com/avatar.webp",
                },
                "channel": {"slug": "kickcreator"},
                "category": {"id": 9, "name": "Just Chatting"},
                "language_code": "en",
                "started_at": "2026-07-31T12:00:00Z",
                "thumbnail": "https://files.kick.com/thumb.jpg",
                "title": "Official API test",
                "viewer_count": 42,
            }]}),
        ])
        provider = KickStreamProvider(client=client)
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False):
            result = await provider.get_live_status({
                "id": 7, "platform_user_id": "123",
                "platform_channel_slug": "kickcreator",
                "platform_display_name": "Kick Creator",
            })
        self.assertTrue(result["is_live"])
        self.assertEqual(result["stream_id"], "fake-kick-stream")
        self.assertEqual(result["category"], "Just Chatting")
        self.assertFalse(result["generation_available"])
        self.assertNotIn("playback_source", result)

    async def test_kick_offline_response_normalizes(self):
        client = FakeClient([
            FakeResponse(payload={"access_token": "app-token", "expires_in": 3600}),
            FakeResponse(payload={"data": []}),
        ])
        provider = KickStreamProvider(client=client)
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False):
            result = await provider.get_live_status({
                "platform_user_id": "123",
                "platform_channel_slug": "kickcreator",
            })
        self.assertFalse(result["is_live"])
        self.assertEqual(result["status"], "OFFLINE")

    async def test_disabled_kick_integration_makes_no_request(self):
        client = FakeClient([])
        provider = KickStreamProvider(client=client)
        with patch.dict(os.environ, {**KICK_ENVIRONMENT, "KICK_INTEGRATION_ENABLED": "false"}, clear=False):
            with self.assertRaises(StreamProviderError) as raised:
                await provider.resolve_creator("kickcreator")
        self.assertEqual(raised.exception.code, "disabled")
        client.request.assert_not_awaited()

    async def test_rate_limit_has_bounded_retry_metadata(self):
        client = FakeClient([FakeResponse(429, headers={"Retry-After": "30"})])
        provider = KickStreamProvider(client=client)
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False):
            with self.assertRaises(StreamProviderError) as raised:
                await provider.resolve_creator("kickcreator")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.retry_after, 30)

    async def test_one_provider_failure_does_not_stop_other_polling(self):
        failed = MagicMock()
        failed.get_live_status = AsyncMock(side_effect=RuntimeError("fake failure"))
        healthy = MagicMock()
        healthy.get_live_status = AsyncMock(return_value={
            "provider": "twitch", "channel": "healthy", "is_live": False,
        })
        results = await poll_creators_independently(
            [{"provider": "kick"}, {"provider": "twitch"}],
            {"kick": failed, "twitch": healthy},
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "ERROR")
        self.assertEqual(results[1]["provider"], "twitch")


class KickGenerationSafeguardTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_kick_generation_is_rejected_before_enqueue(self):
        with patch.object(main, "enqueue_generation_job") as enqueue, patch.object(
            main, "automatic_usage_snapshot"
        ) as quota:
            response = await main.auto_generate_clip({
                "provider": "kick", "creator_id": "fake-creator",
                "stream_id": "fake-stream",
            })
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"kick_playback_unavailable", response.body)
        enqueue.assert_not_called()
        quota.assert_not_called()

    async def test_scheduler_never_enqueues_kick_creator(self):
        kick_creator = {
            "id": 9, "provider": "kick", "channel": "kickcreator",
        }
        with patch.object(main, "load_creators", return_value=[kick_creator]), patch.object(
            main, "enqueue_eligible_automatic_job"
        ) as enqueue, patch.object(
            main, "automatic_usage_snapshot", return_value={}
        ):
            await main._run_smart_automatic_scheduler_pass()
        enqueue.assert_not_called()

    async def test_enabled_scheduler_polls_kick_without_enqueueing(self):
        kick_creator = {
            "id": 9, "provider": "kick", "channel": "kickcreator",
            "platform_user_id": "123",
        }
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False), patch.object(
            main, "load_creators", return_value=[kick_creator]
        ), patch.object(
            main.kick_stream_provider, "get_live_status",
            new=AsyncMock(return_value={"is_live": True, "stream_id": "kick-live"}),
        ) as status_check, patch.object(
            main, "enqueue_eligible_automatic_job"
        ) as enqueue, patch.object(
            main, "automatic_usage_snapshot", return_value={}
        ):
            await main._run_smart_automatic_scheduler_pass()
        status_check.assert_awaited_once_with(kick_creator)
        enqueue.assert_not_called()

    def test_token_encryption_does_not_store_plaintext(self):
        with patch.dict(os.environ, KICK_ENVIRONMENT, clear=False):
            encrypted = main._encrypt_kick_token("fake-sensitive-token")
            self.assertNotIn("fake-sensitive-token", encrypted)
            self.assertEqual(
                main._decrypt_kick_token(encrypted), "fake-sensitive-token",
            )

    def test_no_playback_scraping_or_stream_key_scope_exists(self):
        source = (
            Path(__file__).resolve().parent / "stream_providers.py"
        ).read_text(encoding="utf-8").lower()
        self.assertNotIn("streamkey:read", source)
        self.assertNotIn("player.kick.com", source)
        self.assertNotIn("beautifulsoup", source)
        self.assertNotIn("selenium", source)

    def test_creator_uniqueness_is_provider_scoped(self):
        source = (Path(__file__).resolve().parent / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ON monitored_creators (provider, platform_user_id)", source
        )
        self.assertIn(
            "ON monitored_creators (provider, platform_channel_slug)", source
        )
        self.assertNotIn("login TEXT NOT NULL UNIQUE", source)


if __name__ == "__main__":
    unittest.main()
