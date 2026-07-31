from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import r2_cleanup


NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)
CONFIG = {
    "enabled": False,
    "dry_run": True,
    "unpublished_retention_days": 30,
    "failed_retention_days": 7,
    "batch_size": 50,
    "poll_hours": 24,
}


def clip(status="ready_for_review", age_days=31, **updates):
    value = {
        "generated_clip_id": "fake-generated-clip",
        "status": status,
        "object_key": "clips/fake-generated-clip/video.mp4",
        "generated_at": NOW - timedelta(days=age_days),
        "retry_count": 2,
        "retention_locked": False,
        "is_favorited": False,
        "is_retained": False,
    }
    value.update(updates)
    return value


class CleanupEligibilityTests(unittest.TestCase):
    def decision(self, value, active_job=False):
        return r2_cleanup.clip_cleanup_eligibility(
            value, now=NOW, config=CONFIG, active_job=active_job,
        )

    def test_published_clip_never_qualifies(self):
        self.assertEqual(self.decision(clip("published", 100))["reason"], "published")

    def test_old_unpublished_clip_qualifies(self):
        self.assertTrue(self.decision(clip(age_days=30))["eligible"])

    def test_recent_unpublished_clip_does_not_qualify(self):
        self.assertEqual(self.decision(clip(age_days=29))["reason"], "too_new")

    def test_old_failed_clip_qualifies(self):
        decision = self.decision(clip("failed", 7, retry_count=2))
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reason"], "failed_retention")

    def test_retryable_failure_does_not_qualify(self):
        self.assertFalse(self.decision(clip("failed", 30, retry_count=1))["eligible"])

    def test_locked_favorited_or_retained_clip_never_qualifies(self):
        for field in ("retention_locked", "is_favorited", "is_retained"):
            with self.subTest(field=field):
                self.assertEqual(
                    self.decision(clip(age_days=100, **{field: True}))["reason"],
                    "locked",
                )

    def test_scheduled_and_publishing_clips_never_qualify(self):
        for status in ("scheduled", "publishing", "processing", "queued"):
            with self.subTest(status=status):
                self.assertFalse(self.decision(clip(status, 100))["eligible"])

    def test_active_job_blocks_cleanup(self):
        self.assertEqual(
            self.decision(clip(age_days=100), active_job=True)["reason"],
            "active_job",
        )

    def test_pending_or_deleted_clip_is_idempotently_excluded(self):
        self.assertFalse(self.decision(clip(deletion_pending_at=NOW))["eligible"])
        self.assertFalse(self.decision(clip(object_deleted_at=NOW))["eligible"])

    def test_stale_pending_claim_is_recoverable(self):
        decision = self.decision(
            clip(deletion_pending_at=NOW - timedelta(hours=3)),
        )
        self.assertTrue(decision["eligible"])

    def test_defaults_are_disabled_and_dry_run(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = r2_cleanup.cleanup_config()
        self.assertFalse(settings["enabled"])
        self.assertTrue(settings["dry_run"])
        self.assertEqual(settings["unpublished_retention_days"], 30)
        self.assertEqual(settings["failed_retention_days"], 7)


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for R2 cleanup integration tests",
)
class CleanupPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        import generation_jobs
        import main

        cls.psycopg = psycopg
        cls.sql = sql
        cls.base_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_r2_cleanup_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(cls.base_url, options="-c search_path=pg_catalog") as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
            connection.commit()
        cls.schema_url = make_conninfo(
            cls.base_url, options=f"-csearch_path={cls.schema}",
        )
        cls.main_database = patch.object(main, "DATABASE_URL", cls.schema_url)
        cls.job_database = patch.dict(os.environ, {"DATABASE_URL": cls.schema_url})
        cls.main_database.start()
        cls.job_database.start()
        if not main._ensure_clip_history_table() or not generation_jobs.ensure_generation_jobs_table():
            raise RuntimeError("isolated cleanup migration failed")

    @classmethod
    def tearDownClass(cls):
        self = cls
        self.job_database.stop()
        self.main_database.stop()
        with self.psycopg.connect(self.base_url, options="-c search_path=pg_catalog") as connection:
            with connection.cursor() as cursor:
                cursor.execute(self.sql.SQL("DROP SCHEMA {} CASCADE").format(self.sql.Identifier(self.schema)))
            connection.commit()

    def setUp(self):
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM clip_generation_jobs")
                cursor.execute("DELETE FROM twitch_clip_history")
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        clip_id, generated_clip_id, status, object_key,
                        generated_at, retry_count
                    ) VALUES ('fake-source', 'fake-generated', 'ready_for_review',
                              'clips/fake-generated/video.mp4', NOW() - INTERVAL '31 days', 0)
                    """
                )
            connection.commit()

    def test_dry_run_deletes_nothing(self):
        with patch("r2_cleanup.get_video_object_size", return_value=123), patch(
            "r2_cleanup.delete_video_object_with_result"
        ) as delete:
            result = r2_cleanup.cleanup_report(self.schema_url, config=CONFIG)
        delete.assert_not_called()
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["deleted"], 0)

    def test_successful_delete_archives_once(self):
        execute = {**CONFIG, "dry_run": False, "enabled": True}
        with patch(
            "r2_cleanup.delete_video_object_with_result",
            return_value={"deleted": True, "bytes": 123, "error": ""},
        ) as delete:
            first = r2_cleanup.cleanup_report(self.schema_url, config=execute)
            second = r2_cleanup.cleanup_report(self.schema_url, config=execute)
        self.assertEqual(first["deleted"], 1)
        self.assertEqual(second["deleted"], 0)
        delete.assert_called_once()
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status, object_deleted_at FROM twitch_clip_history")
                status, deleted_at = cursor.fetchone()
        self.assertEqual(status, "archived")
        self.assertIsNotNone(deleted_at)

    def test_failed_delete_clears_claim_for_retry(self):
        execute = {**CONFIG, "dry_run": False, "enabled": True}
        with patch(
            "r2_cleanup.delete_video_object_with_result",
            return_value={"deleted": False, "bytes": 0, "error": "temporary"},
        ):
            result = r2_cleanup.cleanup_report(self.schema_url, config=execute)
        self.assertEqual(result["failed"], 1)
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT deletion_pending_at, deletion_error FROM twitch_clip_history")
                pending, error = cursor.fetchone()
        self.assertIsNone(pending)
        self.assertEqual(error, "temporary")


if __name__ == "__main__":
    unittest.main()
