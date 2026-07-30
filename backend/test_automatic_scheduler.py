from __future__ import annotations

import contextlib
import io
import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import generation_jobs
import main


class AutomaticSchedulerUnitTests(unittest.IsolatedAsyncioTestCase):
    def _live_channel(self, age_seconds: int = 600) -> dict[str, object]:
        return {
            "is_live": True,
            "stream_id": "fake-live-stream",
            "user_id": "fake-creator-id",
            "started_at": (
                datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            ).isoformat(),
        }

    def test_polling_without_new_material_creates_no_job(self):
        channel = self._live_channel(599)
        with patch.object(main, "get_stream_state", return_value=None):
            _, _, seconds = main._automatic_stream_material(channel, {})
        self.assertLess(seconds, 600)

    async def test_ten_minutes_of_new_material_creates_one_job(self):
        channel = self._live_channel(601)
        job = {"id": "fake-job"}
        with (
            patch.object(
                main,
                "load_creators",
                return_value=[{"channel": "fake_creator"}],
            ),
            patch.object(
                main,
                "get_twitch_channel_data",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(main, "get_stream_state", return_value=None),
            patch.object(
                main,
                "automatic_usage_snapshot",
                side_effect=[
                    {
                        "creator_clips_created": 0,
                        "clips_created": 0,
                        "recent_average_bytes": 10 * 1024 * 1024,
                        "creator_state": {},
                    },
                    {
                        "jobs_enqueued": 1,
                        "clips_created": 0,
                        "estimated_outbound_bytes": 10 * 1024 * 1024,
                        "actual_outbound_bytes": 0,
                    },
                ],
            ),
            patch.object(
                main,
                "enqueue_eligible_automatic_job",
                return_value=(job, "new_material", {}),
            ) as enqueue,
        ):
            await main._run_smart_automatic_scheduler_pass()
        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.kwargs["stream_id"],
            "fake-live-stream",
        )

    async def test_no_material_records_skip_without_enqueue(self):
        channel = self._live_channel(300)
        with (
            patch.object(
                main,
                "load_creators",
                return_value=[{"channel": "fake_creator"}],
            ),
            patch.object(
                main,
                "get_twitch_channel_data",
                new=AsyncMock(return_value=channel),
            ),
            patch.object(main, "get_stream_state", return_value=None),
            patch.object(
                main,
                "automatic_usage_snapshot",
                side_effect=[
                    {
                        "creator_clips_created": 0,
                        "clips_created": 0,
                        "creator_state": {},
                    },
                    {
                        "jobs_enqueued": 0,
                        "clips_created": 0,
                        "estimated_outbound_bytes": 0,
                        "actual_outbound_bytes": 0,
                    },
                ],
            ),
            patch.object(main, "record_automatic_skip") as skip,
            patch.object(main, "enqueue_eligible_automatic_job") as enqueue,
        ):
            await main._run_smart_automatic_scheduler_pass()
        skip.assert_called_once_with(
            "fake-creator-id",
            "fake_creator",
            "no_new_material",
        )
        enqueue.assert_not_called()

    def test_evaluated_range_is_not_enqueued_again(self):
        now = datetime.now(timezone.utc)
        channel = {
            **self._live_channel(1800),
            "started_at": (now - timedelta(seconds=1800)).isoformat(),
        }
        usage = {
            "creator_state": {
                "last_eligibility_stream_id": "fake-live-stream",
                "last_eligibility_range_end": now - timedelta(seconds=300),
            }
        }
        with patch.object(main, "get_stream_state", return_value=None):
            _, _, seconds = main._automatic_stream_material(channel, usage)
        self.assertLess(seconds, 600)

    async def test_manual_endpoint_ignores_disabled_automatic_switch(self):
        with (
            patch.dict(
                os.environ,
                {"AUTO_CLIP_AUTOMATIC_GENERATION_ENABLED": "false"},
            ),
            patch.object(
                main,
                "enqueue_generation_job",
                return_value=(
                    {
                        "id": "fake-manual-job",
                        "status": "queued",
                        "trigger_type": "manual",
                    },
                    True,
                ),
            ) as enqueue,
            patch.object(
                main,
                "serialize_generation_job",
                return_value={"id": "fake-manual-job", "status": "queued"},
            ),
        ):
            response = await main.auto_generate_clip()
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once_with("manual")


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for automatic scheduler SQL tests",
)
class AutomaticSchedulerPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql

        cls.database_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_auto_test_{uuid.uuid4().hex[:12]}"
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
        with psycopg.connect(cls.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_schema()")
                active_schema = cursor.fetchone()[0]
        if active_schema == "public" or active_schema != cls.schema:
            raise RuntimeError(f"Unsafe test schema: {active_schema}")

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        cls.environment.stop()
        with psycopg.connect(cls.database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(cls.schema)
                    )
                )

    def setUp(self):
        self.assertTrue(generation_jobs.ensure_generation_jobs_table())

    def tearDown(self):
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE clip_generation_outbound_transfers, "
                    "clip_generation_jobs"
                )
                cursor.execute("TRUNCATE auto_clip_automation_state")
            connection.commit()

    def _enqueue(self, creator_id="creator-1", **overrides):
        values = {
            "creator_login": f"fake_{creator_id}",
            "creator_id": creator_id,
            "stream_id": "stream-1",
            "range_end": datetime.now(timezone.utc),
            "estimated_outbound_bytes": 10 * 1024 * 1024,
            "creator_daily_limit": 2,
            "global_daily_limit": 6,
            "cooldown_minutes": 60,
            "outbound_daily_budget_bytes": 150 * 1024 * 1024,
        }
        values.update(overrides)
        return generation_jobs.enqueue_eligible_automatic_job(**values)

    def _complete(self, job, outcome="clip_created"):
        claimed = generation_jobs.claim_generation_job("test-worker")
        self.assertEqual(str(claimed["id"]), str(job["id"]))
        return generation_jobs.complete_generation_job(
            str(job["id"]),
            "test-worker",
            "fake-clip" if outcome == "clip_created" else None,
            outcome=outcome,
        )

    def test_repeated_and_deferred_jobs_block_enqueue(self):
        first, reason, _ = self._enqueue()
        self.assertIsNotNone(first)
        self.assertEqual(reason, "new_material")
        duplicate, duplicate_reason, _ = self._enqueue()
        self.assertIsNone(duplicate)
        self.assertEqual(duplicate_reason, "job_already_active")
        claimed = generation_jobs.claim_generation_job("test-worker")
        generation_jobs.defer_generation_job(
            str(claimed["id"]),
            "test-worker",
            "fake low memory",
        )
        deferred, deferred_reason, _ = self._enqueue(creator_id="creator-2")
        self.assertIsNone(deferred)
        self.assertEqual(deferred_reason, "job_already_active")

    def test_cooldown_and_creator_daily_limit(self):
        job, _, _ = self._enqueue()
        self.assertTrue(self._complete(job))
        cooldown, reason, _ = self._enqueue()
        self.assertIsNone(cooldown)
        self.assertEqual(reason, "cooldown")
        limit, limit_reason, _ = self._enqueue(
            cooldown_minutes=0,
            creator_daily_limit=1,
        )
        self.assertIsNone(limit)
        self.assertEqual(limit_reason, "creator_daily_limit")

    def test_completed_range_cannot_be_enqueued_again(self):
        range_end = datetime.now(timezone.utc)
        job, _, _ = self._enqueue(range_end=range_end)
        self.assertTrue(self._complete(job))
        duplicate, reason, _ = self._enqueue(
            range_end=range_end,
            cooldown_minutes=0,
        )
        self.assertIsNone(duplicate)
        self.assertEqual(reason, "no_new_material")

    def test_global_limit_and_outbound_budget(self):
        job, _, _ = self._enqueue()
        self.assertTrue(self._complete(job))
        limited, reason, _ = self._enqueue(
            creator_id="creator-2",
            global_daily_limit=1,
        )
        self.assertIsNone(limited)
        self.assertEqual(reason, "global_daily_limit")

        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE clip_generation_outbound_transfers, "
                    "clip_generation_jobs"
                )
            connection.commit()
        budgeted, budget_reason, _ = self._enqueue(
            estimated_outbound_bytes=20 * 1024 * 1024,
            outbound_daily_budget_bytes=10 * 1024 * 1024,
        )
        self.assertIsNone(budgeted)
        self.assertEqual(budget_reason, "outbound_budget")

    def test_success_counts_but_no_clip_does_not(self):
        job, _, _ = self._enqueue()
        self.assertTrue(self._complete(job))
        usage = generation_jobs.automatic_usage_snapshot("creator-1")
        self.assertEqual(usage["clips_created"], 1)
        self.assertEqual(usage["creator_clips_created"], 1)

        no_clip, _, _ = self._enqueue(
            creator_id="creator-2",
            cooldown_minutes=0,
        )
        self.assertTrue(self._complete(no_clip, outcome="no_clip_found"))
        usage = generation_jobs.automatic_usage_snapshot("creator-2")
        self.assertEqual(usage["clips_created"], 1)
        self.assertEqual(usage["creator_clips_created"], 0)

    def test_dashboard_count_uses_strict_success_predicate(self):
        import psycopg

        rows = [
            ("manual", "completed", "clip_created", "manual-clip", 0),
            ("automatic", "completed", "no_clip_found", None, 0),
            ("automatic", "failed", "clip_created", "failed-clip", 0),
            (
                "automatic",
                "deferred_memory",
                "clip_created",
                "deferred-clip",
                0,
            ),
            ("automatic", "queued", "clip_created", "queued-clip", 0),
            ("automatic", "completed", "clip_created", None, 0),
            ("automatic", "completed", "clip_created", "real-clip", 0),
            (
                "automatic",
                "completed",
                "clip_created",
                "yesterday-clip",
                -1,
            ),
        ]
        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                for trigger, status, outcome, clip_id, day_offset in rows:
                    cursor.execute(
                        """
                        INSERT INTO clip_generation_jobs (
                            id, status, trigger_type, requested_creator,
                            requested_creator_id, result_clip_id, outcome,
                            created_at, completed_at
                        ) VALUES (
                            %s, %s, %s, 'fake_creator', 'creator-1',
                            %s, %s, NOW() + (%s * INTERVAL '1 day'),
                            NOW() + (%s * INTERVAL '1 day')
                        )
                        """,
                        (
                            uuid.uuid4(),
                            status,
                            trigger,
                            clip_id,
                            outcome,
                            day_offset,
                            day_offset,
                        ),
                    )
            connection.commit()
        usage = generation_jobs.automatic_usage_snapshot(
            "creator-1",
            "UTC",
        )
        self.assertEqual(usage["automatic_jobs_enqueued_today"], 6)
        self.assertEqual(usage["automatic_clips_created_today"], 1)
        self.assertEqual(usage["creator_clips_created"], 1)

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM clip_generation_jobs
                    WHERE result_clip_id IS DISTINCT FROM 'real-clip'
                    """
                )
            connection.commit()
        blocked, reason, scheduler_usage = self._enqueue(
            creator_id="creator-2",
            global_daily_limit=1,
            cooldown_minutes=0,
            workspace_timezone="UTC",
        )
        self.assertIsNone(blocked)
        self.assertEqual(reason, "global_daily_limit")
        self.assertEqual(scheduler_usage["daily_clips"], 1)

    def test_workspace_timezone_date_boundary(self):
        import psycopg

        workspace_timezone = "America/Los_Angeles"
        zone = ZoneInfo(workspace_timezone)
        local_today = datetime.now(zone).date()
        local_midnight = datetime.combine(
            local_today,
            datetime.min.time(),
            tzinfo=zone,
        )
        instants = (
            ("inside-today", local_midnight + timedelta(minutes=1)),
            ("outside-today", local_midnight - timedelta(minutes=1)),
        )
        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                for clip_id, completed_at in instants:
                    cursor.execute(
                        """
                        INSERT INTO clip_generation_jobs (
                            id, status, trigger_type, requested_creator_id,
                            result_clip_id, outcome, created_at, completed_at
                        ) VALUES (
                            %s, 'completed', 'automatic', 'creator-1',
                            %s, 'clip_created', %s, %s
                        )
                        """,
                        (
                            uuid.uuid4(),
                            clip_id,
                            completed_at,
                            completed_at,
                        ),
                    )
            connection.commit()
        usage = generation_jobs.automatic_usage_snapshot(
            "creator-1",
            workspace_timezone,
        )
        self.assertEqual(usage["automatic_clips_created_today"], 1)
        self.assertEqual(usage["creator_clips_created"], 1)

    def test_simultaneous_scheduler_claims_create_one_job(self):
        results = []

        def enqueue():
            results.append(self._enqueue())

        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item[0] is not None for item in results), 1)

    def test_zero_byte_direct_transfer_does_not_consume_budget(self):
        job, _, _ = self._enqueue()
        self.assertFalse(
            generation_jobs.record_generation_job_outbound_bytes(
                str(job["id"]),
                0,
            )
        )
        usage = generation_jobs.automatic_usage_snapshot()
        self.assertEqual(usage["actual_outbound_bytes"], 0)

    def test_render_originated_transfer_is_counted_on_transfer_day(self):
        job, _, _ = self._enqueue()
        self.assertTrue(
            generation_jobs.record_generation_job_outbound_bytes(
                str(job["id"]),
                12345,
                "r2",
            )
        )
        usage = generation_jobs.automatic_usage_snapshot()
        self.assertEqual(usage["actual_outbound_bytes"], 12345)


if __name__ == "__main__":
    unittest.main()
