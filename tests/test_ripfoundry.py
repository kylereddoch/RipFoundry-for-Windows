import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ripfoundry import (
    JobCancelled,
    MediaInfo,
    PROCESS_BOTH,
    PROCESS_ENHANCED,
    PROCESS_NONE,
    PROCESS_UPSCALE,
    Runner,
    analysis_summary,
    copy_verified,
    existing_video_base,
    is_ffmpeg_progress_line,
    is_supported_video,
    parse_ffmpeg_progress,
    parse_handbrake_progress,
    recommend_existing_processing,
)


def media(codec="h264", height=480, field_order="progressive"):
    return MediaInfo(codec, 120.0, 720, height, "4:3", 1, 0, field_order, "mov,mp4")


class RecommendationTests(unittest.TestCase):
    def test_supported_video_extensions(self):
        self.assertTrue(is_supported_video(Path("movie.MP4")))
        self.assertTrue(is_supported_video(Path("movie.m4v")))
        self.assertTrue(is_supported_video(Path("movie.mkv")))
        self.assertFalse(is_supported_video(Path("movie.avi")))

    def test_jellyfin_folder_name_becomes_output_base(self):
        source = Path(r"C:\Movies\Movie Name (2026) [tmdbid-1]\Movie Name (2026) [tmdbid-1].mp4")
        self.assertEqual(existing_video_base(source), "Movie Name (2026) [tmdbid-1]")

    def test_version_suffix_is_removed_outside_jellyfin_folder(self):
        source = Path(r"C:\Incoming\Movie Name - 480p.mp4")
        self.assertEqual(existing_video_base(source), "Movie Name")

    def test_recommendation_matrix(self):
        cases = [
            (media("mpeg2video", 480, "tt"), PROCESS_BOTH),
            (media("h264", 480), PROCESS_UPSCALE),
            (media("h264", 720), PROCESS_UPSCALE),
            (media("h264", 1080), PROCESS_NONE),
            (media("hevc", 1080), PROCESS_ENHANCED),
            (media("hevc", 2160), PROCESS_UPSCALE),
        ]
        for info, expected in cases:
            with self.subTest(info=info):
                self.assertEqual(recommend_existing_processing(info)[0], expected)

    def test_analysis_summary_explains_recommendation(self):
        info = media("h264", 480)
        recommendation, reason = recommend_existing_processing(info)
        summary = analysis_summary(Path("movie.mp4"), info, recommendation, reason)
        self.assertIn("Recommendation: 1080p only", summary)
        self.assertIn("Field order: progressive", summary)

    def test_ffmpeg_progress_parsing(self):
        self.assertAlmostEqual(parse_ffmpeg_progress("out_time_us=30000000", 120), 25.0)
        self.assertAlmostEqual(parse_ffmpeg_progress("out_time=00:01:00.000000", 120), 50.0)
        self.assertEqual(parse_ffmpeg_progress("progress=end", 120), 100.0)
        self.assertTrue(is_ffmpeg_progress_line("speed=0.55x"))
        self.assertFalse(is_ffmpeg_progress_line("Input #0, mov, from movie.mp4"))

    def test_handbrake_progress_parsing(self):
        line = "Encoding: task 1 of 1, 42.35 % (18.22 fps, avg 17.90 fps, ETA 00h10m12s)"
        self.assertAlmostEqual(parse_handbrake_progress(line), 42.35)
        self.assertIsNone(parse_handbrake_progress("Scanning title 1 of 1"))


class RunnerTests(unittest.TestCase):
    def settings(self, staging):
        return SimpleNamespace(
            staging=str(staging),
            ffmpeg=sys.executable,
            ffprobe=sys.executable,
            handbrake=sys.executable,
            makemkv=sys.executable,
            crf=18,
            preset="slow",
            hb_preset="medium",
        )

    def test_mp4_upscale_uses_subrip_and_disables_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "movie.mp4"
            output = root / "movie - 1080p.mkv"
            source.write_bytes(b"source")
            captured = []
            runner = Runner(self.settings(root / "stage"), lambda _message: None, lambda _value: None)

            def fake_run(args, capture=False, check=True, **_kwargs):
                captured.append(args)
                Path(args[-1]).write_bytes(b"encoded")
                return 0

            runner.run = fake_run
            runner.upscale(source, output, 120)
            args = captured[0]
            self.assertIn("-nostdin", args)
            self.assertIn("-progress", args)
            self.assertIn("pipe:1", args)
            self.assertIn("-nostats", args)
            self.assertEqual(args[args.index("-c:s") + 1], "srt")

    def test_cancel_stops_active_process_and_cleans_registered_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / "stage" / "process-video-jobs" / "cancel-test"
            stage.mkdir(parents=True)
            stage.joinpath("partial.mkv").write_bytes(b"partial")
            cancel_event = threading.Event()
            runner = Runner(
                self.settings(root / "stage"),
                lambda _message: None,
                lambda _value: None,
                cancel_event=cancel_event,
            )
            runner.register_cancel_cleanup(stage)
            result = []

            def run_slow_process():
                try:
                    runner.run([
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('started', flush=True); time.sleep(30)",
                    ])
                except Exception as exc:
                    result.append(exc)

            worker = threading.Thread(target=run_slow_process)
            worker.start()
            deadline = time.time() + 5
            while runner.current_process is None and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(runner.current_process)
            runner.cancel()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], JobCancelled)
            runner.cleanup_cancelled_job()
            self.assertFalse(stage.exists())

    def test_cancelled_ffmpeg_does_not_retry_with_fallback_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "movie.mp4"
            output = root / "movie - 1080p.mkv"
            source.write_bytes(b"source")
            calls = []
            runner = Runner(self.settings(root / "stage"), lambda _message: None, lambda _value: None)

            def cancelled_run(args, **_kwargs):
                calls.append(args)
                raise JobCancelled("cancelled")

            runner.run = cancelled_run
            with self.assertRaises(JobCancelled):
                runner.upscale(source, output, 120)
            self.assertEqual(len(calls), 1)

    def test_cancelled_copy_removes_partial_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mkv"
            destination = root / "library" / "movie.mkv"
            source.write_bytes(b"source bytes")

            def cancel():
                raise JobCancelled("cancelled")

            with self.assertRaises(JobCancelled):
                copy_verified(source, destination, lambda _message: None, cancel_check=cancel)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".partial").exists())

    def test_both_outputs_are_created_from_unchanged_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            movie = root / "Movie Name (2026) [tmdbid-1]"
            movie.mkdir()
            source = movie / "Movie Name (2026) [tmdbid-1].mp4"
            source.write_bytes(b"original source bytes")
            original = source.read_bytes()
            info = media("mpeg2video", 480, "tt")
            runner = Runner(self.settings(root / "stage"), lambda _message: None, lambda _value: None)
            runner.ffprobe = lambda _path: info
            runner.enhanced = lambda src, out, _duration=None: out.write_bytes(b"enhanced from " + src.read_bytes())
            runner.upscale = lambda src, out, _duration=None: out.write_bytes(b"1080 from " + src.read_bytes())
            runner.validate_processed = lambda _source_info, _output, target="1080": info

            completed = runner.process_existing(source, PROCESS_BOTH)

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(completed), 2)
            self.assertTrue(movie.joinpath("Movie Name (2026) [tmdbid-1] - 480p Enhanced.mkv").exists())
            self.assertTrue(movie.joinpath("Movie Name (2026) [tmdbid-1] - 1080p.mkv").exists())


if __name__ == "__main__":
    unittest.main()
