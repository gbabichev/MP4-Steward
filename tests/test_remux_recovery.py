#!/usr/bin/env python3
"""Real-media regression tests. Run with Python 3 and ffmpeg/ffprobe on PATH.

Optional: --samples /path/to/folder also converts and validates existing MKVs
into temporary MP4s. Source samples are never changed or deleted.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import MP4_Steward as steward


SHIM = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ["RECOVERY_STATE"])
calls = json.loads(state.read_text()) if state.exists() else []
calls.append(args)
state.write_text(json.dumps(calls))
case = os.environ["RECOVERY_CASE"]
first = len(calls) == 1
if case == "fallback_failure" or (first and case in ("remux_exit", "disk_full", "permission")):
    print({"disk_full": "No space left on device", "permission": "Permission denied"}
          .get(case, "Injected mux failure"), file=sys.stderr)
    sys.exit(1)
if first and case == "truncated":
    args[-1:-1] = ["-t", "2"]
result = subprocess.run([os.environ["REAL_FFMPEG"], *args], stdout=subprocess.DEVNULL)
if result.returncode == 0:
    # Reproduce older FFmpeg's subtitle-based progress even with newer builds.
    print("out_time_us=500000\nout_time=00:00:00.500000\nprogress=end", flush=True)
sys.exit(result.returncode)
'''


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.digest()


class RemuxRecoveryTests(unittest.TestCase):
    samples = None

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.ffprobe = shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("Install ffmpeg and ffprobe first")
        cls.directory = tempfile.TemporaryDirectory(prefix="mp4-steward-tests-")
        cls.work = Path(cls.directory.name)
        subtitle = cls.work / "short.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nShort subtitle\n")
        cls.fixture = cls.work / "source.mkv"
        subprocess.run([
            cls.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=64x64:rate=24:duration=12", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=12", "-i", str(subtitle),
            "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264",
            "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt", str(cls.fixture),
        ], check=True)
        cls.incompatible = cls.work / "ffv1.mkv"
        subprocess.run([
            cls.ffmpeg, "-v", "error", "-i", str(cls.fixture), "-map", "0",
            "-c", "copy", "-c:v", "ffv1", str(cls.incompatible),
        ], check=True)
        cls.shim = cls.work / "ffmpeg-shim"
        cls.shim.write_text(SHIM)
        cls.shim.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        steward.CANCELLATION_REQUESTED.clear()
        self.log_patch = mock.patch.object(steward, "LOGFILE", str(self.work / "converter.log"))
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.addCleanup(steward.CANCELLATION_REQUESTED.clear)

    def check_case(self, case, status, calls, *, mode="remux", codec="hevc"):
        source = self.incompatible if case == "unsupported_codec" else self.fixture
        original_hash = digest(source)
        output = self.work / f"{mode}-{case}.mp4"
        state = self.work / f"{mode}-{case}.json"
        state.unlink(missing_ok=True)
        real_runner = steward.run_ffmpeg

        def injected_runner(command, *args, **kwargs):
            return real_runner([str(self.shim), *command[1:]], *args, **kwargs)

        if case == "cancelled":
            steward.CANCELLATION_REQUESTED.set()
        logs = io.StringIO()
        with mock.patch.dict(os.environ, {
            "REAL_FFMPEG": self.ffmpeg, "RECOVERY_CASE": case, "RECOVERY_STATE": str(state),
        }), mock.patch.object(steward, "run_ffmpeg", side_effect=injected_runner), \
                contextlib.redirect_stdout(logs):
            result = steward.convert_to_mp4(str(source), str(output), mode)
        self.assertEqual(result.status, status, logs.getvalue())
        commands = json.loads(state.read_text()) if state.exists() else []
        self.assertEqual(len(commands), calls, commands)
        self.assertEqual(digest(source), original_hash)
        self.assertFalse(list(self.work.glob("*.av.mp4")), "Intermediate output leaked")
        if status == "success":
            probe = steward.probe_media(str(output))
            self.assertAlmostEqual(steward.media_duration(probe), 12, delta=0.1)
            self.assertEqual(steward.get_video_codec(probe), codec)
            self.assertEqual(sum(s["codec_type"] == "subtitle" for s in probe["streams"]), 1)
            self.assertTrue(all(command[-1].endswith(".mp4") for command in commands))
            self.assertIn("checking the completed output with ffprobe", logs.getvalue())
        if case == "truncated":
            self.assertIn("video duration mismatch", logs.getvalue())
        if case == "fallback_failure":
            self.assertIn("Remux failed:", result.reason)
            self.assertIn("H.265 MP4 fallback failed:", result.reason)
        return result

    def test_short_subtitle_progress_preserves_remux(self):
        self.check_case("short_progress", "success", 1, codec="h264")

    def test_failed_remux_encodes_once(self):
        self.check_case("remux_exit", "success", 3)

    def test_truncated_output_is_rejected_and_reencoded(self):
        self.check_case("truncated", "success", 3)

    def test_failed_fallback_does_not_loop(self):
        self.check_case("fallback_failure", "failed", 2)

    def test_storage_errors_do_not_retry(self):
        for case in ("disk_full", "permission"):
            with self.subTest(case=case):
                self.check_case(case, "failed", 1)

    def test_cancellation_does_not_start_ffmpeg(self):
        self.check_case("cancelled", "cancelled", 0)

    def test_cancelled_remux_does_not_retry(self):
        with mock.patch.object(steward, "run_ffmpeg", return_value=
                               steward.FFmpegRunResult(False, True, 0, "Cancelled")) as runner, \
                contextlib.redirect_stdout(io.StringIO()):
            result = steward.convert_to_mp4(str(self.fixture), str(self.work / "cancel.mp4"), "remux")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(runner.call_count, 1)

    def test_incompatible_remux_encodes_directly(self):
        self.check_case("unsupported_codec", "success", 2)

    def test_smart_remux_failure_also_recovers(self):
        self.check_case("remux_exit", "success", 3, mode="smart")

    def test_existing_samples(self):
        if self.samples is None:
            self.skipTest("Pass --samples to test existing MKVs")
        sources = sorted(self.samples.glob("*.mkv"))
        self.assertTrue(sources, "No MKV samples found")
        for source in sources:
            with self.subTest(sample=source.name):
                original_hash = digest(source)
                output = self.work / "sample-output.mp4"
                with contextlib.redirect_stdout(io.StringIO()):
                    result = steward.convert_to_mp4(str(source), str(output), "remux")
                self.assertTrue(result.success, result.reason)
                self.assertAlmostEqual(steward.media_duration(steward.probe_media(str(source))),
                                       steward.media_duration(steward.probe_media(str(output))), delta=0.1)
                self.assertEqual(digest(source), original_hash)
                output.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path)
    options, remaining = parser.parse_known_args()
    RemuxRecoveryTests.samples = options.samples
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
