import sys
import tempfile
import threading
import time
import unittest
import queue
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ripfoundry import (
    JobCancelled,
    MediaInfo,
    App,
    DiscTitle,
    ExtraMetadata,
    MovieMetadata,
    PROCESS_BOTH,
    PROCESS_ENHANCED,
    PROCESS_NONE,
    PROCESS_UPSCALE,
    Runner,
    StagedExtra,
    StagedExtrasBatch,
    analysis_summary,
    choose_folder,
    copy_verified,
    existing_video_base,
    is_ffmpeg_progress_line,
    is_supported_video,
    movie_library_base,
    parse_ffmpeg_progress,
    parse_handbrake_progress,
    parse_makemkv_scan_output,
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

    def test_high_noon_scan_output_returns_all_seven_titles(self):
        records = ['CINFO:1,6206,"DVD disc"', 'CINFO:2,0,"HIGH_NOON"']
        title_data = [
            (0, "21", "1:24:33", "4.5 GB", "4902707200", "E1_t00.mkv"),
            (1, "13", "0:22:04", "1.1 GB", "1217841152", "C1_t01.mkv"),
            (2, "2", "0:09:47", "506.7 MB", "531394560", "B1_t02.mkv"),
            (3, "2", "0:01:44", "59.3 MB", "62201856", "D1_t03.mkv"),
            (4, "2", "0:01:16", "62.2 MB", "65286144", "D3_t04.mkv"),
            (5, "2", "0:01:35", "56.5 MB", "59250688", "D2_t05.mkv"),
            (6, "1", "0:05:37", "9.2 MB", "9744384", "B4_t06.mkv"),
        ]
        for title_id, chapters, duration, size, size_bytes, output_name in title_data:
            records.extend([
                f'TINFO:{title_id},8,0,"{chapters}"',
                f'TINFO:{title_id},9,0,"{duration}"',
                f'TINFO:{title_id},10,0,"{size}"',
                f'TINFO:{title_id},11,0,"{size_bytes}"',
                f'TINFO:{title_id},27,0,"{output_name}"',
            ])

        titles, hint = parse_makemkv_scan_output("\n".join(records))

        self.assertEqual(len(titles), 7)
        self.assertEqual(hint, "High Noon")
        self.assertEqual(titles[0].duration_seconds, 5073)
        self.assertEqual(titles[0].size_bytes, 4902707200)
        self.assertEqual(titles[6].output_name, "B4_t06.mkv")


class FolderPickerTests(unittest.TestCase):
    def test_windows_picker_returns_selected_folder(self):
        parent = object()
        with mock.patch("ripfoundry.os.name", "nt"), mock.patch(
            "ripfoundry._choose_windows_folder", return_value=r"C:\Movies\High Noon"
        ) as picker:
            selected = choose_folder(parent, r"C:\Movies", "Choose a movie folder")

        self.assertEqual(selected, r"C:\Movies\High Noon")
        picker.assert_called_once_with(parent, r"C:\Movies", "Choose a movie folder")

    def test_windows_picker_falls_back_when_modern_dialog_is_unavailable(self):
        parent = object()
        with mock.patch("ripfoundry.os.name", "nt"), mock.patch(
            "ripfoundry._choose_windows_folder", side_effect=OSError("unavailable")
        ), mock.patch(
            "ripfoundry.filedialog.askdirectory", return_value=r"D:\Movies"
        ) as fallback:
            selected = choose_folder(parent, "D:\\", "Choose a folder")

        self.assertEqual(selected, r"D:\Movies")
        fallback.assert_called_once_with(parent=parent, initialdir="D:\\", title="Choose a folder")


class ScanWorkflowTests(unittest.TestCase):
    def test_scan_uses_job_lifecycle_and_queues_result(self):
        class FakeLabel:
            def __init__(self):
                self.text = ""

            def configure(self, text):
                self.text = text

        class FakeRunner:
            def __init__(self):
                self.phases = []

            def begin_phase(self, message):
                self.phases.append(message)

            def scan_disc(self):
                return [object()] * 7, "High Noon"

        runner = FakeRunner()
        events = queue.Queue()
        app = SimpleNamespace(
            job_running=False,
            disc_label=FakeLabel(),
            q=events,
            save_settings_silent=lambda: None,
            runner=lambda: runner,
        )
        lifecycle_calls = []

        def threaded(work, job_kind="processing"):
            lifecycle_calls.append(True)
            self.assertEqual(job_kind, "scan")
            work()

        app.threaded = threaded

        App.scan(app)

        self.assertEqual(lifecycle_calls, [True])
        self.assertEqual(runner.phases, ["Scanning DVD..."])
        self.assertIn("about a minute", app.disc_label.text)
        kind, (titles, hint) = events.get_nowait()
        self.assertEqual(kind, "scan_result")
        self.assertEqual(len(titles), 7)
        self.assertEqual(hint, "High Noon")


class RunnerTests(unittest.TestCase):
    def settings(self, staging):
        return SimpleNamespace(
            staging=str(staging),
            ffmpeg=sys.executable,
            ffprobe=sys.executable,
            handbrake=sys.executable,
            makemkv=sys.executable,
            movies=str(Path(staging).parent / "library"),
            dvd_source="disc:0",
            scan_min_length=60,
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

    def test_cancel_stops_active_captured_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cancel_event = threading.Event()
            runner = Runner(
                self.settings(root / "stage"),
                lambda _message: None,
                lambda _value: None,
                cancel_event=cancel_event,
            )
            result = []

            def run_slow_process():
                try:
                    runner.run([
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('started', flush=True); time.sleep(30)",
                    ], capture=True)
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

    def test_streamed_command_failure_includes_recent_tool_output(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = Runner(self.settings(Path(temp) / "stage"), lambda _message: None, lambda _value: None)
            with self.assertRaisesRegex(RuntimeError, "specific failure detail"):
                runner.run([
                    sys.executable,
                    "-c",
                    "import sys; print('specific failure detail'); sys.exit(7)",
                ])

    def test_stage_extra_titles_creates_lossless_review_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.settings(root / "stage")
            progress = []
            runner = Runner(settings, lambda _message: None, progress.append)
            commands = []

            def fake_run(args, **_kwargs):
                commands.append(args)
                output = Path(args[-1])
                output.joinpath("generated.mkv").write_bytes(f"title {args[4]}".encode())
                return 0

            runner.run = fake_run
            runner.ffprobe = lambda _path: media("mpeg2video", 480, "tt")
            titles = [
                DiscTitle(1, duration="0:22:04", size="1.1 GB"),
                DiscTitle(2, duration="0:09:47", size="506.7 MB"),
            ]

            batch = runner.stage_extra_titles(titles, MovieMetadata("High Noon", "1952", 288))

            self.assertEqual([command[1] for command in commands], ["--minlength=60", "--minlength=60"])
            self.assertEqual([command[4] for command in commands], ["1", "2"])
            self.assertEqual(len(batch.items), 2)
            self.assertTrue(batch.items[0].path.is_file())
            self.assertIn("DVD Title 1 - 0-22-04.mkv", batch.items[0].path.name)
            self.assertEqual(batch.items[0].path.read_bytes(), b"title 1")
            self.assertEqual(progress, [0, 50, 50, 100])

    def test_publish_extras_uses_parent_movie_and_jellyfin_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.settings(root / "stage")
            movies = Path(settings.movies)
            movies.mkdir()
            review = root / "stage" / "extra-review" / "batch"
            review.mkdir(parents=True)
            first = review / "title-1.mkv"
            second = review / "title-2.mkv"
            first.write_bytes(b"making of")
            second.write_bytes(b"interview")
            batch = StagedExtrasBatch(review, [
                StagedExtra(DiscTitle(1, duration="0:22:04"), first),
                StagedExtra(DiscTitle(2, duration="0:05:37"), second),
            ])
            meta = MovieMetadata("High Noon", "1952", 288)
            runner = Runner(settings, lambda _message: None, lambda _value: None)

            completed = runner.publish_extras(batch, meta, [
                ExtraMetadata("The Making of High Noon", "featurettes"),
                ExtraMetadata("Tex Ritter Radio Interview", "interviews"),
            ])

            movie_root = movies / movie_library_base(meta)
            self.assertEqual(completed, [
                movie_root / "featurettes" / "The Making of High Noon.mkv",
                movie_root / "interviews" / "Tex Ritter Radio Interview.mkv",
            ])
            self.assertEqual(completed[0].read_bytes(), b"making of")
            self.assertEqual(completed[1].read_bytes(), b"interview")
            self.assertFalse(review.exists())

    def test_publish_extras_rejects_duplicate_destination_before_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.settings(root / "stage")
            Path(settings.movies).mkdir()
            review = root / "stage" / "extra-review" / "batch"
            review.mkdir(parents=True)
            first = review / "title-1.mkv"
            second = review / "title-2.mkv"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            batch = StagedExtrasBatch(review, [
                StagedExtra(DiscTitle(1), first),
                StagedExtra(DiscTitle(2), second),
            ])
            runner = Runner(settings, lambda _message: None, lambda _value: None)

            with self.assertRaisesRegex(RuntimeError, "same destination"):
                runner.publish_extras(
                    batch,
                    MovieMetadata("High Noon", "1952", 288),
                    [ExtraMetadata("Duplicate", "featurettes"), ExtraMetadata("duplicate", "featurettes")],
                )

            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_publish_extras_rolls_back_completed_destinations_when_batch_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.settings(root / "stage")
            movies = Path(settings.movies)
            movies.mkdir()
            review = root / "stage" / "extra-review" / "batch"
            review.mkdir(parents=True)
            first = review / "title-1.mkv"
            second = review / "title-2.mkv"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            batch = StagedExtrasBatch(review, [
                StagedExtra(DiscTitle(1), first),
                StagedExtra(DiscTitle(2), second),
            ])
            runner = Runner(settings, lambda _message: None, lambda _value: None)
            copy_count = 0

            def fail_second_copy(source, destination, *_args):
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.with_name(destination.name + ".partial").write_bytes(b"partial")
                    raise RuntimeError("second copy failed")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())

            with mock.patch("ripfoundry.copy_verified", side_effect=fail_second_copy):
                with self.assertRaisesRegex(RuntimeError, "second copy failed"):
                    runner.publish_extras(
                        batch,
                        MovieMetadata("High Noon", "1952", 288),
                        [ExtraMetadata("First", "featurettes"), ExtraMetadata("Second", "interviews")],
                    )

            movie_root = movies / movie_library_base(MovieMetadata("High Noon", "1952", 288))
            self.assertFalse(movie_root.joinpath("featurettes", "First.mkv").exists())
            self.assertFalse(movie_root.joinpath("interviews", "Second.mkv").exists())
            self.assertFalse(movie_root.joinpath("interviews", "Second.mkv.partial").exists())
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

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
