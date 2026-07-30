from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import storage_service


class FakeClientError(Exception):
    def __init__(self):
        self.response = {
            "ResponseMetadata": {"HTTPStatusCode": 404},
            "Error": {"Code": "NoSuchKey", "Message": "missing"},
        }


class BandwidthSafeguardTests(unittest.TestCase):
    def setUp(self):
        media = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        media.write(b"final-video-bytes")
        media.close()
        self.media_path = Path(media.name)
        self.environment = {
            "OBJECT_STORAGE_ENABLED": "true",
            "OBJECT_STORAGE_ENDPOINT": "https://r2.invalid",
            "OBJECT_STORAGE_REGION": "auto",
            "OBJECT_STORAGE_BUCKET": "fake-bucket",
            "OBJECT_STORAGE_ACCESS_KEY_ID": "fake-key",
            "OBJECT_STORAGE_SECRET_ACCESS_KEY": "fake-secret",
            "OBJECT_STORAGE_PUBLIC_BASE_URL": "https://media.invalid",
        }

    def tearDown(self):
        self.media_path.unlink(missing_ok=True)

    def _client(self):
        return MagicMock()

    def _missing_object_error(self):
        return FakeClientError()

    def test_repeated_final_upload_is_idempotent(self):
        client = self._client()
        client.head_object.side_effect = [
            self._missing_object_error(),
            {"ContentLength": self.media_path.stat().st_size},
        ]
        output = io.StringIO()
        boto3_module = SimpleNamespace(client=MagicMock(return_value=client))
        with patch.dict(os.environ, self.environment, clear=False):
            with patch.dict(sys.modules, {"boto3": boto3_module}):
                with contextlib.redirect_stdout(output):
                    first = storage_service.upload_video(
                        str(self.media_path),
                        "FakeClip",
                    )
                    second = storage_service.upload_video(
                        str(self.media_path),
                        "FakeClip",
                    )
        self.assertEqual(first, second)
        client.upload_file.assert_called_once()
        self.assertIn("destination=r2", output.getvalue())
        self.assertIn("reason=object_already_exists", output.getvalue())

    def test_existing_final_object_skips_upload(self):
        client = self._client()
        client.head_object.return_value = {
            "ContentLength": self.media_path.stat().st_size,
        }
        boto3_module = SimpleNamespace(client=MagicMock(return_value=client))
        with patch.dict(os.environ, self.environment, clear=False):
            with patch.dict(sys.modules, {"boto3": boto3_module}):
                result = storage_service.upload_video(
                    str(self.media_path),
                    "FakeClip",
                )
        client.upload_file.assert_not_called()
        self.assertEqual(result["durable_url"].split("/")[2], "media.invalid")

    def test_frontend_uses_direct_r2_only_after_user_action(self):
        frontend_root = Path(__file__).resolve().parents[1] / "frontend" / "src"
        ai_clips = (
            frontend_root / "components" / "AIClips.jsx"
        ).read_text(encoding="utf-8")
        queue = (
            frontend_root / "components" / "UnpublishedQueue.jsx"
        ).read_text(encoding="utf-8")
        for source in (ai_clips, queue):
            self.assertIn("clip.durable_url", source)
            self.assertNotIn("/api/clips/${clip.id}/video", source)
            self.assertNotIn('preload="metadata"', source)
        self.assertIn('preload="none"', ai_clips)
        self.assertIn('preload="none"', queue)
        self.assertIn("previewClipId === clip.id", ai_clips)
        self.assertIn("previewClipId === clip.id", queue)

    def test_backend_video_endpoint_never_returns_file_response(self):
        main_source = (
            Path(__file__).resolve().parent / "main.py"
        ).read_text(encoding="utf-8")
        endpoint = main_source[
            main_source.index('@app.get("/api/clips/{clip_id}/video")'):
            main_source.index('@app.post("/api/publish")')
        ]
        self.assertNotIn("FileResponse", endpoint)
        self.assertIn("RedirectResponse", endpoint)
        self.assertIn("direct_r2_url", endpoint)

    def test_rejected_candidates_cannot_reach_storage_upload(self):
        main_source = (
            Path(__file__).resolve().parent / "main.py"
        ).read_text(encoding="utf-8")
        score_rejection = main_source.index(
            'best_clip["score"] < AUTO_CLIP_MIN_SCORE'
        )
        final_upload = main_source.index(
            "storage_result = await asyncio.to_thread("
        )
        self.assertLess(score_rejection, final_upload)


if __name__ == "__main__":
    unittest.main()
