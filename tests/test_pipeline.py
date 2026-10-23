import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from cgworkbench.core import (Project, Protected, Stopped, WorkbenchError, digest,
                              exclusive, frame_path, read_json, run, verify, write_json)
from cgworkbench.media import compare

WORKER = '''import sys, time
from PIL import Image
frame = int(sys.argv[1])
mode = sys.argv[3]
if mode == "partial" and frame == 2:
    sys.exit(0)
if mode == "timeout" and frame == 2:
    time.sleep(15)
if mode == "corrupt":
    open(sys.argv[2], "wb").write(b"broken PNG")
else:
    size = (4, 4) if mode == "wrong_size" else (16, 16)
    color = (0, 0, 0) if mode == "missing_subject" else (20, 80, 230)
    Image.new("RGB", size, color).save(sys.argv[2])
'''


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cg-workbench-test-")
        self.root = Path(self.tmp.name)
        (self.root / "worker.py").write_text(WORKER, encoding="utf-8")
        self.c = {"schema_version": 1, "name": "fixture", "inputs": ["worker.py"],
                  "command": ["{python}", "{project}/worker.py", "{frame}", "{output}", "normal"],
                  "output": "output", "frames": [1, 3], "image": [16, 16], "fps": 12,
                  "timeout_seconds": 5, "poll_seconds": 0.02,
                  "storage": {"reserve_bytes": 1, "job_budget_bytes": 1}}
        self.path = self.root / "project.json"

    def tearDown(self):
        self.tmp.cleanup()

    def project(self):
        write_json(self.path, self.c)
        return Project(self.path)

    def state(self):
        return read_json(self.root / "output" / "state.json")

    def test_resume_keeps_completed_file(self):
        p = self.project()
        with self.assertRaises(Stopped):
            run(p, max_new_frames=1)
        generation = p.output / self.state()["generation"]
        first = frame_path(generation, 1)
        before = first.stat().st_mtime_ns
        run(p)
        self.assertEqual(first.stat().st_mtime_ns, before)
        self.assertEqual(verify(p)["verified_frames"], 3)
        self.assertEqual(self.state()["visual"], "not_reviewed")

    def test_success_exit_with_partial_output_fails_and_preserves_first(self):
        self.c["command"][-1] = "partial"
        p = self.project()
        with self.assertRaises(WorkbenchError):
            run(p)
        self.assertEqual(self.state()["phase"], "failed")
        self.assertEqual(self.state()["completed_frames"], 1)
        self.assertEqual(verify(p)["technical"], "fail")

    def test_corrupt_and_wrong_size_rejected(self):
        for mode in ("corrupt", "wrong_size"):
            with self.subTest(mode=mode):
                self.c["command"][-1] = mode
                p = self.project()
                with self.assertRaises(WorkbenchError):
                    run(p)
                self.assertEqual(self.state()["completed_frames"], 0)

    def test_valid_image_missing_content_is_not_aesthetic_pass(self):
        self.c["command"][-1] = "missing_subject"
        p = self.project()
        self.assertEqual(run(p)["technical"], "pass")
        self.c["content_check"] = {"kind": "channel_presence", "roi": [0, 0, 16, 16],
                                   "channel": 2, "min_value": 200, "min_pixels": 100}
        with self.assertRaisesRegex(WorkbenchError, "content check failed"):
            run(self.project())
        self.c["command"][-1] = "normal"
        self.assertEqual(run(self.project())["technical"], "pass")

    def test_changed_input_new_generation_old_preserved(self):
        p = self.project()
        first = run(p)
        old = p.output / first["generation"]
        worker = self.root / "worker.py"
        worker.write_text(WORKER + "\n# changed dependency\n", encoding="utf-8")
        self.assertEqual(verify(p)["technical"], "fail")
        second = run(p)
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertTrue(frame_path(old, 3).is_file())

    def test_corrupt_cache_rerenders_only_that_frame(self):
        p = self.project()
        first = run(p)
        generation = p.output / first["generation"]
        untouched = frame_path(generation, 1).stat().st_mtime_ns
        frame_path(generation, 2).write_bytes(b"corrupt")
        self.assertEqual(verify(p)["technical"], "fail")
        run(p)
        self.assertEqual(frame_path(generation, 1).stat().st_mtime_ns, untouched)
        self.assertEqual(verify(p)["technical"], "pass")
        self.assertEqual(len(list((generation / "rejected").glob("*.png"))), 1)

    def test_decodable_tamper_is_not_accepted(self):
        p = self.project()
        result = run(p)
        generation = p.output / result["generation"]
        Image.new("RGB", (16, 16), "red").save(frame_path(generation, 1))
        self.assertIn("hash changed", verify(p)["problems"]["1"])

    def test_resource_protection_and_bounded_recovery(self):
        p = self.project()
        low = shutil._ntuple_diskusage(100, 100, 0)
        high = shutil._ntuple_diskusage(100, 0, 100)
        with patch("cgworkbench.core.shutil.disk_usage", return_value=low):
            with self.assertRaises(Protected):
                run(p)
        self.assertEqual(self.state()["phase"], "protected")
        # Initial check fails once, then real worker frames run after capacity recovers.
        with patch("cgworkbench.core.shutil.disk_usage", side_effect=[low] + [high] * 500):
            self.assertEqual(run(p, wait_seconds=0.02)["technical"], "pass")

    def test_runtime_resource_protection_preserves_receipts(self):
        p = self.project()
        with self.assertRaises(Stopped):
            run(p, max_new_frames=1)
        high = shutil._ntuple_diskusage(100, 0, 100)
        low = shutil._ntuple_diskusage(100, 100, 0)
        with patch("cgworkbench.core.shutil.disk_usage", side_effect=[high, low]):
            with self.assertRaises(Protected):
                run(p, max_recoveries=0)
        self.assertEqual(self.state()["completed_frames"], 1)
        self.assertEqual(run(p)["technical"], "pass")

    def test_timeout_is_not_retried(self):
        self.c["command"][-1] = "timeout"
        self.c["timeout_seconds"] = 0.8
        p = self.project()
        started = time.monotonic()
        with self.assertRaisesRegex(WorkbenchError, "timed out"):
            run(p)
        self.assertLess(time.monotonic()-started, 12)
        self.assertEqual(self.state()["completed_frames"], 1)

    def test_exclusive_lock(self):
        p = self.project()
        with exclusive(p.output / "workbench.lock"):
            with self.assertRaises(WorkbenchError):
                run(p)
        self.assertEqual(run(p)["technical"], "pass")

    def test_stop_file_interrupts_actual_worker_then_resumes(self):
        # Interrupt while a child is alive, not only between frames.
        self.c["command"][-1] = "timeout"
        self.c["timeout_seconds"] = 30
        self.project()
        stop = self.root / "STOP"
        process = subprocess.Popen([sys.executable, "-m", "cgworkbench", "run", str(self.path), "--stop-file", str(stop)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    state = self.state()
                    if state.get("frame") == 2 and state["phase"] == "rendering":
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(0.05)
            else:
                self.fail("Worker did not start")
            stop.touch()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 3, (stdout, stderr))
            self.assertEqual(self.state()["phase"], "paused")
            self.assertEqual(self.state()["completed_frames"], 1)
            stop.unlink()
            # The same input resumes using the existing frame; give frame 2 its full 15s.
            before = self.state()["generation"]
            result = run(Project(self.path))
            self.assertEqual(result["generation"], before)
            self.assertEqual(result["technical"], "pass")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_mutation_during_render_not_committed(self):
        p = self.project()
        from cgworkbench import core
        original = core.execute
        def mutate(*args, **kwargs):
            original(*args, **kwargs)
            (self.root / "worker.py").write_text(WORKER+"\n# changed during render", encoding="utf-8")
        with patch("cgworkbench.core.execute", side_effect=mutate):
            with self.assertRaisesRegex(WorkbenchError, "changed while rendering"):
                run(p)
        self.assertEqual(self.state()["completed_frames"], 0)

    def test_same_size_comparison_preserves_pixels(self):
        a, b, out = [self.root / name for name in ("a.png", "b.png", "comparison.png")]
        Image.new("RGB", (16, 16), "red").save(a)
        Image.new("RGB", (16, 16), "blue").save(b)
        compare(a, b, out, (2, 3, 14, 15))
        with Image.open(out) as im:
            self.assertEqual(im.size, (24, 40))
            self.assertEqual(im.getpixel((0, 28)), (255, 0, 0))
        with self.assertRaises(WorkbenchError):
            compare(a, b, out)

    def test_verification_detects_input_mutation(self):
        p = self.project()
        run(p)
        from cgworkbench import core
        original = core.verified_frames
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.root / "worker.py").write_text(WORKER + "\n# changed during verification", encoding="utf-8")
            return result
        with patch("cgworkbench.core.verified_frames", side_effect=mutate):
            report = verify(p)
        self.assertEqual(report["technical"], "fail")
        self.assertIn("inputs", report["problems"])

    def test_invalid_config_rejected(self):
        for key, value in [("frames", [3, 1]), ("fps", 0), ("poll_seconds", float("inf")), ("image", [0, 10])]:
            with self.subTest(key=key):
                original = self.c.get(key)
                self.c[key] = value
                with self.assertRaises((WorkbenchError, ValueError)):
                    self.project()
                self.c[key] = original


if __name__ == "__main__":
    unittest.main()
