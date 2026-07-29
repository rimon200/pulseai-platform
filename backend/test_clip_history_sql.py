from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main
import stream_history


class ClipHistoryLoggingTests(unittest.TestCase):
    def test_status_write_error_logs_types_without_parameter_values(self):
        clip = {
            "id": "FAKE_SAFE_LOG_CLIP",
            "url": "https://clips.twitch.tv/FAKE_SAFE_LOG_CLIP",
            "_stream_id": "317742236632",
        }
        captured = io.StringIO()
        with patch.object(main, "DATABASE_URL", "postgresql://configured"):
            with patch(
                "psycopg.connect",
                side_effect=RuntimeError("fake database error"),
            ):
                with contextlib.redirect_stdout(captured):
                    written = main._clip_history_upsert(
                        clip,
                        "failed",
                        increment_retry=True,
                    )
        output = captured.getvalue()
        self.assertFalse(written)
        self.assertIn("operation=status_update", output)
        self.assertIn("table=twitch_clip_history", output)
        self.assertIn("source_stream_id:str", output)
        self.assertIn("increment_retry:bool", output)
        self.assertNotIn("317742236632", output)
        self.assertNotIn("postgresql://configured", output)


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for clip-history SQL tests",
)
class PostgreSQLClipHistorySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        cls.psycopg = psycopg
        cls.sql = sql
        cls.base_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_clip_sql_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(
            cls.base_url,
            options="-c search_path=pg_catalog",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(
                        sql.Identifier(cls.schema)
                    )
                )
            connection.commit()
        cls.schema_url = make_conninfo(
            cls.base_url,
            options=f"-csearch_path={cls.schema}",
        )
        cls.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": cls.schema_url,
                "PGOPTIONS": f"-c search_path={cls.schema}",
            },
        )
        cls.environment.start()
        cls.database_url = patch.object(main, "DATABASE_URL", cls.schema_url)
        cls.database_url.start()
        if not main._ensure_clip_history_table():
            raise RuntimeError("isolated clip-history migration failed")
        if not stream_history.ensure_stream_history_tables():
            raise RuntimeError("isolated stream-history migration failed")

    @classmethod
    def tearDownClass(cls):
        cls.database_url.stop()
        cls.environment.stop()
        with cls.psycopg.connect(
            cls.base_url,
            options="-c search_path=pg_catalog",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    cls.sql.SQL("DROP SCHEMA {} CASCADE").format(
                        cls.sql.Identifier(cls.schema)
                    )
                )
            connection.commit()

    def test_stream_id_binds_to_text_and_retry_flag_binds_to_boolean(self):
        stream_id = "317742236632"
        clip_id = "FAKE_SQL_BINDING"
        written = main._clip_history_upsert(
            {
                "id": clip_id,
                "url": f"https://clips.twitch.tv/{clip_id}",
                "_stream_id": stream_id,
            },
            "failed",
            failure_stage="transcription",
            increment_retry=True,
        )
        self.assertTrue(written)
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_stream_id, retry_count
                    FROM twitch_clip_history
                    WHERE provider = 'twitch' AND clip_id = %s
                    """,
                    (clip_id,),
                )
                saved = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT data_type FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'auto_clip_stream_state'
                      AND column_name = 'is_refreshable'
                    """
                )
                refreshable_type = cursor.fetchone()[0]
        self.assertEqual(saved, (stream_id, 1))
        self.assertEqual(refreshable_type, "boolean")

        now = datetime.now(timezone.utc)
        state = stream_history.register_newest_stream(
            "fake-binding-creator",
            {"stream_id": stream_id, "started_at": now},
        )
        self.assertIs(state["is_refreshable"], True)
        self.assertEqual(state["stream_id"], stream_id)

    def test_legacy_missing_and_bad_records_do_not_abort_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            records = [
                {"title": "missing every Twitch identifier"},
                {
                    "id": "FAKE_BAD_LEGACY",
                    "url": "https://clips.twitch.tv/FAKE_BAD_LEGACY",
                    "created_at": "not-a-timestamp",
                },
                {
                    "id": "FAKE_GOOD_LEGACY",
                    "url": "https://clips.twitch.tv/FAKE_GOOD_LEGACY",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
            (base_dir / "clips.json").write_text(
                json.dumps(records),
                encoding="utf-8",
            )
            captured = io.StringIO()
            with patch.object(main, "BASE_DIR", base_dir):
                with self.psycopg.connect(self.schema_url) as connection:
                    with connection.cursor() as cursor:
                        with contextlib.redirect_stdout(captured):
                            completed = main._backfill_clip_history(cursor)
                    connection.commit()
        self.assertTrue(completed)
        output = captured.getvalue()
        self.assertIn(
            "CLIP HISTORY BACKFILL SKIPPED | "
            "reason=missing_stable_twitch_identifier | source=clips.json",
            output,
        )
        self.assertIn("operation=backfill_record", output)
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT clip_id FROM twitch_clip_history
                    WHERE clip_id IN ('FAKE_BAD_LEGACY', 'FAKE_GOOD_LEGACY')
                    ORDER BY clip_id
                    """
                )
                saved_ids = [row[0] for row in cursor.fetchall()]
        self.assertEqual(saved_ids, ["FAKE_GOOD_LEGACY"])


if __name__ == "__main__":
    unittest.main()
