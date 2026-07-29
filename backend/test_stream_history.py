from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import main
import stream_history


class StreamPlanningTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.live = {
            "stream_id": "live-1",
            "is_live": True,
            "started_at": self.now - timedelta(hours=1),
            "title": "Live",
        }
        self.channel = {
            "is_live": True,
            "stream_id": "live-1",
            "started_at": self.live["started_at"].isoformat(),
            "title": "Live",
            "completed_streams": [
                {
                    "stream_id": "vod-1",
                    "video_id": "video-1",
                    "started_at": self.now - timedelta(days=1),
                    "ended_at": self.now - timedelta(hours=22),
                    "is_live": False,
                },
                {
                    "stream_id": "vod-2",
                    "video_id": "video-2",
                    "started_at": self.now - timedelta(days=2),
                    "ended_at": self.now - timedelta(days=2, hours=-2),
                    "is_live": False,
                },
            ],
        }

    def test_every_job_selects_newest_stream_first(self):
        with patch.object(
            main,
            "register_newest_stream",
            return_value={"last_checked_at": None},
        ) as register:
            target = main._select_stream_search_target(
                "creator-1",
                self.channel,
                historical_only=False,
            )
        self.assertEqual(target["stream_id"], "live-1")
        self.assertTrue(target["_is_newest"])
        register.assert_called_once()

    def test_unchanged_newest_resumes_after_last_checked_time(self):
        checked_at = self.now - timedelta(minutes=5)
        target = {
            "stream_id": "live-1",
            "started_at": self.now - timedelta(hours=1),
            "_is_newest": True,
            "_stream_state": {"last_checked_at": checked_at},
        }
        self.assertEqual(
            main._stream_discovery_start(target),
            checked_at - timedelta(seconds=1),
        )

    def test_partial_stream_without_completed_check_rescans_from_start(self):
        target = {
            "stream_id": "vod-1",
            "started_at": self.now - timedelta(days=1),
            "_is_newest": False,
            "_stream_state": {
                "last_checked_at": self.now - timedelta(minutes=5),
                "processing_state": "partial",
            },
        }
        self.assertEqual(
            main._stream_discovery_start(target),
            target["started_at"],
        )

    def test_exhausted_streams_do_not_consume_historical_selection(self):
        with patch.object(main, "register_newest_stream", return_value={}):
            with patch.object(main, "get_historical_cursor", return_value={}):
                with patch.object(
                    main,
                    "get_exhausted_stream_ids",
                    return_value={"vod-1"},
                ):
                    with patch.object(
                        main,
                        "get_stream_state",
                        return_value={"processing_state": "partial"},
                    ):
                        target = main._select_stream_search_target(
                            "creator-1",
                            self.channel,
                            historical_only=True,
                        )
        self.assertEqual(target["stream_id"], "vod-2")

    def test_historical_search_resumes_strictly_before_saved_cursor(self):
        cursor = {
            "next_before_timestamp": self.channel["completed_streams"][0][
                "started_at"
            ],
            "last_stream_id": "vod-1",
        }
        with patch.object(main, "register_newest_stream", return_value={}):
            with patch.object(main, "get_historical_cursor", return_value=cursor):
                with patch.object(
                    main, "get_exhausted_stream_ids", return_value=set()
                ):
                    with patch.object(
                        main,
                        "get_stream_state",
                        return_value={"processing_state": "pending"},
                    ):
                        target = main._select_stream_search_target(
                            "creator-1",
                            self.channel,
                            historical_only=True,
                        )
        self.assertEqual(target["stream_id"], "vod-2")

    def test_successful_newest_is_still_refreshable_next_job(self):
        with patch.object(
            main,
            "register_newest_stream",
            return_value={"processing_state": "succeeded"},
        ):
            first = main._select_stream_search_target(
                "creator-1", self.channel, historical_only=False
            )
            second = main._select_stream_search_target(
                "creator-1", self.channel, historical_only=False
            )
        self.assertEqual(first["stream_id"], second["stream_id"])
        self.assertTrue(second["_is_newest"])


class StreamDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_material_in_same_live_stream_is_returned(self):
        now = datetime.now(timezone.utc)
        checked_at = now - timedelta(minutes=5)
        captured_params = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, headers, params):
                captured_params.append(dict(params))
                return SimpleNamespace(
                    status_code=200,
                    text="",
                    json=lambda: {
                        "data": [
                            {
                                "id": "FakeNewMaterial",
                                "url": "https://clips.twitch.tv/FakeNewMaterial",
                                "created_at": now.isoformat(),
                                "duration": 30,
                            }
                        ],
                        "pagination": {},
                    },
                )

        target = {
            "stream_id": "live-1",
            "video_id": "",
            "started_at": now - timedelta(hours=1),
            "is_live": True,
            "_is_newest": True,
            "_stream_state": {"last_checked_at": checked_at},
        }
        with patch.object(
            main,
            "get_twitch_access_token",
            return_value="fake-token",
        ):
            with patch.object(main.httpx, "AsyncClient", return_value=Client()):
                with patch.object(
                    main,
                    "_clip_history_upsert",
                    return_value=True,
                ):
                    clips, complete, _ = (
                        await main.fetch_twitch_clips_for_stream(
                            "creator-1",
                            target,
                            set(),
                            set(),
                            3,
                        )
                    )
        self.assertEqual([clip["id"] for clip in clips], ["FakeNewMaterial"])
        self.assertTrue(complete)
        self.assertEqual(
            captured_params[0]["started_at"],
            (checked_at - timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )

    async def test_unchanged_live_stream_returns_no_candidates(self):
        now = datetime.now(timezone.utc)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, headers, params):
                return SimpleNamespace(
                    status_code=200,
                    text="",
                    json=lambda: {"data": [], "pagination": {}},
                )

        target = {
            "stream_id": "live-1",
            "video_id": "",
            "started_at": now - timedelta(hours=2),
            "is_live": True,
            "_is_newest": True,
            "_stream_state": {"last_checked_at": now - timedelta(minutes=5)},
        }
        with patch.object(
            main,
            "get_twitch_access_token",
            return_value="fake-token",
        ):
            with patch.object(main.httpx, "AsyncClient", return_value=Client()):
                clips, complete, _ = await main.fetch_twitch_clips_for_stream(
                    "creator-1", target, set(), set(), 3
                )
        self.assertEqual(clips, [])
        self.assertTrue(complete)


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for stream-history integration tests",
)
class PostgreSQLStreamHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql

        cls.database_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_stream_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(cls.database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(
                        sql.Identifier(cls.schema)
                    )
                )
        cls.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": cls.database_url,
                "PGOPTIONS": f"-c search_path={cls.schema}",
            },
        )
        cls.environment.start()
        cls.lock_patch = patch.object(
            stream_history,
            "STREAM_HISTORY_ADVISORY_LOCK_ID",
            int(uuid.uuid4().int % 2_000_000_000),
        )
        cls.lock_patch.start()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        cls.lock_patch.stop()
        cls.environment.stop()
        with psycopg.connect(cls.database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(cls.schema)
                    )
                )

    def setUp(self):
        self.assertTrue(stream_history.ensure_stream_history_tables())
        self.assertTrue(stream_history.ensure_stream_history_tables())
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE auto_clip_stream_state")
                cursor.execute("TRUNCATE auto_clip_historical_cursor")
            connection.commit()

    def test_newer_stream_demotes_former_newest_without_exhausting_it(self):
        old = {
            "stream_id": "fake-old",
            "started_at": self.now - timedelta(days=1),
            "ended_at": self.now - timedelta(hours=22),
        }
        new = {
            "stream_id": "fake-new",
            "started_at": self.now,
            "ended_at": None,
        }
        stream_history.register_newest_stream("fake-creator", old)
        stream_history.update_stream_progress(
            "fake-creator",
            "fake-old",
            processing_state="partial",
            retryable_failure_state="temporary_download_failure",
        )
        stream_history.register_newest_stream("fake-creator", new)
        old_state = stream_history.get_stream_state(
            "fake-creator", "fake-old"
        )
        self.assertFalse(old_state["is_refreshable"])
        self.assertEqual(old_state["processing_state"], "partial")
        cursor = stream_history.get_historical_cursor("fake-creator")
        self.assertEqual(cursor["next_before_timestamp"], new["started_at"])

    def test_retryable_failure_remains_partial_and_not_exhausted(self):
        stream = {
            "stream_id": "fake-retryable",
            "started_at": self.now - timedelta(days=1),
            "ended_at": self.now - timedelta(hours=22),
        }
        stream_history.register_newest_stream("fake-creator", stream)
        stream_history.update_stream_progress(
            "fake-creator",
            "fake-retryable",
            processing_state="partial",
            retryable_failure_state="temporary_transcription_failure",
        )
        state = stream_history.get_stream_state(
            "fake-creator", "fake-retryable"
        )
        self.assertEqual(state["processing_state"], "partial")
        self.assertEqual(
            state["retryable_failure_state"],
            "temporary_transcription_failure",
        )
        self.assertNotIn(
            "fake-retryable",
            stream_history.get_exhausted_stream_ids("fake-creator"),
        )

    def test_exhausted_state_is_terminal_for_older_stream(self):
        old = {
            "stream_id": "fake-old",
            "started_at": self.now - timedelta(days=1),
            "ended_at": self.now - timedelta(hours=22),
        }
        new = {
            "stream_id": "fake-new",
            "started_at": self.now,
            "ended_at": None,
        }
        stream_history.register_newest_stream("fake-creator", old)
        stream_history.register_newest_stream("fake-creator", new)
        stream_history.update_stream_progress(
            "fake-creator", "fake-old", processing_state="exhausted",
            range_end=old["ended_at"], checked_complete=True,
        )
        stream_history.update_stream_progress(
            "fake-creator", "fake-old", processing_state="partial",
            retryable_failure_state="late-retry",
        )
        state = stream_history.get_stream_state("fake-creator", "fake-old")
        self.assertEqual(state["processing_state"], "exhausted")
        self.assertIsNone(state["retryable_failure_state"])
        self.assertEqual(state["last_checked_at"], old["ended_at"])
        self.assertIn(
            "fake-old",
            stream_history.get_exhausted_stream_ids("fake-creator"),
        )

    def test_concurrent_cursor_updates_cannot_move_back_to_newer_stream(self):
        newer = self.now - timedelta(days=1)
        older = self.now - timedelta(days=2)
        errors = []

        def save(timestamp, stream_id):
            try:
                stream_history.save_historical_cursor(
                    "fake-creator",
                    next_before_timestamp=timestamp,
                    last_stream_id=stream_id,
                )
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=save, args=(newer, "newer")),
            threading.Thread(target=save, args=(older, "older")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        cursor = stream_history.get_historical_cursor("fake-creator")
        self.assertEqual(cursor["next_before_timestamp"], older)
        self.assertEqual(cursor["last_stream_id"], "older")


if __name__ == "__main__":
    unittest.main()
