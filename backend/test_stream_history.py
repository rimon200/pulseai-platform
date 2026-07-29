from __future__ import annotations

import contextlib
import io
import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
                    return_value=False,
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


class StreamTraversalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.creator = {
            "name": "Fake Streamer",
            "channel": "fake_streamer",
        }
        self.channel = {
            "is_live": True,
            "stream_id": "live-newest",
            "user_id": "fake-creator",
            "started_at": (self.now - timedelta(hours=1)).isoformat(),
            "title": "Current live stream",
            "completed_streams": [
                {
                    "stream_id": "vod-newer",
                    "video_id": "video-newer",
                    "started_at": self.now - timedelta(days=1),
                    "ended_at": self.now - timedelta(hours=22),
                    "is_live": False,
                },
                {
                    "stream_id": "vod-older",
                    "video_id": "video-older",
                    "started_at": self.now - timedelta(days=2),
                    "ended_at": self.now - timedelta(hours=46),
                    "is_live": False,
                },
            ],
        }

    def _pipeline_patches(self, fetch_side_effect):
        stack = contextlib.ExitStack()
        cursor = {"value": None}
        exhausted = set()

        def get_cursor(_creator_id):
            return (
                {
                    "next_before_timestamp": cursor["value"],
                    "last_stream_id": "saved",
                }
                if cursor["value"] is not None
                else {}
            )

        def update_progress(_creator_id, stream_id, **values):
            if values.get("processing_state") == "exhausted":
                exhausted.add(stream_id)
            return True

        def exhaust_and_save_cursor(
            _creator_id,
            stream_id,
            *,
            range_end,
            next_before_timestamp,
        ):
            del range_end
            exhausted.add(stream_id)
            cursor["value"] = next_before_timestamp

        stack.enter_context(
            patch.object(main, "load_creators", return_value=[self.creator])
        )
        stack.enter_context(
            patch.object(
                main,
                "get_twitch_channel_data",
                new=AsyncMock(return_value=self.channel),
            )
        )
        stack.enter_context(patch.object(main, "_load_creator_cursor", return_value=0))
        stack.enter_context(patch.object(main, "_save_creator_cursor"))
        stack.enter_context(
            patch.object(main, "register_newest_stream", return_value={})
        )
        stack.enter_context(
            patch.object(
                main,
                "register_historical_stream",
                return_value={"processing_state": "pending"},
            )
        )
        stack.enter_context(patch.object(main, "get_stream_state", return_value=None))
        stack.enter_context(
            patch.object(
                main,
                "get_exhausted_stream_ids",
                side_effect=lambda _creator_id: set(exhausted),
            )
        )
        stack.enter_context(
            patch.object(main, "get_historical_cursor", side_effect=get_cursor)
        )
        stack.enter_context(
            patch.object(main, "update_stream_progress", side_effect=update_progress)
        )
        stack.enter_context(
            patch.object(
                main,
                "exhaust_stream_and_advance_cursor",
                side_effect=lambda creator_id, stream_id, range_start, range_end: (
                    exhaust_and_save_cursor(
                        creator_id,
                        stream_id,
                        range_end=range_end,
                        next_before_timestamp=range_start,
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(
                main,
                "_load_clip_history_exclusions",
                return_value=(set(), set(), True),
            )
        )
        fetch = stack.enter_context(
            patch.object(
                main,
                "fetch_twitch_clips_for_stream",
                new=AsyncMock(side_effect=fetch_side_effect),
            )
        )
        stack.enter_context(
            patch.object(main, "_log_memory_check")
        )
        stack.enter_context(
            patch.object(main, "_log_performance_timing")
        )
        main.app.state.clip_history_ready = True
        main.app.state.current_stream_grace_active = False
        return stack, fetch, exhausted, cursor

    async def test_current_failure_traverses_multiple_historical_streams(self):
        searched_streams = []

        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            searched_streams.append(stream_target["stream_id"])
            main.app.state.current_stream_grace_active = False
            return [], True, ""

        stack, _, exhausted, cursor = self._pipeline_patches(fetch)
        captured = io.StringIO()
        with stack:
            with contextlib.redirect_stdout(captured):
                result = await main._run_auto_generate_clip_pipeline()

        self.assertEqual(
            searched_streams,
            ["live-newest", "vod-newer", "vod-older"],
        )
        self.assertEqual(exhausted, {"vod-newer", "vod-older"})
        self.assertEqual(
            cursor["value"],
            self.channel["completed_streams"][1]["started_at"],
        )
        self.assertEqual(result["outcome_reason"], "no_more_streams")
        output = captured.getvalue()
        self.assertIn("mode=current", output)
        self.assertEqual(output.count("mode=historical"), 2)
        self.assertIn(
            "STREAM SEARCH COMPLETE | streams_checked=3 | "
            "clip_created=false | reason=no_more_streams",
            output,
        )

    async def test_current_failure_stops_after_recursive_clip_creation(self):
        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            self.assertEqual(stream_target["stream_id"], "live-newest")
            return [], True, ""

        stack, _, _, _ = self._pipeline_patches(fetch)
        original_pipeline = main._run_auto_generate_clip_pipeline
        recursive_result = {"id": "generated-from-history"}
        with stack:
            with patch.object(
                main,
                "_run_auto_generate_clip_pipeline",
                new=AsyncMock(return_value=recursive_result),
            ) as recursive_call:
                result = await original_pipeline()
        self.assertEqual(result, recursive_result)
        recursive_call.assert_awaited_once()
        self.assertTrue(
            recursive_call.await_args.kwargs["_historical_only"]
        )

    async def test_incomplete_current_discovery_still_enters_history(self):
        searched_streams = []

        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            searched_streams.append(stream_target["stream_id"])
            return [], stream_target["stream_id"] != "live-newest", ""

        stack, _, exhausted, _ = self._pipeline_patches(fetch)
        with stack:
            result = await main._run_auto_generate_clip_pipeline()
        self.assertEqual(
            searched_streams,
            [
                "live-newest",
                "live-newest",
                "vod-newer",
                "vod-older",
            ],
        )
        self.assertEqual(exhausted, {"vod-newer", "vod-older"})
        self.assertEqual(
            result["outcome_reason"],
            "partial_streams_remaining",
        )
        self.assertNotIn("No suitable clip", result["message"])

    async def test_exhausted_historical_stream_is_skipped_forever(self):
        searched_streams = []
        exhausted = {"vod-newer"}

        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            searched_streams.append(stream_target["stream_id"])
            return [], True, ""

        stack, _, _, _ = self._pipeline_patches(fetch)
        stack.enter_context(
            patch.object(
                main,
                "get_exhausted_stream_ids",
                side_effect=lambda _creator_id: set(exhausted),
            )
        )
        with stack:
            await main._run_auto_generate_clip_pipeline()
        self.assertEqual(searched_streams, ["live-newest", "vod-older"])

    async def test_partial_historical_stream_holds_durable_cursor(self):
        searched_streams = []

        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            stream_id = stream_target["stream_id"]
            searched_streams.append(stream_id)
            return [], stream_id != "vod-newer", ""

        stack, _, exhausted, cursor = self._pipeline_patches(fetch)
        with stack:
            result = await main._run_auto_generate_clip_pipeline()
        self.assertEqual(
            searched_streams,
            [
                "live-newest",
                "vod-newer",
                "vod-newer",
                "vod-older",
            ],
        )
        self.assertNotIn("vod-newer", exhausted)
        self.assertIn("vod-older", exhausted)
        self.assertIsNone(cursor["value"])
        self.assertEqual(
            result["outcome_reason"],
            "partial_streams_remaining",
        )

    async def test_essential_exhaustion_failure_preserves_cursor(self):
        searched_streams = []

        async def fetch(broadcaster_id, stream_target, **_kwargs):
            del broadcaster_id
            searched_streams.append(stream_target["stream_id"])
            return [], True, ""

        stack, _, exhausted, cursor = self._pipeline_patches(fetch)
        stack.enter_context(
            patch.object(
                main,
                "exhaust_stream_and_advance_cursor",
                side_effect=RuntimeError("fake cursor outage"),
            )
        )
        captured = io.StringIO()
        with stack:
            with contextlib.redirect_stdout(captured):
                result = await main._run_auto_generate_clip_pipeline()
        self.assertEqual(searched_streams, ["live-newest", "vod-newer"])
        self.assertNotIn("vod-newer", exhausted)
        self.assertIsNone(cursor["value"])
        self.assertEqual(
            result["outcome_reason"],
            "stream_state_persistence_failed",
        )
        self.assertIn("result=partial", captured.getvalue())


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

    def test_historical_cursor_survives_worker_restart_connections(self):
        saved_timestamp = self.now - timedelta(days=3)
        stream_history.save_historical_cursor(
            "fake-restarted-creator",
            next_before_timestamp=saved_timestamp,
            last_stream_id="fake-restart-stream",
        )
        first_worker_view = stream_history.get_historical_cursor(
            "fake-restarted-creator"
        )
        second_worker_view = stream_history.get_historical_cursor(
            "fake-restarted-creator"
        )
        self.assertEqual(
            first_worker_view["next_before_timestamp"],
            saved_timestamp,
        )
        self.assertEqual(second_worker_view, first_worker_view)

    def test_atomic_exhaustion_rolls_back_when_cursor_write_fails(self):
        old = {
            "stream_id": "fake-atomic-old",
            "started_at": self.now - timedelta(days=1),
            "ended_at": self.now - timedelta(hours=22),
        }
        new = {
            "stream_id": "fake-atomic-new",
            "started_at": self.now,
        }
        stream_history.register_newest_stream("fake-atomic-creator", old)
        stream_history.register_newest_stream("fake-atomic-creator", new)
        cursor_before = stream_history.get_historical_cursor(
            "fake-atomic-creator"
        )
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE FUNCTION fail_fake_cursor_write()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        RAISE EXCEPTION 'fake cursor failure';
                    END
                    $$
                    """
                )
                cursor.execute(
                    """
                    CREATE TRIGGER fail_fake_cursor_write_trigger
                    BEFORE INSERT OR UPDATE
                    ON auto_clip_historical_cursor
                    FOR EACH ROW EXECUTE FUNCTION fail_fake_cursor_write()
                    """
                )
            connection.commit()
        try:
            with self.assertRaises(Exception):
                stream_history.exhaust_stream_and_advance_cursor(
                    "fake-atomic-creator",
                    "fake-atomic-old",
                    range_start=old["started_at"],
                    range_end=old["ended_at"],
                )
        finally:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DROP TRIGGER fail_fake_cursor_write_trigger
                        ON auto_clip_historical_cursor
                        """
                    )
                    cursor.execute("DROP FUNCTION fail_fake_cursor_write()")
                connection.commit()
        old_state = stream_history.get_stream_state(
            "fake-atomic-creator",
            "fake-atomic-old",
        )
        cursor_after = stream_history.get_historical_cursor(
            "fake-atomic-creator"
        )
        self.assertNotEqual(old_state["processing_state"], "exhausted")
        self.assertEqual(cursor_after, cursor_before)


if __name__ == "__main__":
    unittest.main()
