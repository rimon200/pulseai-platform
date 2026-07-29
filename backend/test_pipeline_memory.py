from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def _successful_score(self):
        return {
            "score": 80,
            "reason": "fake",
            "hook": "fake",
            "visual_score": 80,
            "transcript_score": 80,
            "context_score": 80,
            "confidence": 80,
            "decision": "accept",
        }

    def _evaluate_successfully(self, video_path, memory_results):
        with contextlib.ExitStack() as stack:
            download = stack.enter_context(
                patch.object(
                    main,
                    "download_twitch_clip",
                    return_value=str(video_path),
                )
            )
            admission = stack.enter_context(
                patch.object(
                    main,
                    "_whisper_memory_admitted",
                    side_effect=memory_results,
                )
            )
            sleep = stack.enter_context(patch.object(main.time, "sleep"))
            transcribe = stack.enter_context(
                patch.object(
                    main,
                    "_transcribe_video_with_segments_subprocess",
                    return_value={
                        "transcript": "fake transcript",
                        "segments": [],
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_score_multimodal_clip_subprocess",
                    return_value=self._successful_score(),
                )
            )
            stack.enter_context(patch.object(main, "release_whisper_model"))
            result = main._fully_evaluate_candidate(
                **self._candidate_arguments()
            )
        return result, download, admission, sleep, transcribe

    def test_low_memory_before_download_triggers_one_bounded_recheck(self):
        with patch.object(
            main,
            "_whisper_memory_admitted",
            side_effect=[
                (False, 100.0, 180.0),
                (False, 110.0, 180.0),
            ],
        ) as admission:
            with patch.object(main.time, "sleep") as sleep:
                with patch("builtins.print") as log:
                    admitted, _, _ = main._admit_candidate_batch_memory(3, 1)

        self.assertFalse(admitted)
        self.assertEqual(admission.call_count, 2)
        sleep.assert_called_once_with(3.0)
        output = "\n".join(
            " ".join(str(argument) for argument in call.args)
            for call in log.call_args_list
        )
        self.assertIn("stage=before_download", output)
        self.assertIn("available_mb=110.0", output)
        self.assertIn("required_mb=180.0", output)
        self.assertIn("decision=defer", output)

    def test_recovered_memory_before_download_permits_download(self):
        with patch.object(
            main,
            "_whisper_memory_admitted",
            side_effect=[
                (False, 100.0, 180.0),
                (True, 190.0, 180.0),
            ],
        ) as admission:
            with patch.object(main.time, "sleep") as sleep:
                admitted, _, _ = main._admit_candidate_batch_memory(3, 1)

        self.assertTrue(admitted)
        self.assertEqual(admission.call_count, 2)
        sleep.assert_called_once_with(3.0)
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertLess(
            source.index("_admit_candidate_batch_memory(",
                         source.index("async def _run_auto_generate_clip_pipeline")),
            source.index("_fully_evaluate_candidate(",
                         source.index("async def _run_auto_generate_clip_pipeline")),
        )

    def test_low_memory_after_download_gets_one_recheck_and_cleans_file(self):
        video_path = self._download_file()
        with patch.object(
            main,
            "download_twitch_clip",
            return_value=str(video_path),
        ):
            with patch.object(
                main,
                "_whisper_memory_admitted",
                side_effect=[
                    (False, 100.0, 180.0),
                    (False, 110.0, 180.0),
                    (False, 110.0, 180.0),
                ],
            ) as admission:
                with patch.object(main.time, "sleep") as sleep:
                    with patch("builtins.print") as log:
                        with patch.object(
                            main,
                            "_transcribe_video_with_segments_subprocess",
                        ) as transcribe:
                            result = main._fully_evaluate_candidate(
                                **self._candidate_arguments()
                            )
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_stage"], "whisper_admission")
        self.assertTrue(result["memory_rejected_after_download"])
        self.assertEqual(admission.call_count, 3)
        sleep.assert_called_once_with(3.0)
        transcribe.assert_not_called()
        self.assertFalse(video_path.exists())
        output = "\n".join(
            " ".join(str(argument) for argument in call.args)
            for call in log.call_args_list
        )
        self.assertIn("stage=before_whisper", output)
        self.assertIn("available_mb=110.0", output)
        self.assertIn("required_mb=180.0", output)
        self.assertIn("decision=defer", output)

    def test_recovered_memory_after_download_permits_whisper(self):
        video_path = self._download_file()
        try:
            result, download, admission, sleep, transcribe = (
                self._evaluate_successfully(
                    video_path,
                    [
                        (False, 100.0, 180.0),
                        (True, 190.7, 180.0),
                    ],
                )
            )
            self.assertTrue(result["success"])
            self.assertEqual(admission.call_count, 2)
            sleep.assert_called_once_with(3.0)
            download.assert_called_once()
            transcribe.assert_called_once_with(str(video_path))
        finally:
            video_path.unlink(missing_ok=True)

    def test_recheck_cooldown_is_bounded_and_not_repeated(self):
        with patch.dict(
            os.environ,
            {"WHISPER_MEMORY_RECHECK_SECONDS": "999"},
        ):
            with patch.object(
                main,
                "_whisper_memory_admitted",
                side_effect=[
                    (False, 100.0, 180.0),
                    (False, 100.0, 180.0),
                ],
            ) as admission:
                with patch.object(main.time, "sleep") as sleep:
                    admitted, _, _ = main._admit_candidate_batch_memory(3, 1)

        self.assertFalse(admitted)
        self.assertEqual(admission.call_count, 2)
        sleep.assert_called_once_with(30.0)

    def test_persistent_low_memory_continues_rescue_contract(self):
        video_path = self._download_file()
        with patch.object(
            main,
            "download_twitch_clip",
            return_value=str(video_path),
        ):
            with patch.object(
                main,
                "_whisper_memory_admitted",
                side_effect=[
                    (False, 100.0, 180.0),
                    (False, 100.0, 180.0),
                    (False, 100.0, 180.0),
                ],
            ):
                with patch.object(main.time, "sleep"):
                    result = main._fully_evaluate_candidate(
                        **self._candidate_arguments()
                    )

        self.assertFalse(result["success"])
        self.assertTrue(result["memory_rejected_after_download"])
        self.assertFalse(video_path.exists())
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn('("rescue", rescue_candidate_numbers)', source)
        self.assertIn('if not evaluation_result["success"]:', source)

    def test_sufficient_memory_proceeds_to_isolated_whisper(self):
        video_path = self._download_file()
        try:
            result, download, admission, sleep, transcribe = (
                self._evaluate_successfully(
                    video_path,
                    [
                        (True, 250.0, 180.0),
                    ],
                )
            )
            self.assertTrue(result["success"])
            self.assertEqual(admission.call_count, 1)
            sleep.assert_not_called()
            download.assert_called_once()
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


class NativeMemoryLifecycleTests(unittest.TestCase):
    def test_native_trim_is_safe_when_libc_is_unavailable(self):
        with patch.object(
            main.ctypes,
            "CDLL",
            side_effect=OSError("libc unavailable"),
        ):
            self.assertFalse(main._trim_native_memory("test_unavailable"))

    def test_layout_detection_runs_in_waited_subprocess(self):
        layout = {
            "mode": "single_subject",
            "confidence": 1.0,
            "reason": "fake",
            "version": "layout-v1",
            "sample_count": 3,
            "regions": [],
        }

        def complete_worker(command, **kwargs):
            output_path = Path(
                command[command.index("--output-json") + 1]
            )
            output_path.write_text(json.dumps(layout), encoding="utf-8")
            return MagicMock(stderr="")

        with patch.object(
            main.subprocess,
            "run",
            side_effect=complete_worker,
        ) as run:
            result = main._detect_visual_layout_subprocess("/fake/video.mp4")

        self.assertEqual(result, layout)
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_unrecovered_batch_admission_precedes_all_downloads(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        pipeline_start = source.index(
            "async def _run_auto_generate_clip_pipeline"
        )
        admission = source.index(
            "_admit_candidate_batch_memory(",
            pipeline_start,
        )
        evaluation = source.index(
            "_fully_evaluate_candidate(",
            pipeline_start,
        )
        self.assertLess(admission, evaluation)
        self.assertIn(
            '"Clip generation deferred because worker memory did not "',
            source[admission:evaluation],
        )


if __name__ == "__main__":
    unittest.main()
