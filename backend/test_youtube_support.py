from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import generation_jobs
from stream_providers import StreamProviderError, TwitchStreamProvider, YouTubeUploadProvider
from youtube_uploads import (
    YouTubeSourceError,
    classify_youtube_upload,
    claim_youtube_upload,
    ensure_youtube_tables,
    select_clip_candidates,
    validate_authorized_media_url,
)


YOUTUBE_ENV = {
    "YOUTUBE_INTEGRATION_ENABLED": "true",
    "YOUTUBE_API_KEY": "fake-api-key",
    "YOUTUBE_MIN_VIDEO_DURATION_MINUTES": "12",
    "YOUTUBE_CLIPS_PER_VIDEO": "3",
    "YOUTUBE_MAX_CLIPS_PER_VIDEO": "5",
    "YOUTUBE_CLIP_MIN_SECONDS": "45",
    "YOUTUBE_CLIP_TARGET_SECONDS": "90",
    "YOUTUBE_CLIP_MAX_SECONDS": "300",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.get = AsyncMock(side_effect=list(responses))


class YouTubeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_twitch_provider_behavior_is_unchanged(self):
        loader = AsyncMock(return_value={"channel": "same", "is_live": True})
        result = await TwitchStreamProvider(loader).get_live_status({"channel": "same"})
        self.assertTrue(result["is_live"])
        loader.assert_awaited_once_with("same")

    async def test_channel_and_upload_playlist_resolution(self):
        client = FakeClient([FakeResponse(payload={"items": [{
            "id": "UCabcdefghijklmnopqrstuv",
            "snippet": {
                "title": "Authorized Creator", "customUrl": "@authorized",
                "thumbnails": {"high": {"url": "https://images.example/avatar.jpg"}},
            },
            "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}},
        }]})])
        provider = YouTubeUploadProvider(client=client)
        with patch.dict(os.environ, YOUTUBE_ENV, clear=False):
            result = await provider.resolve_channel("@authorized")
        self.assertEqual(result["platform_user_id"], "UCabcdefghijklmnopqrstuv")
        self.assertEqual(result["uploads_playlist_id"], "UUuploads")
        self.assertEqual(client.get.await_args.kwargs["params"]["forHandle"], "@authorized")
        self.assertEqual(client.get.await_args.kwargs["params"]["key"], "fake-api-key")

    async def test_new_upload_detection_retrieves_video_metadata(self):
        client = FakeClient([
            FakeResponse(payload={"items": [
                {"contentDetails": {"videoId": "video-one"}},
                {"contentDetails": {"videoId": "video-two"}},
            ]}),
            FakeResponse(payload={"items": [{
                "id": "video-one",
                "snippet": {
                    "title": "Long form", "description": "Allowed",
                    "publishedAt": "2026-07-31T12:00:00Z",
                    "channelTitle": "Authorized Creator",
                    "liveBroadcastContent": "none",
                    "thumbnails": {"high": {"url": "https://images.example/v.jpg"}},
                },
                "contentDetails": {"duration": "PT18M5S"},
                "status": {"privacyStatus": "public", "uploadStatus": "processed"},
            }]})
        ])
        provider = YouTubeUploadProvider(client=client)
        with patch.dict(os.environ, YOUTUBE_ENV, clear=False):
            uploads = await provider.check_new_uploads(
                {"uploads_playlist_id": "UUuploads"}
            )
        self.assertEqual(len(uploads), 2)
        self.assertEqual(uploads[0]["duration_seconds"], 1085)
        self.assertEqual(uploads[1]["privacy_status"], "unavailable")

    async def test_disabled_integration_makes_no_request(self):
        client = FakeClient([])
        provider = YouTubeUploadProvider(client=client)
        with patch.dict(os.environ, {**YOUTUBE_ENV, "YOUTUBE_INTEGRATION_ENABLED": "false"}):
            with self.assertRaises(StreamProviderError) as raised:
                await provider.resolve_channel("@authorized")
        self.assertEqual(raised.exception.code, "disabled")
        client.get.assert_not_awaited()

    async def test_quota_error_is_typed(self):
        client = FakeClient([FakeResponse(403, {
            "error": {"errors": [{"reason": "quotaExceeded"}]}
        })])
        provider = YouTubeUploadProvider(client=client)
        with patch.dict(os.environ, YOUTUBE_ENV, clear=False):
            with self.assertRaises(StreamProviderError) as raised:
                await provider.resolve_channel("@authorized")
        self.assertEqual(raised.exception.code, "quota_exceeded")


class YouTubeSourceAndSelectionTests(unittest.TestCase):
    def test_long_form_qualifies_and_short_upcoming_private_skip(self):
        base = {
            "privacy_status": "public", "upload_status": "processed",
            "live_broadcast_content": "none",
        }
        with patch.dict(os.environ, YOUTUBE_ENV, clear=False):
            self.assertEqual(
                classify_youtube_upload({**base, "duration_seconds": 1200}),
                ("eligible", "detected"),
            )
            self.assertEqual(
                classify_youtube_upload({**base, "duration_seconds": 60}),
                ("video_too_short", "skipped"),
            )
            self.assertEqual(
                classify_youtube_upload({
                    **base, "duration_seconds": 1200,
                    "live_broadcast_content": "upcoming",
                }),
                ("livestream_placeholder", "skipped"),
            )
            self.assertEqual(
                classify_youtube_upload({
                    **base, "duration_seconds": 1200,
                    "privacy_status": "private",
                }),
                ("private_or_deleted", "skipped"),
            )

    def test_private_and_youtube_hosts_are_rejected(self):
        with self.assertRaises(YouTubeSourceError):
            validate_authorized_media_url(
                "https://media.example/video.mp4",
                allowed_hosts={"media.example"}, resolved_addresses=["127.0.0.1"],
            )
        with self.assertRaises(YouTubeSourceError):
            validate_authorized_media_url(
                "https://www.youtube.com/watch?v=fake",
                allowed_hosts={"www.youtube.com"}, resolved_addresses=["8.8.8.8"],
            )

    def test_approved_public_source_host_is_accepted(self):
        result = validate_authorized_media_url(
            "https://media.creator.example/video.mp4",
            allowed_hosts={"media.creator.example"},
            resolved_addresses=["8.8.8.8"],
        )
        self.assertEqual(result, "https://media.creator.example/video.mp4")

    def test_default_selection_is_three_nonoverlapping_bounded_clips(self):
        segments = []
        for index in range(90):
            start = 40 + index * 20
            segments.append({
                "start": start, "end": start + 12,
                "text": f"This is a crazy moment number {index}! What happens next?",
            })
        with patch.dict(os.environ, YOUTUBE_ENV, clear=False):
            selected = select_clip_candidates(segments, 2400)
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(45 <= item["duration_seconds"] <= 300 for item in selected))
        for left, right in zip(selected, selected[1:]):
            overlap = min(left["end_seconds"], right["end_seconds"]) - max(
                left["start_seconds"], right["start_seconds"]
            )
            self.assertLessEqual(
                overlap,
                0.35 * min(left["duration_seconds"], right["duration_seconds"]),
            )

    def test_no_scraping_player_or_automatic_tiktok_code(self):
        sources = "\n".join(
            (Path(__file__).parent / name).read_text(encoding="utf-8").lower()
            for name in ("stream_providers.py", "youtube_uploads.py", "youtube_generation.py")
        )
        self.assertNotIn("youtube-dl", sources)
        self.assertNotIn("yt-dlp", sources)
        self.assertNotIn("player_response", sources)
        self.assertNotIn("publish_clip_to_tiktok", sources)
        main_source = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        self.assertIn("COALESCE(source_platform, provider) <> 'youtube'", main_source)


class YouTubeGenerationSafeguardTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_not_configured_blocks_before_quota_or_job(self):
        upload = {
            "id": "fake-upload", "creator_id": 7,
            "platform_video_id": "fake-video", "source_status": "not_configured",
            "authorized_media_source_type": None,
            "authorized_media_source_config_encrypted": None,
        }
        with patch.object(main, "DATABASE_URL", "postgresql://configured"), patch.object(
            main, "get_youtube_upload", return_value=upload
        ), patch.object(main, "enqueue_generation_job") as enqueue, patch.object(
            main, "automatic_usage_snapshot"
        ) as quota:
            response = await main.auto_generate_clip({
                "provider": "youtube", "upload_id": "fake-upload",
            })
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"youtube_source_not_configured", response.body)
        enqueue.assert_not_called()
        quota.assert_not_called()


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for YouTube PostgreSQL tests",
)
class YouTubePostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        cls.psycopg = psycopg
        cls.sql = sql
        cls.base_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_youtube_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(cls.base_url, options="-c search_path=pg_catalog") as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
            connection.commit()
        cls.schema_url = make_conninfo(cls.base_url, options=f"-csearch_path={cls.schema}")
        cls.environment = patch.dict(
            os.environ,
            {"DATABASE_URL": cls.schema_url, "PGOPTIONS": f"-c search_path={cls.schema}"},
        )
        cls.environment.start()
        cls.database_patch = patch.object(main, "DATABASE_URL", cls.schema_url)
        cls.database_patch.start()
        with psycopg.connect(cls.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE monitored_creators (
                        id BIGSERIAL PRIMARY KEY,
                        twitch_user_id TEXT,
                        login TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        priority INTEGER NOT NULL DEFAULT 0,
                        notes TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        provider TEXT NOT NULL DEFAULT 'twitch',
                        platform_user_id TEXT,
                        platform_channel_slug TEXT
                    )
                    """
                )
            connection.commit()
        if not ensure_youtube_tables(cls.schema_url):
            raise RuntimeError("isolated YouTube migration failed")
        if not generation_jobs.ensure_generation_jobs_table():
            raise RuntimeError("isolated generation-job migration failed")

    @classmethod
    def tearDownClass(cls):
        cls.database_patch.stop()
        cls.environment.stop()
        with cls.psycopg.connect(cls.base_url, options="-c search_path=pg_catalog") as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    cls.sql.SQL("DROP SCHEMA {} CASCADE").format(
                        cls.sql.Identifier(cls.schema)
                    )
                )
            connection.commit()

    def test_migration_idempotency_unique_video_and_stale_claim_recovery(self):
        self.assertTrue(ensure_youtube_tables(self.schema_url))
        upload_id = uuid.uuid4()
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO monitored_creators (
                        provider, platform_user_id, platform_channel_slug,
                        login, display_name, authorized_media_source_type
                    ) VALUES ('youtube', 'UCfake', 'same_name', 'same_name',
                              'Same Name', 'manual_upload') RETURNING id
                    """
                )
                creator_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO youtube_uploads (
                        id, creator_id, platform_video_id, title,
                        duration_seconds, source_status
                    ) VALUES (%s, %s, 'fake-video', 'Fake long upload', 1200, 'ready')
                    """,
                    (upload_id, creator_id),
                )
            connection.commit()
        first = claim_youtube_upload(self.schema_url, str(upload_id), "worker-one")
        self.assertEqual(first["claimed_by"], "worker-one")
        self.assertIsNone(
            claim_youtube_upload(self.schema_url, str(upload_id), "worker-two")
        )
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE youtube_uploads SET claim_expires_at = NOW() - INTERVAL '1 minute'
                    WHERE id = %s
                    """,
                    (upload_id,),
                )
            connection.commit()
        recovered = claim_youtube_upload(self.schema_url, str(upload_id), "worker-two")
        self.assertEqual(recovered["claimed_by"], "worker-two")
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                with self.assertRaises(self.psycopg.errors.UniqueViolation):
                    cursor.execute(
                        """
                        INSERT INTO youtube_uploads (
                            id, creator_id, platform_video_id, title
                        ) VALUES (%s, %s, 'fake-video', 'Duplicate')
                        """,
                        (uuid.uuid4(), creator_id),
                    )

    def test_youtube_generation_job_is_idempotent_per_upload(self):
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM youtube_uploads LIMIT 1")
                upload_id = cursor.fetchone()[0]
        first, first_created = generation_jobs.enqueue_generation_job(
            "manual", "same_name", provider="youtube",
            source_upload_id=str(upload_id),
        )
        second, second_created = generation_jobs.enqueue_generation_job(
            "manual", "same_name", provider="youtube",
            source_upload_id=str(upload_id),
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
