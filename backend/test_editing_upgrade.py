from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
import numpy as np
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai
import main
import video_editing


class SpeechCaptionAndLayoutTests(unittest.TestCase):
    class _ReadableCapture:
        def isOpened(self):
            return True

        def get(self, property_id):
            values = {
                video_editing.cv2.CAP_PROP_FRAME_COUNT: 100,
                video_editing.cv2.CAP_PROP_FRAME_WIDTH: 1920,
                video_editing.cv2.CAP_PROP_FRAME_HEIGHT: 1080,
            }
            return values.get(property_id, 0)

        def set(self, _property_id, _value):
            pass

        def read(self):
            return True, np.zeros((270, 480, 3), dtype=np.uint8)

        def release(self):
            pass

    def test_captions_disabled_has_no_speech_dialogue(self):
        content = video_editing._build_overlay_ass_content(
            "Supported Title",
            48,
            [{"start": 0, "end": 2, "text": "private transcript profanity"}],
            speech_captions_enabled=False,
        )
        self.assertNotIn("Caption,,", content)
        self.assertNotIn("private transcript profanity", content)
        self.assertIn("TopTitle", content)

    def test_single_subject_fallback(self):
        layout = video_editing._single_subject_layout("low confidence", 0.4, 5)
        self.assertEqual(layout["mode"], "single_subject")
        self.assertEqual(layout["sample_count"], 5)

    def test_multi_person_split_uses_distinct_crops(self):
        with tempfile.NamedTemporaryFile(suffix=".ass") as overlay:
            graph = video_editing._build_filter_chain(
                Path(overlay.name),
                visual_layout={
                    "mode": "multi_person_split",
                    "regions": [
                        {"x": 0, "y": 0, "width": 0.52, "height": 1},
                        {"x": 0.48, "y": 0, "width": 0.52, "height": 1},
                    ],
                },
            )
        self.assertIn("split=2", graph)
        self.assertEqual(graph.count("split=2"), 1)
        self.assertNotIn("split=3", graph)
        self.assertEqual(graph.count("[layout_input_"), 4)
        self.assertIn("x='iw*0.00000'", graph)
        self.assertIn("x='iw*0.48000'", graph)
        self.assertIn("vstack=inputs=2", graph)

    def test_multi_person_evidence_selects_split_layout(self):
        faces = [
            {"center_x": 0.2, "width": 0.1, "height": 0.2},
            {"center_x": 0.8, "width": 0.1, "height": 0.2},
        ]
        with patch.object(
            video_editing.cv2,
            "VideoCapture",
            return_value=self._ReadableCapture(),
        ):
            with patch.object(
                video_editing,
                "_detect_face_like_regions",
                return_value=faces,
            ):
                layout = video_editing.detect_visual_layout("fake.mp4")
        self.assertEqual(layout["mode"], "multi_person_split")
        self.assertNotEqual(layout["regions"][0], layout["regions"][1])

    def test_reaction_split_retains_face_and_content_regions(self):
        with tempfile.NamedTemporaryFile(suffix=".ass") as overlay:
            graph = video_editing._build_filter_chain(
                Path(overlay.name),
                visual_layout={
                    "mode": "reaction_split",
                    "regions": [
                        {"x": 0, "y": 0, "width": 0.42, "height": 1},
                        {"x": 0.32, "y": 0, "width": 0.68, "height": 1},
                    ],
                },
            )
        self.assertIn("w='iw*0.42000'", graph)
        self.assertIn("w='iw*0.68000'", graph)
        self.assertIn("shortest=1:eof_action=endall", graph)

    def test_edge_webcam_evidence_selects_reaction_layout(self):
        faces = [{"center_x": 0.15, "width": 0.1, "height": 0.2}]
        with patch.object(
            video_editing.cv2,
            "VideoCapture",
            return_value=self._ReadableCapture(),
        ):
            with patch.object(
                video_editing,
                "_detect_face_like_regions",
                return_value=faces,
            ):
                layout = video_editing.detect_visual_layout("fake.mp4")
        self.assertEqual(layout["mode"], "reaction_split")
        self.assertEqual(len(layout["regions"]), 2)

    def test_layout_sample_count_is_capped(self):
        class FakeCapture:
            requested_frames = []

            def isOpened(self):
                return True

            def get(self, property_id):
                values = {
                    video_editing.cv2.CAP_PROP_FRAME_COUNT: 100,
                    video_editing.cv2.CAP_PROP_FRAME_WIDTH: 1920,
                    video_editing.cv2.CAP_PROP_FRAME_HEIGHT: 1080,
                }
                return values.get(property_id, 0)

            def set(self, _property_id, value):
                self.requested_frames.append(value)

            def read(self):
                return True, np.zeros((270, 480, 3), dtype=np.uint8)

            def release(self):
                pass

        capture = FakeCapture()
        sampled_widths = []

        def inspect_one_frame(frame):
            sampled_widths.append(frame.shape[1])
            return []

        with patch.dict(
            os.environ,
            {
                "VIDEO_LAYOUT_SAMPLE_COUNT": "99",
                "VIDEO_LAYOUT_SAMPLE_MAX_WIDTH": "999",
            },
        ):
            with patch.object(video_editing.cv2, "VideoCapture", return_value=capture):
                with patch.object(
                    video_editing,
                    "_detect_face_like_regions",
                    side_effect=inspect_one_frame,
                ):
                    video_editing.detect_visual_layout("fake.mp4")
        self.assertLessEqual(len(capture.requested_frames), 4)
        self.assertEqual(len(sampled_widths), len(capture.requested_frames))
        self.assertTrue(all(width <= 320 for width in sampled_widths))

    def test_low_memory_fallback_selects_single_subject(self):
        split_layout = {
            "mode": "reaction_split",
            "confidence": 0.9,
            "reason": "test",
            "version": "layout-v1",
            "sample_count": 3,
            "regions": [{"x": 0}, {"x": 0.5}],
        }
        with patch.dict(
            os.environ,
            {"VIDEO_LAYOUT_MEMORY_FALLBACK_MB": "120"},
        ):
            fallback = main._apply_visual_layout_memory_fallback(
                split_layout,
                119.9,
            )
        self.assertEqual(fallback["mode"], "single_subject")
        self.assertEqual(fallback["regions"], [])

    def test_layout_frames_are_processed_sequentially(self):
        state = {"active": 0, "maximum": 0, "calls": 0}

        def inspect_frame(_frame):
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            state["calls"] += 1
            state["active"] -= 1
            return []

        with patch.object(
            video_editing.cv2,
            "VideoCapture",
            return_value=self._ReadableCapture(),
        ):
            with patch.object(
                video_editing,
                "_detect_face_like_regions",
                side_effect=inspect_frame,
            ):
                video_editing.detect_visual_layout("fake.mp4")
        self.assertEqual(state["calls"], 3)
        self.assertEqual(state["maximum"], 1)

    @unittest.skipUnless(
        os.getenv("RUN_FFMPEG_LAYOUT_TESTS", "").lower() == "true",
        "set RUN_FFMPEG_LAYOUT_TESTS=true for render integration",
    )
    def test_split_layout_preserves_duration_and_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg and ffprobe are required")
        for duration in (2, 61):
            with self.subTest(duration=duration):
                with tempfile.TemporaryDirectory() as directory:
                    raw_path = Path(directory) / "source.mp4"
                    output_path = Path(directory) / "output.mp4"
                    subprocess.run(
                        [
                            ffmpeg, "-y",
                            "-f", "lavfi", "-i",
                            f"testsrc=size=320x180:rate=24:duration={duration}",
                            "-f", "lavfi", "-i",
                            f"sine=frequency=440:duration={duration}",
                            "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "aac", "-shortest", str(raw_path),
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    overlay_path = Path(directory) / "overlay.ass"
                    overlay_path.write_text("", encoding="utf-8")
                    graph = video_editing._build_filter_chain(
                        overlay_path,
                        visual_layout={
                            "mode": "multi_person_split",
                            "regions": [
                                {"x": 0, "y": 0, "width": 0.52, "height": 1},
                                {"x": 0.48, "y": 0, "width": 0.52, "height": 1},
                            ],
                        },
                    )
                    graph = graph.rsplit(";", 1)[0] + ";[composed]null"
                    subprocess.run(
                        [
                            ffmpeg, "-y", "-threads", "1", "-i", str(raw_path),
                            "-vf", graph, "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-b:a", "128k", "-t", str(duration), str(output_path),
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    probe = subprocess.run(
                        [
                            ffprobe, "-v", "error", "-show_streams",
                            "-show_format", "-of", "json", str(output_path),
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        text=True,
                    )
                    metadata = json.loads(probe.stdout)
                    output_duration = float(metadata["format"]["duration"])
                    self.assertAlmostEqual(output_duration, duration, delta=0.25)
                    self.assertTrue(
                        any(
                            stream.get("codec_type") == "audio"
                            for stream in metadata["streams"]
                        )
                    )


class TitleQualityTests(unittest.TestCase):
    def _response(self, title):
        return SimpleNamespace(output_text=title)

    def test_supported_title_passes_without_retry(self):
        transcript = "player lands impossible clutch and wins final round"
        with patch.object(
            ai.client.responses,
            "create",
            return_value=self._response("Player Lands Impossible Clutch And Wins 🔥"),
        ) as create:
            package = ai.generate_ai_title_package(
                transcript,
                {"score_hook": "impossible clutch wins final round"},
            )
        self.assertEqual(create.call_count, 1)
        self.assertFalse(package["title_fallback_used"])
        self.assertGreaterEqual(package["title_relevance_score"], 0.45)

    def test_unsupported_title_triggers_one_retry(self):
        with patch.object(
            ai.client.responses,
            "create",
            side_effect=[
                self._response("Banana Orchestra Starts Dancing Tonight 😳"),
                self._response("Player Lands Final Round Clutch Win 🔥"),
            ],
        ) as create:
            package = ai.generate_ai_title_package(
                "player lands a final round clutch win",
                {"score_hook": "final round clutch win"},
            )
        self.assertEqual(create.call_count, 2)
        self.assertFalse(package["title_fallback_used"])

    def test_failed_retry_uses_deterministic_fallback(self):
        with patch.object(
            ai.client.responses,
            "create",
            return_value=self._response("Banana Orchestra Starts Dancing Tonight 😳"),
        ) as create:
            package = ai.generate_ai_title_package(
                "player lands a final round clutch win",
                {"score_hook": "player lands final round clutch win"},
            )
        self.assertEqual(create.call_count, 2)
        self.assertTrue(package["title_fallback_used"])

    def test_card_title_has_one_trailing_relevant_emoji(self):
        title = ai._sanitize_generated_title(
            "Player Lands The Final Clutch Win 😳 😂",
            "player lands the final clutch win",
        )
        emojis = ai._TITLE_EMOJI_PATTERN.findall(title)
        self.assertEqual(len(emojis), 1)
        self.assertTrue(title.endswith(emojis[0]))

    def test_burned_title_is_emoji_free_and_two_lines(self):
        prepared, font_size, _ = video_editing._prepare_title_text(
            "Player Lands The Impossible Final Round Clutch Win 🔥"
        )
        self.assertNotRegex(prepared, video_editing.TITLE_EMOJI_PATTERN)
        self.assertLessEqual(len(prepared.splitlines()), 2)
        self.assertGreaterEqual(font_size, video_editing.TITLE_MIN_FONT_SIZE)


if __name__ == "__main__":
    unittest.main()
