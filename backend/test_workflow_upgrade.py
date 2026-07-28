from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import main
import ai
import storage_service


class WorkflowUpgradeTests(unittest.TestCase):
    def test_clip_eleven_remains_retrievable(self):
        clips = [{"id": str(index)} for index in range(1, 13)]
        second_page = main._paginate_items(clips, page=2, limit=10)
        self.assertEqual([item["id"] for item in second_page["items"]], ["11", "12"])

    def test_unpublished_pagination_does_not_delete_items(self):
        clips = [{"id": str(index), "status": "ready_for_review"} for index in range(15)]
        first_page = main._paginate_items(clips, page=1, limit=10)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["total"], 15)
        self.assertEqual(len(clips), 15)

    def test_published_history_remains_paginated(self):
        clips = [{"id": str(index), "status": "published"} for index in range(12)]
        self.assertEqual(main._paginate_items(clips, 2, 10)["items"][0]["id"], "10")

    def test_weak_moment_is_not_padded_to_sixty_seconds(self):
        with patch.dict(os.environ, {
            "AUTO_CLIP_LONGFORM_ENABLED": "true",
            "AUTO_CLIP_LONGFORM_TARGET_PERCENT": "100",
        }):
            profile = main._select_duration_profile({
                "id": "FAKE_WEAK",
                "duration": 30,
                "transcript": "short moment",
                "segments": [],
            })
        self.assertEqual(profile["duration_profile"], "short")
        self.assertLessEqual(profile["requested_duration"], 30)

    def test_long_profile_requires_coherent_source(self):
        with patch.dict(os.environ, {
            "AUTO_CLIP_LONGFORM_ENABLED": "true",
            "AUTO_CLIP_LONGFORM_TARGET_PERCENT": "100",
        }):
            profile = main._select_duration_profile({
                "id": "FAKE_LONG",
                "duration": 80,
                "transcript": " ".join(["context"] * 100),
                "segments": [{}] * 8,
            })
        self.assertEqual(profile["duration_profile"], "long")
        self.assertLessEqual(profile["requested_duration"], 80)
        self.assertEqual(profile["actual_duration"], profile["requested_duration"])

    def test_empty_caption_package_is_structured(self):
        package = ai.generate_tiktok_caption_package("", "FAKE_CREATOR", "FAKE_GAME")
        self.assertEqual(package["ai_hashtags"], [])
        self.assertEqual(package["caption_generation_version"], "caption-v1")

    def test_direct_post_is_unavailable_by_default(self):
        self.assertFalse(main._default_publish_settings()["direct_post_available"])
        self.assertEqual(main._default_publish_settings()["post_mode"], "draft")

    def test_object_storage_disabled_fallback(self):
        with patch.dict(os.environ, {"OBJECT_STORAGE_ENABLED": "false"}):
            self.assertFalse(storage_service.object_storage_enabled())

    def test_identifier_mismatch_is_rejected(self):
        clip_id, clip_url = main._normalized_twitch_identifiers({
            "id": "FAKE_A",
            "url": "https://clips.twitch.tv/FAKE_B",
        })
        self.assertEqual((clip_id, clip_url), ("", ""))


if __name__ == "__main__":
    unittest.main()
