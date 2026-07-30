from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from fastapi import HTTPException

import main


class TikTokDraftPublishingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        media = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        media.write(b"fake-video")
        media.close()
        self.video_path = Path(media.name)
        self.clip = {
            "id": "fake-generated-clip",
            "twitch_clip_id": "FakeTwitchClip",
            "public_url": "https://clips.twitch.tv/FakeTwitchClip",
            "title": "Fake clip",
            "video_path": str(self.video_path),
            "status": "ready_for_review",
            "actual_duration": 30,
        }

    def tearDown(self):
        self.video_path.unlink(missing_ok=True)

    def _publishing_context(self, post_mode: str):
        return (
            patch.object(main.Path, "open", mock_open()),
            patch.object(main.json, "load", return_value=[dict(self.clip)]),
            patch.object(main, "load_published", return_value=[]),
            patch.object(
                main,
                "get_publish_settings",
                AsyncMock(return_value={"post_mode": post_mode}),
            ),
        )

    async def test_draft_upload_succeeds_without_creator_info(self):
        creator_info = AsyncMock(
            side_effect=AssertionError("creator_info must not run for drafts")
        )
        contexts = self._publishing_context("draft")
        with contexts[0], contexts[1], contexts[2], contexts[3]:
            with patch.object(
                main, "_get_tiktok_creator_info", creator_info
            ):
                with patch.object(
                    main, "_claim_clip_for_publishing", return_value=True
                ) as claim:
                    with patch.object(
                        main,
                        "upload_tiktok_draft",
                        AsyncMock(
                            return_value={
                                "publish_id": "fake-publish-id",
                                "upload_result": {"accepted": True},
                            }
                        ),
                    ) as upload:
                        with patch.object(
                            main,
                            "_mark_clip_uploaded_to_inbox",
                            return_value=True,
                        ) as mark_uploaded:
                            result = await main.publish_clip_to_tiktok(self.clip)

        creator_info.assert_not_awaited()
        claim.assert_called_once()
        upload.assert_awaited_once_with(
            str(self.video_path),
            "",
            "fake-generated-clip",
        )
        mark_uploaded.assert_called_once()
        self.assertEqual(
            mark_uploaded.call_args.args[1],
            "fake-publish-id",
        )
        self.assertTrue(result["success"])

    async def test_draft_prefers_tiktok_pull_from_r2(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": {"publish_id": "fake-pull-id"},
            "error": {"code": "ok"},
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.put = AsyncMock()
        client_context = MagicMock()
        client_context.__aenter__ = AsyncMock(return_value=client)
        client_context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(
            main,
            "_load_provider_token_data",
            return_value={"access_token": "fake-token"},
        ):
            with patch.object(
                main,
                "_extract_tiktok_token_fields",
                return_value={"access_token": "fake-token"},
            ):
                with patch.object(
                    main,
                    "_is_tiktok_access_token_expiring",
                    return_value=False,
                ):
                    with patch.object(
                        main.httpx,
                        "AsyncClient",
                        return_value=client_context,
                    ):
                        result = await main.upload_tiktok_draft(
                            "",
                            "https://media.invalid/clips/fake.mp4",
                        )
        self.assertEqual(result["publish_id"], "fake-pull-id")
        self.assertEqual(result["transfer_method"], "PULL_FROM_URL")
        client.put.assert_not_awaited()
        self.assertEqual(
            client.post.await_args.kwargs["json"]["source_info"]["source"],
            "PULL_FROM_URL",
        )

    async def test_direct_post_still_requires_creator_info(self):
        creator_error = HTTPException(
            status_code=502,
            detail="TikTok creator publishing capabilities are unavailable.",
        )
        contexts = self._publishing_context("direct")
        with contexts[0], contexts[1], contexts[2], contexts[3]:
            with patch.object(
                main,
                "_get_tiktok_creator_info",
                AsyncMock(side_effect=creator_error),
            ) as creator_info:
                with patch.object(
                    main, "_claim_clip_for_publishing"
                ) as claim:
                    with patch.object(
                        main, "upload_tiktok_draft", AsyncMock()
                    ) as upload:
                        with self.assertRaises(HTTPException) as raised:
                            await main.publish_clip_to_tiktok(self.clip)

        creator_info.assert_awaited_once()
        claim.assert_not_called()
        upload.assert_not_awaited()
        self.assertEqual(raised.exception.status_code, 502)

    async def test_tiktok_failure_restores_publishing_claim(self):
        upload_error = HTTPException(
            status_code=502,
            detail="TikTok draft video upload failed.",
        )
        output = io.StringIO()
        contexts = self._publishing_context("draft")
        with contexts[0], contexts[1], contexts[2], contexts[3]:
            with patch.object(
                main, "_get_tiktok_creator_info", AsyncMock()
            ) as creator_info:
                with patch.object(
                    main, "_claim_clip_for_publishing", return_value=True
                ):
                    with patch.object(
                        main,
                        "upload_tiktok_draft",
                        AsyncMock(side_effect=upload_error),
                    ):
                        with patch.object(
                            main,
                            "_restore_clip_after_publish_failure",
                            return_value=True,
                        ) as restore:
                            with contextlib.redirect_stdout(output):
                                with self.assertRaises(HTTPException) as raised:
                                    await main.publish_clip_to_tiktok(self.clip)

        creator_info.assert_not_awaited()
        restore.assert_called_once()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("TIKTOK DRAFT UPLOAD FAILED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
