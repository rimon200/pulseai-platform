from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from download_service import DownloadService


class FakeYtDlpProcess:
    def __init__(self, output: str, return_code: int = 0):
        self.stdout = io.StringIO(output)
        self.return_code = return_code
        self.finished = False
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.finished = True
        return self.return_code

    def poll(self):
        return self.return_code if self.finished else None

    def terminate(self):
        self.terminate_calls += 1
        self.finished = True

    def kill(self):
        self.kill_calls += 1
        self.finished = True


class YtDlpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service = DownloadService()

    def test_process_is_reaped_and_stream_closed_on_success(self):
        process = FakeYtDlpProcess("download complete\n")
        stdout = process.stdout
        with patch("download_service.subprocess.Popen", return_value=process):
            self.service._run_ytdlp_process(["yt-dlp", "fake"])
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertTrue(process.finished)
        self.assertTrue(stdout.closed)

    def test_process_is_reaped_and_stream_closed_on_failure(self):
        process = FakeYtDlpProcess("download failed\n", return_code=1)
        stdout = process.stdout
        with patch("download_service.subprocess.Popen", return_value=process):
            with self.assertRaises(Exception):
                self.service._run_ytdlp_process(["yt-dlp", "fake"])
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertTrue(process.finished)
        self.assertTrue(stdout.closed)

    def test_output_logging_is_bounded(self):
        process = FakeYtDlpProcess(
            "".join(f"line {index}\n" for index in range(500))
        )
        captured = io.StringIO()
        with patch("download_service.subprocess.Popen", return_value=process):
            with contextlib.redirect_stdout(captured):
                self.service._run_ytdlp_process(["yt-dlp", "fake"])
        emitted_lines = captured.getvalue().splitlines()
        self.assertLessEqual(len(emitted_lines), 201)
        self.assertIn("additional output suppressed", emitted_lines[-1])


class GenerationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.app.state.clip_generation_busy = False
        main.app.state.clip_generation_db_lease = None

    async def asyncTearDown(self):
        if main.app.state.clip_generation_busy:
            await main.end_clip_generation()

    async def test_manual_and_automatic_generation_cannot_overlap(self):
        with patch.object(
            main,
            "_try_acquire_generation_db_lease",
            return_value=True,
        ):
            first = await main.try_begin_clip_generation()
            second = await main.try_begin_clip_generation()
        self.assertTrue(first)
        self.assertFalse(second)

    async def test_database_lease_rejection_prevents_generation(self):
        with patch.object(
            main,
            "_try_acquire_generation_db_lease",
            return_value=None,
        ):
            admitted = await main.try_begin_clip_generation()
        self.assertFalse(admitted)


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL", "").strip(),
    "TEST_DATABASE_URL is required for cross-worker lease test",
)
class PostgreSQLGenerationLeaseTests(unittest.TestCase):
    def test_only_one_database_session_acquires_generation_lease(self):
        with patch.object(main, "DATABASE_URL", os.environ["TEST_DATABASE_URL"]):
            first_lease = main._try_acquire_generation_db_lease()
            try:
                second_lease = main._try_acquire_generation_db_lease()
                self.assertIsNotNone(first_lease)
                self.assertIsNone(second_lease)
            finally:
                main._release_generation_db_lease(first_lease)


class WhisperAdmissionTests(unittest.TestCase):
    def _candidate_arguments(self):
        return {
            "twitch_clip": {
                "id": "FAKE_MEMORY_CLIP",
                "url": "https://clips.twitch.tv/FAKE_MEMORY_CLIP",
                "duration": 30,
            },
            "candidate_number": 1,
            "total_candidates": 3,
            "creator": {"name": "Fake Creator"},
            "stream": {"game_name": "Fake Game"},
            "stream_title": "Fake Stream",
            "viewer_count": 10,
            "persisted_clips": [],
        }

    def _download_file(self) -> Path:
        downloads_dir = Path(main.BASE_DIR) / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=downloads_dir,
            suffix=".mp4",
            delete=False,
        )
        handle.write(b"fake-video-data")
        handle.close()
        return Path(handle.name)

    def test_low_memory_rejects_whisper_and_cleans_download(self):
        video_path = self._download_file()
        with patch.object(
            main,
            "download_twitch_clip",
            return_value=str(video_path),
        ):
            with patch.object(
                main,
                "_whisper_memory_admitted",
                return_value=(False, 100.0, 180.0),
            ):
                with patch.object(
                    main,
                    "_transcribe_video_with_segments_subprocess",
                ) as transcribe:
                    result = main._fully_evaluate_candidate(
                        **self._candidate_arguments()
                    )
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_stage"], "whisper_admission")
        transcribe.assert_not_called()
        self.assertFalse(video_path.exists())

    def test_sufficient_memory_proceeds_to_isolated_whisper(self):
        video_path = self._download_file()
        try:
            with patch.object(
                main,
                "download_twitch_clip",
                return_value=str(video_path),
            ):
                with patch.object(
                    main,
                    "_whisper_memory_admitted",
                    return_value=(True, 250.0, 180.0),
                ):
                    with patch.object(
                        main,
                        "_transcribe_video_with_segments_subprocess",
                        return_value={
                            "transcript": "fake transcript",
                            "segments": [],
                        },
                    ) as transcribe:
                        with patch.object(
                            main,
                            "score_multimodal_clip",
                            return_value={
                                "score": 80,
                                "reason": "fake",
                                "hook": "fake",
                                "visual_score": 80,
                                "transcript_score": 80,
                                "context_score": 80,
                                "confidence": 80,
                                "decision": "accept",
                            },
                        ):
                            with patch.object(main, "release_whisper_model"):
                                result = main._fully_evaluate_candidate(
                                    **self._candidate_arguments()
                                )
            self.assertTrue(result["success"])
            transcribe.assert_called_once_with(str(video_path))
        finally:
            video_path.unlink(missing_ok=True)

    def test_failed_candidate_result_remains_rescue_eligible(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn('("rescue", rescue_candidate_numbers)', source)
        self.assertIn('if not evaluation_result["success"]:', source)
        self.assertIn("continue", source[source.index(
            'if not evaluation_result["success"]:'
        ):source.index(
            'clip = evaluation_result["clip"]'
        )])


if __name__ == "__main__":
    unittest.main()
