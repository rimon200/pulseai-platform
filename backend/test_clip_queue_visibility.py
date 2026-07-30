from __future__ import annotations

import contextlib
import asyncio
import io
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class FrontendVisibilityContractTests(unittest.TestCase):
    def setUp(self):
        self.frontend_dir = Path(__file__).resolve().parents[1] / "frontend" / "src"

    def test_ai_clips_uses_paginated_filter_owned_requests(self):
        source = (
            self.frontend_dir / "components" / "AIClips.jsx"
        ).read_text(encoding="utf-8")
        self.assertIn("buildClipListUrl(", source)
        self.assertIn('useState("All")', source)
        self.assertIn("Load More", source)
        self.assertNotIn("fetch(`${API_BASE_URL}/api/clips`)", source)

    def test_app_polling_cannot_overwrite_ai_clips_state(self):
        source = (self.frontend_dir / "App.jsx").read_text(encoding="utf-8")
        self.assertIn("<AIClips styles={styles} />", source)
        self.assertNotIn("clips={clips}\n  setClips={setClips}", source)

    def test_unknown_status_is_not_reviewable(self):
        source = (
            self.frontend_dir / "components" / "aiClipsPagination.js"
        ).read_text(encoding="utf-8")
        publishable_block = source[source.index(
            "export const isPublishableClipStatus"
        ):]
        self.assertNotIn('"unknown"', publishable_block)

    def test_unpublished_queue_navigation_is_wired(self):
        source = (self.frontend_dir / "App.jsx").read_text(encoding="utf-8")
        self.assertIn('{ name: "Unpublished Queue", icon: "📋" }', source)
        self.assertIn('activePage === "Unpublished Queue"', source)
        self.assertIn("<UnpublishedQueue styles={styles} />", source)


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for PostgreSQL queue tests",
)
class PostgreSQLQueuePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        cls.psycopg = psycopg
        cls.sql = sql
        cls.base_url = os.environ["TEST_DATABASE_URL"]
        cls.schema = f"pulseai_queue_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(
            cls.base_url,
            options="-c search_path=pg_catalog",
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
                )
            connection.commit()
        cls.schema_url = make_conninfo(
            cls.base_url,
            options=f"-csearch_path={cls.schema}",
        )
        cls.original_database_url = main.DATABASE_URL
        cls.original_base_dir = main.BASE_DIR
        cls.original_history_ready = main.app.state.clip_history_ready
        main.DATABASE_URL = cls.schema_url
        main.BASE_DIR = Path(__file__).resolve().parent
        if not main._ensure_clip_history_table():
            raise RuntimeError("isolated clip-history migration failed")
        main.app.state.clip_history_ready = True

    @classmethod
    def tearDownClass(cls):
        main.DATABASE_URL = cls.original_database_url
        main.BASE_DIR = cls.original_base_dir
        main.app.state.clip_history_ready = cls.original_history_ready
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

    def _clip(self, clip_id: str) -> dict[str, object]:
        return {
            "id": f"generated-{clip_id}",
            "twitch_clip_id": clip_id,
            "public_url": f"https://clips.twitch.tv/{clip_id}",
            "title": "Safe generated title",
            "creator": "Fake Creator",
            "creator_id": f"creator-{clip_id}",
            "source_creator_id": f"creator-{clip_id}",
            "game": "Fake Game",
            "score": 88,
            "transcript": "A truthful fake transcript for integration testing.",
            "video_path": f"/tmp/{clip_id}.mp4",
            "raw_video_path": f"/tmp/{clip_id}-raw.mp4",
            "object_key": f"clips/{clip_id}/fake.mp4",
            "durable_url": f"https://media.invalid/{clip_id}.mp4",
            "ai_post_caption": "A truthful fake caption.",
            "ai_hashtags": ["#FakeCreator", "#FakeGame"],
            "ai_tiktok_description": "A truthful fake caption. #FakeCreator #FakeGame",
            "caption_generation_version": "caption-v1",
            "duration_profile": "short",
            "requested_duration": 30,
            "actual_duration": 30,
        }

    def _status(self, clip_id: str) -> str:
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status FROM twitch_clip_history
                    WHERE provider = 'twitch' AND clip_id = %s
                    """,
                    (clip_id,),
                )
                return cursor.fetchone()[0]

    def _seed_status(self, clip_id: str, status: str) -> None:
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        provider, clip_id, clip_url, status
                    ) VALUES ('twitch', %s, %s, %s)
                    """,
                    (
                        clip_id,
                        f"https://clips.twitch.tv/{clip_id}",
                        status,
                    ),
                )
            connection.commit()

    def test_missing_history_row_is_inserted_ready_for_review(self):
        clip_id = "FAKE_VISIBILITY_MISSING"
        saved = main._persist_generated_clip_record(self._clip(clip_id))
        self.assertEqual(saved["status"], "ready_for_review")
        self.assertEqual(self._status(clip_id), "ready_for_review")
        self.assertEqual(saved["generated_clip_id"], f"generated-{clip_id}")
        self.assertTrue(
            main._verify_generated_clip_retrieval(
                saved["generated_clip_id"],
            )
        )

    def test_newly_persisted_clip_is_first_in_all_and_unpublished(self):
        clip_id = f"FAKE_VISIBILITY_NEWEST_{uuid.uuid4().hex}"
        clip = self._clip(clip_id)
        clip["creator"] = f"Newest Creator {uuid.uuid4().hex}"
        clip["created_at"] = datetime.now(timezone.utc) - timedelta(days=90)
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                for index in range(13):
                    older_id = f"FAKE_EXISTING_{uuid.uuid4().hex}_{index}"
                    cursor.execute(
                        """
                        INSERT INTO twitch_clip_history (
                            provider, clip_id, clip_url, creator_name,
                            created_at, generated_at, generated_clip_id,
                            status
                        ) VALUES (
                            'twitch', %s, %s, %s,
                            NOW() - INTERVAL '1 day',
                            NOW() - INTERVAL '1 day',
                            %s, 'ready_for_review'
                        )
                        """,
                        (
                            older_id,
                            f"https://clips.twitch.tv/{older_id}",
                            clip["creator"],
                            f"generated-{older_id}",
                        ),
                    )
            connection.commit()
        saved = main._persist_generated_clip_record(clip)
        all_clips = asyncio.run(main.get_clips(
            limit=12,
            page=1,
            status_filter="all",
            creator=clip["creator"],
        ))
        unpublished = asyncio.run(main.get_clips(
            limit=12,
            page=1,
            status_filter="unpublished",
            creator=clip["creator"],
        ))
        published = asyncio.run(main.get_clips(
            limit=12,
            page=1,
            status_filter="published",
            creator=clip["creator"],
        ))
        self.assertEqual(all_clips["total"], 14)
        self.assertEqual(len(all_clips["items"]), 12)
        self.assertTrue(all_clips["has_more"])
        self.assertEqual(all_clips["items"][0]["id"], saved["generated_clip_id"])
        self.assertEqual(
            unpublished["items"][0]["id"],
            saved["generated_clip_id"],
        )
        self.assertNotIn(
            saved["generated_clip_id"],
            {clip["id"] for clip in published["items"]},
        )
        self.assertEqual(
            all_clips["items"][0]["created_at"],
            saved["created_at"],
        )

    def test_nonterminal_rows_become_ready_for_review(self):
        for status in ("discovered", "processing", "fully_evaluated"):
            with self.subTest(status=status):
                clip_id = f"FAKE_VISIBILITY_{status.upper()}"
                self._seed_status(clip_id, status)
                saved = main._persist_generated_clip_record(self._clip(clip_id))
                self.assertEqual(saved["status"], "ready_for_review")
                self.assertEqual(self._status(clip_id), "ready_for_review")

    def test_stronger_statuses_are_not_downgraded(self):
        for status in (
            "publishing",
            "uploaded_to_inbox",
            "published",
            "archived",
        ):
            with self.subTest(status=status):
                clip_id = f"FAKE_VISIBILITY_{status.upper()}"
                self._seed_status(clip_id, status)
                saved = main._persist_generated_clip_record(self._clip(clip_id))
                self.assertEqual(saved["status"], status)
                self.assertEqual(self._status(clip_id), status)

    def test_r2_media_is_retained_and_recovery_is_logged(self):
        clip = self._clip("FAKE_VISIBILITY_FAILURE")
        captured = io.StringIO()
        with patch("psycopg.connect", side_effect=RuntimeError("fake database outage")):
            with contextlib.redirect_stdout(captured):
                saved = main._persist_generated_clip_record(clip)
        self.assertIsNone(saved)
        self.assertIn(
            "CLIP QUEUE PERSISTENCE FAILED - R2 MEDIA RETAINED",
            captured.getvalue(),
        )
        self.assertIn(str(clip["object_key"]), captured.getvalue())

    def test_database_creator_survives_reload_and_reinitialization(self):
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO monitored_creators (
                        twitch_user_id, login, display_name, enabled, priority
                    ) VALUES (
                        'FAKE_USER_ID', 'fakepersistentcreator',
                        'Fake Persistent Creator', TRUE, 99
                    )
                    """
                )
            connection.commit()
        self.assertIn(
            "fakepersistentcreator",
            {creator["channel"] for creator in main.load_creators()},
        )
        self.assertTrue(main._ensure_clip_history_table())
        self.assertIn(
            "fakepersistentcreator",
            {creator["channel"] for creator in main.load_creators()},
        )

    def test_json_backfill_does_not_overwrite_database_creator(self):
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE monitored_creators
                    SET display_name = 'Database Owned Name', priority = 77
                    WHERE login = 'kaicenat'
                    """
                )
            connection.commit()
        self.assertTrue(main._ensure_clip_history_table())
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT display_name, priority FROM monitored_creators
                    WHERE login = 'kaicenat'
                    """
                )
                row = cursor.fetchone()
        self.assertEqual(row, ("Database Owned Name", 77))

    def _seed_paginated_clips(self, creator_name: str) -> list[str]:
        newest = datetime.now(timezone.utc)
        expected_ids = []
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                for index in range(15):
                    clip_id = f"FAKE_PAGE_{index:02d}"
                    generated_id = f"generated-page-{index:02d}"
                    expected_ids.append(generated_id)
                    cursor.execute(
                        """
                        INSERT INTO twitch_clip_history (
                            provider, clip_id, clip_url, creator_name,
                            created_at, generated_at, generated_clip_id,
                            status, object_key, durable_url
                        ) VALUES (
                            'twitch', %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            clip_id,
                            f"https://clips.twitch.tv/{clip_id}",
                            creator_name,
                            newest - timedelta(minutes=index),
                            newest - timedelta(minutes=index),
                            generated_id,
                            "published" if index in {2, 13} else "ready_for_review",
                            f"clips/{clip_id}/fake.mp4",
                            f"https://media.invalid/{clip_id}.mp4",
                        ),
                    )
            connection.commit()
        return expected_ids

    def test_pagination_filters_and_display_limit_do_not_delete_clips(self):
        creator_name = f"Pagination Creator {uuid.uuid4().hex}"
        expected_ids = self._seed_paginated_clips(creator_name)
        first_page = asyncio.run(main.get_clips(
            limit=12,
            page=1,
            status_filter="all",
            creator=creator_name,
        ))
        second_page = asyncio.run(main.get_clips(
            limit=12,
            page=2,
            status_filter="all",
            creator=creator_name,
        ))
        self.assertEqual(
            [clip["id"] for clip in first_page["items"]],
            expected_ids[:12],
        )
        self.assertTrue(first_page["has_more"])
        self.assertEqual(
            [clip["id"] for clip in second_page["items"]],
            expected_ids[12:],
        )
        self.assertFalse(second_page["has_more"])
        combined_ids = {
            clip["id"]
            for clip in first_page["items"] + second_page["items"]
        }
        self.assertEqual(combined_ids, set(expected_ids))

        unpublished = asyncio.run(main.get_clips(
            limit=100,
            page=1,
            status_filter="unpublished",
            creator=creator_name,
        ))
        published = asyncio.run(main.get_clips(
            limit=100,
            page=1,
            status_filter="published",
            creator=creator_name,
        ))
        self.assertIn(expected_ids[14], {
            clip["id"] for clip in unpublished["items"]
        })
        self.assertEqual(
            {clip["id"] for clip in published["items"]},
            {expected_ids[2], expected_ids[13]},
        )
        with self.psycopg.connect(self.schema_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*), COUNT(object_key), COUNT(durable_url)
                    FROM twitch_clip_history
                    WHERE creator_name = %s
                    """,
                    (creator_name,),
                )
                counts = cursor.fetchone()
        self.assertEqual(counts, (15, 15, 15))

    def test_old_unpublished_clip_uses_normal_publish_flow(self):
        clip_id = f"FAKE_OLD_PUBLISH_{uuid.uuid4().hex}"
        generated_id = f"generated-{clip_id}"
        with tempfile.NamedTemporaryFile(suffix=".mp4") as media:
            media.write(b"fake-video")
            media.flush()
            with self.psycopg.connect(self.schema_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO twitch_clip_history (
                            provider, clip_id, clip_url, creator_name,
                            created_at, generated_at, generated_clip_id,
                            status, local_video_path, actual_duration
                        ) VALUES (
                            'twitch', %s, %s, 'Old Creator',
                            NOW() - INTERVAL '90 days',
                            NOW() - INTERVAL '90 days',
                            %s, 'ready_for_review', %s, 30
                        )
                        """,
                        (
                            clip_id,
                            f"https://clips.twitch.tv/{clip_id}",
                            generated_id,
                            media.name,
                        ),
                    )
                connection.commit()
            with patch.object(main, "load_published", return_value=[]):
                with patch.object(
                    main,
                    "get_publish_settings",
                    new=AsyncMock(return_value={"post_mode": "draft"}),
                ):
                    with patch.object(
                        main,
                        "upload_tiktok_draft",
                        new=AsyncMock(return_value={"publish_id": "fake-upload"}),
                    ):
                        result = asyncio.run(
                            main.publish_clip_to_tiktok({"id": generated_id})
                        )
        self.assertEqual(result["publish_id"], "fake-upload")
        self.assertEqual(self._status(clip_id), "uploaded_to_inbox")


if __name__ == "__main__":
    unittest.main()
