import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import storage_service
import main


class StoragePreviewTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "OBJECT_STORAGE_ENABLED": "true",
            "OBJECT_STORAGE_ENDPOINT": "https://account.r2.cloudflarestorage.com",
            "OBJECT_STORAGE_REGION": "auto",
            "OBJECT_STORAGE_BUCKET": "fake-bucket",
            "OBJECT_STORAGE_ACCESS_KEY_ID": "fake-access",
            "OBJECT_STORAGE_SECRET_ACCESS_KEY": "fake-secret",
            "OBJECT_STORAGE_PREVIEW_URL_EXPIRATION_SECONDS": "900",
        }

    def test_object_key_without_durable_url_gets_presigned_url(self):
        client = MagicMock()
        client.generate_presigned_url.return_value = (
            "https://signed.invalid/clip.mp4?expires=fresh"
        )
        with patch.dict(os.environ, self.environment, clear=False), patch(
            "storage_service._storage_client",
            return_value=client,
        ):
            result = storage_service.get_video_preview_url(
                "clips/fake-clip/video.mp4",
                "fake-clip",
            )
        self.assertTrue(result["preview_available"])
        self.assertEqual(result["source"], "presigned_r2")
        self.assertEqual(result["expires_in_seconds"], 900)
        client.head_object.assert_called_once()
        client.generate_presigned_url.assert_called_once()

    def test_each_request_generates_a_fresh_presigned_url(self):
        client = MagicMock()
        client.generate_presigned_url.side_effect = [
            "https://signed.invalid/clip.mp4?token=one",
            "https://signed.invalid/clip.mp4?token=two",
        ]
        with patch.dict(os.environ, self.environment, clear=False), patch(
            "storage_service._storage_client",
            return_value=client,
        ):
            first = storage_service.get_video_preview_url(
                "clips/fake-clip/video.mp4",
                "fake-clip",
            )
            second = storage_service.get_video_preview_url(
                "clips/fake-clip/video.mp4",
                "fake-clip",
            )
        self.assertNotEqual(first["preview_url"], second["preview_url"])

    def test_public_base_url_returns_encoded_direct_url(self):
        environment = {
            **self.environment,
            "OBJECT_STORAGE_PUBLIC_BASE_URL": "https://media.invalid",
        }
        with patch.dict(os.environ, environment, clear=False):
            result = storage_service.get_video_preview_url(
                "clips/fake clip/video.mp4",
                "fake-clip",
            )
        self.assertEqual(result["source"], "public_url")
        self.assertEqual(
            result["durable_url"],
            "https://media.invalid/clips/fake%20clip/video.mp4",
        )
        self.assertEqual(result["preview_url"], result["durable_url"])

    def test_missing_object_is_unavailable_cleanly(self):
        error = RuntimeError("missing")
        error.response = {
            "ResponseMetadata": {"HTTPStatusCode": 404},
            "Error": {"Code": "NoSuchKey"},
        }
        client = MagicMock()
        client.head_object.side_effect = error
        output = io.StringIO()
        with patch.dict(os.environ, self.environment, clear=False), patch(
            "storage_service._storage_client",
            return_value=client,
        ), redirect_stdout(output):
            result = storage_service.get_video_preview_url(
                "clips/missing/video.mp4",
                "missing",
            )
        self.assertFalse(result["preview_available"])
        self.assertIn("reason=object_missing", output.getvalue())

    def test_unsafe_or_empty_key_is_not_signed(self):
        for key in ("", "../outside.mp4", "/clips/absolute.mp4"):
            with self.subTest(key=key):
                result = storage_service.get_video_preview_url(key, "fake")
                self.assertFalse(result["preview_available"])


class ClipListPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_queue_item_gets_dynamic_preview_fields(self):
        clip = {
            "id": "generated-legacy",
            "object_key": "clips/generated-legacy/video.mp4",
            "durable_url": None,
        }
        with patch(
            "main.get_video_preview_url",
            return_value={
                "durable_url": "",
                "preview_url": "https://signed.invalid/fresh",
                "preview_available": True,
                "source": "presigned_r2",
            },
        ) as signer:
            items = await main._attach_clip_preview_urls([clip])
        self.assertEqual(items[0]["preview_url"], "https://signed.invalid/fresh")
        self.assertTrue(items[0]["preview_available"])
        signer.assert_called_once_with(
            "clips/generated-legacy/video.mp4",
            "generated-legacy",
        )

    async def test_existing_durable_url_does_not_require_signing(self):
        clip = {
            "id": "generated-public",
            "object_key": "clips/generated-public/video.mp4",
            "durable_url": "https://media.invalid/clips/generated-public/video.mp4",
        }
        with patch("main.get_video_preview_url") as signer:
            items = await main._attach_clip_preview_urls([clip])
        self.assertEqual(items[0]["preview_url"], clip["durable_url"])
        self.assertTrue(items[0]["preview_available"])
        signer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
