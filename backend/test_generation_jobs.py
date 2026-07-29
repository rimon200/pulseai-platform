from __future__ import annotations

import os
import threading
import unittest
import uuid
from unittest.mock import patch

import generation_jobs


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for PostgreSQL generation-job tests",
)
class PostgreSQLGenerationJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql

        cls.database_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_job_test_{uuid.uuid4().hex[:12]}"
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
        cls.embedded_lock_id = patch.object(
            generation_jobs,
            "EMBEDDED_WORKER_ADVISORY_LOCK_ID",
            int(uuid.uuid4().int % 2_000_000_000),
        )
        cls.embedded_lock_id.start()
        with psycopg.connect(cls.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW search_path")
                self_search_path = cursor.fetchone()[0]
                cursor.execute("SELECT current_schema()")
                active_schema = cursor.fetchone()[0]
        if active_schema != cls.schema or "public" == active_schema:
            raise RuntimeError(
                f"Unsafe test schema: {active_schema} ({self_search_path})"
            )

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql

        cls.embedded_lock_id.stop()
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
        self.assertTrue(generation_jobs.ensure_generation_jobs_table())

    def tearDown(self):
        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE clip_generation_jobs")
            connection.commit()

    def test_enqueue_is_idempotent_for_one_active_job(self):
        first, created = generation_jobs.enqueue_generation_job("manual")
        second, reused_created = generation_jobs.enqueue_generation_job(
            "automatic"
        )
        self.assertTrue(created)
        self.assertFalse(reused_created)
        self.assertEqual(first["id"], second["id"])

    def test_only_one_embedded_worker_owns_the_loop(self):
        first = generation_jobs.try_acquire_embedded_worker_ownership()
        self.assertIsNotNone(first)
        try:
            second = generation_jobs.try_acquire_embedded_worker_ownership()
            self.assertIsNone(second)
        finally:
            generation_jobs.release_embedded_worker_ownership(first)
        replacement = generation_jobs.try_acquire_embedded_worker_ownership()
        self.assertIsNotNone(replacement)
        generation_jobs.release_embedded_worker_ownership(replacement)

    def test_only_one_worker_claims_and_stale_lease_recovers(self):
        job, _ = generation_jobs.enqueue_generation_job("manual")
        results = []

        def claim(worker):
            results.append(
                generation_jobs.claim_generation_job(worker, lease_seconds=60)
            )

        threads = [
            threading.Thread(target=claim, args=(f"worker-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)

        import psycopg

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE clip_generation_jobs
                    SET lease_expires_at = NOW() - INTERVAL '1 second'
                    WHERE id = %s
                    """,
                    (job["id"],),
                )
            connection.commit()
        recovered = generation_jobs.claim_generation_job(
            "recovery-worker",
            lease_seconds=60,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["retry_count"], 1)

    def test_stage_completion_and_deferred_retry(self):
        job, _ = generation_jobs.enqueue_generation_job("manual")
        claimed = generation_jobs.claim_generation_job("worker-a")
        self.assertTrue(
            generation_jobs.update_generation_job_stage(
                str(claimed["id"]),
                "worker-a",
                "rendering",
            )
        )
        self.assertTrue(
            generation_jobs.defer_generation_job(
                str(claimed["id"]),
                "worker-a",
                "memory low",
            )
        )
        deferred_job, deferred_created = generation_jobs.enqueue_generation_job(
            "automatic"
        )
        self.assertFalse(deferred_created)
        self.assertEqual(deferred_job["id"], claimed["id"])
        retried = generation_jobs.claim_generation_job(
            "worker-b",
            deferred_retry_seconds=0,
        )
        self.assertIsNotNone(retried)
        self.assertEqual(retried["retry_count"], 1)
        self.assertTrue(
            generation_jobs.complete_generation_job(
                str(retried["id"]),
                "worker-b",
                "clip-123",
            )
        )
        saved = generation_jobs.get_generation_job(str(job["id"]))
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["result_clip_id"], "clip-123")
        self.assertEqual(saved["outcome"], "clip_created")

        no_clip_job, _ = generation_jobs.enqueue_generation_job("manual")
        no_clip_claim = generation_jobs.claim_generation_job("worker-c")
        self.assertTrue(
            generation_jobs.complete_generation_job(
                str(no_clip_claim["id"]),
                "worker-c",
                None,
                "No suitable clip was found. Try again later.",
            )
        )
        no_clip_saved = generation_jobs.get_generation_job(
            str(no_clip_job["id"])
        )
        self.assertEqual(no_clip_saved["status"], "completed")
        self.assertIsNone(no_clip_saved["result_clip_id"])
        self.assertEqual(no_clip_saved["outcome"], "no_clip_found")

    def test_reaction_regions_persist_with_generated_clip(self):
        import main
        import psycopg

        self.assertTrue(main._ensure_clip_history_table())
        saved = main._persist_generated_clip_record(
            {
                "id": "fake-generated-reaction",
                "twitch_clip_id": "FakeReactionClip",
                "public_url": "https://clips.twitch.tv/FakeReactionClip",
                "creator": "Fake Creator",
                "title": "Fake reaction title",
                "score": 88,
                "visual_layout_mode": "reaction_stream",
                "visual_layout_confidence": 0.9,
                "visual_layout_reason": "fake persistent webcam",
                "visual_layout_version": "layout-v2",
                "reaction_region": {
                    "x": 0.0, "y": 0.0, "width": 0.3, "height": 0.35,
                },
                "content_region": {
                    "x": 0.0, "y": 0.3, "width": 1.0, "height": 0.7,
                },
            }
        )
        self.assertIsNotNone(saved)
        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT visual_layout_mode, reaction_region, content_region
                    FROM twitch_clip_history
                    WHERE provider = 'twitch' AND clip_id = 'FakeReactionClip'
                    """
                )
                row = cursor.fetchone()
        self.assertEqual(row[0], "reaction_stream")
        self.assertEqual(float(row[1]["width"]), 0.3)
        self.assertEqual(float(row[2]["height"]), 0.7)


if __name__ == "__main__":
    unittest.main()
