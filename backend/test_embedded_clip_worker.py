from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import clip_worker
import main


class EmbeddedWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_result_requires_a_real_clip_id(self):
        with patch.object(main, "_ensure_clip_history_table", return_value=True):
            with patch.object(
                main,
                "_run_auto_generate_clip_pipeline",
                new=AsyncMock(
                    return_value={
                        "id": "generated-clip-1",
                        "_job_candidates_examined": 2,
                        "_job_candidates_rejected": 1,
                    }
                ),
            ):
                result = await clip_worker.evaluate_claimed_job(
                    {"id": "job-success"},
                    "worker",
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "clip_created")
        self.assertEqual(result["result_clip_id"], "generated-clip-1")

    async def test_no_clip_pipeline_result_is_not_clip_created(self):
        with patch.object(main, "_ensure_clip_history_table", return_value=True):
            with patch.object(
                main,
                "_run_auto_generate_clip_pipeline",
                new=AsyncMock(
                    return_value={
                        "message": "No viral clips found.",
                        "outcome_reason": "score_threshold",
                        "_job_candidates_examined": 2,
                        "_job_candidates_rejected": 2,
                    }
                ),
            ):
                result = await clip_worker.evaluate_claimed_job(
                    {"id": "job-no-clip"},
                    "worker",
                )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "no_clip_found")
        self.assertIsNone(result["result_clip_id"])
        self.assertEqual(
            result["error_message"],
            clip_worker.NO_CLIP_FOUND_MESSAGE,
        )

    async def test_auto_endpoint_only_enqueues_and_returns_202(self):
        queued = {
            "id": "00000000-0000-0000-0000-000000000001",
            "status": "queued",
        }
        with patch.object(
            main,
            "enqueue_generation_job",
            return_value=(queued, True),
        ):
            with patch.object(
                main,
                "_run_auto_generate_clip_pipeline",
                new=AsyncMock(),
            ) as pipeline:
                response = await main.auto_generate_clip()
        self.assertEqual(response.status_code, 202)
        pipeline.assert_not_awaited()

    async def test_low_memory_defers_without_spawning_child(self):
        stop_event = asyncio.Event()
        job = {"id": "job-low-memory"}

        def defer(*_args):
            stop_event.set()
            return True

        with patch.object(
            clip_worker,
            "embedded_worker_ownership_is_alive",
            return_value=True,
        ):
            with patch.object(
                clip_worker,
                "claim_generation_job",
                return_value=job,
            ):
                with patch.object(
                    clip_worker,
                    "_child_memory_admitted",
                    return_value=(False, 120.0, 180.0),
                ):
                    with patch.object(
                        clip_worker,
                        "defer_generation_job",
                        side_effect=defer,
                    ) as deferred:
                        with patch.object(
                            clip_worker,
                            "_run_claimed_job_child",
                            new=AsyncMock(),
                        ) as child:
                            await clip_worker._owned_worker_loop(
                                stop_event,
                                "worker",
                                0.01,
                                object(),
                            )
        deferred.assert_called_once()
        child.assert_not_awaited()

    async def test_claimed_children_run_sequentially(self):
        stop_event = asyncio.Event()
        jobs = [{"id": "job-1"}, {"id": "job-2"}]
        active = 0
        maximum = 0

        async def run_child(job, _worker_id):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            if job["id"] == "job-2":
                stop_event.set()
            return {"status": "completed", "result_clip_id": job["id"]}

        with patch.object(
            clip_worker,
            "embedded_worker_ownership_is_alive",
            return_value=True,
        ):
            with patch.object(
                clip_worker,
                "claim_generation_job",
                side_effect=jobs,
            ):
                with patch.object(
                    clip_worker,
                    "_child_memory_admitted",
                    return_value=(True, 250.0, 180.0),
                ):
                    with patch.object(
                        clip_worker,
                        "_run_claimed_job_child",
                        side_effect=run_child,
                    ):
                        with patch.object(
                            clip_worker,
                            "_apply_terminal_result",
                            return_value=True,
                        ) as finalized:
                            await clip_worker._owned_worker_loop(
                                stop_event,
                                "worker",
                                0.01,
                                object(),
                            )
        self.assertEqual(maximum, 1)
        self.assertEqual(finalized.call_count, 2)

    async def test_child_is_waited_and_result_file_is_removed(self):
        job = {"id": "job-child"}
        fake_process = MagicMock()
        fake_process.returncode = None
        fake_process.pid = 43210

        async def wait():
            fake_process.returncode = 0
            return 0

        fake_process.wait = AsyncMock(side_effect=wait)

        async def create_process(*arguments, **_kwargs):
            result_path = arguments[arguments.index("--result-file") + 1]
            with open(result_path, "w", encoding="utf-8") as result_file:
                json.dump(
                    {"status": "completed", "result_clip_id": "clip-1"},
                    result_file,
                )
            return fake_process

        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=create_process,
        ):
            result = await clip_worker._run_claimed_job_child(job, "worker")
        self.assertEqual(result["status"], "completed")
        fake_process.wait.assert_awaited()

    async def test_shutdown_terminates_and_reaps_child(self):
        process = MagicMock()
        process.returncode = None
        process.pid = 43211

        async def wait():
            process.returncode = -15
            return -15

        process.wait = AsyncMock(side_effect=wait)
        with patch.object(
            clip_worker.os,
            "killpg",
            side_effect=PermissionError,
        ):
            await clip_worker._terminate_and_reap(process)
        process.terminate.assert_called_once()
        process.wait.assert_awaited()

    async def test_standby_process_does_not_enter_owned_loop(self):
        stop_event = asyncio.Event()

        async def stop_after_wait():
            await asyncio.sleep(0)
            stop_event.set()

        waiter = asyncio.create_task(stop_after_wait())
        with patch.object(
            clip_worker,
            "ensure_generation_jobs_table",
            return_value=True,
        ):
            with patch.object(
                clip_worker,
                "try_acquire_embedded_worker_ownership",
                return_value=None,
            ):
                with patch.object(
                    clip_worker,
                    "_owned_worker_loop",
                    new=AsyncMock(),
                ) as owned_loop:
                    await clip_worker.run_worker_loop(
                        stop_event,
                        embedded=True,
                        poll_seconds=0.01,
                    )
        await waiter
        owned_loop.assert_not_awaited()

    def test_candidate_rejection_log_has_structured_reason(self):
        with patch("builtins.print") as log:
            main._log_candidate_rejection(
                {
                    "twitch_clip_id": "FakeCandidate",
                    "creator": "FakeStreamer",
                    "score": 32,
                },
                "score_threshold",
            )
        output = log.call_args.args[0]
        self.assertIn("streamer=FakeStreamer", output)
        self.assertIn("candidate_identifier=FakeCandidate", output)
        self.assertIn("viral_score=32.00", output)
        self.assertIn("rejection_reason=score_threshold", output)


if __name__ == "__main__":
    unittest.main()
