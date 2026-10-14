"""Run after bench/build.sh: python3 -m unittest discover -s bench -p 'test_*.py'."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import os

from run import summarize, trial, stop, ShutdownTimeout


class BenchmarkTest(unittest.TestCase):
    def test_shutdown_resends_signal_and_keeps_timeout_invalid(self):
        class Process:
            returncode = None
            signals = 0
            killed = False
            def poll(self):
                return self.returncode
            def send_signal(self, sig):
                self.signals += 1
            def wait(self, timeout=None):
                if not self.killed and self.signals < 2:
                    raise subprocess.TimeoutExpired("fake", timeout)
                self.returncode = -9 if self.killed else 0
                return self.returncode
            def kill(self):
                self.killed = True
        proc = Process()
        self.assertEqual(stop(proc, timeout=2, repeat_sigint=True), 2)
        self.assertFalse(proc.killed)
        proc = Process()
        with self.assertRaises(ShutdownTimeout):
            stop(proc, timeout=0)
        self.assertTrue(proc.killed)

    def test_trial_collects_timings_and_rejects_bad_probe_count(self):
        # Protocol-level test with a fake tracer; this does not test kernel BPF.
        target = Path(__file__).resolve().parent / "build/target"
        original_popen = subprocess.Popen
        for hits in (110, 109):
            with self.subTest(hits=hits), tempfile.TemporaryDirectory(dir=target.parent) as directory:
                out = Path(directory).resolve()
                tracer = out / "fake_tracer.py"
                tracer.write_text(
                    "import signal,sys,time\n"
                    "def done(*args):\n"
                    "    print('HITS %d', flush=True)\n" % hits +
                    "    sys.exit(0)\n"
                    "signal.signal(signal.SIGINT, done)\n"
                    "print('AYA_TIMING {\"load_ns\":300,\"attach_ns\":200}', flush=True)\n"
                    "print('READY', flush=True)\n"
                    "while True: time.sleep(1)\n")

                def launch(command, *args, **kwargs):
                    if str(command[0]).endswith("/aya-bench"):
                        command = [sys.executable, str(tracer)]
                    return original_popen(command, *args, **kwargs)

                args = SimpleNamespace(iterations=100, warmup=10, sleep_ns=200,
                                       cpu=min(os.sched_getaffinity(0)), timeout=10)
                with patch("run.subprocess.Popen", side_effect=launch):
                    if hits != 110:
                        with self.assertRaisesRegex(RuntimeError, "probe count mismatch"):
                            trial("aya", 1, args, target, out)
                    else:
                        row = trial("aya", 1, args, target, out)
                        self.assertEqual(row["aya_attach_ns"], 200)
                        self.assertEqual(row["aya_load_ns"], 300)
                        self.assertGreater(row["target_startup_ready_ns"], 0)
                        self.assertGreater(row["tracer_startup_ready_ns"], 0)
                        self.assertEqual(row["setup_to_target_ready_ns"],
                                         row["target_startup_ready_ns"] + row["tracer_startup_ready_ns"])

    def test_target_sleep_cases_and_startup_clock(self):
        target = Path(__file__).resolve().parent / "build/target"
        for sleep_ns in (0, 200, 500, 1000, 5000):
            with self.subTest(sleep_ns=sleep_ns):
                start = time.monotonic_ns()
                output = subprocess.check_output([str(target), "100", "10", str(sleep_ns)], text=True)
                end = time.monotonic_ns()
                ready, measured = [json.loads(line.split(" ", 1)[1]) for line in output.splitlines()]
                self.assertLessEqual(start, ready["monotonic_ns"])
                self.assertLessEqual(ready["monotonic_ns"], end)
                self.assertEqual(measured["sleep_ns"], sleep_ns)
                self.assertEqual(measured["checksum"], 5105)
                self.assertEqual(measured["iterations"], 100)
                self.assertEqual(measured["warmup"], 10)
                self.assertGreaterEqual(measured["elapsed_ns"], sleep_ns * 100)

    def test_each_sleep_uses_its_own_baseline(self):
        rows = []
        for delay, baseline in ((0, 10), (200, 50000)):
            for index in range(3):
                for mode, delta in (("baseline", 0), ("aya", 2000)):
                    rows.append(dict(sleep_ns=delay, mode=mode, status="ok", ns_per_call=baseline+delta,
                                     iterations=100, warmup=10, tracer_startup_ready_ns=0 if not delta else 100,
                                     target_startup_ready_ns=20, setup_to_target_ready_ns=20 if not delta else 120,
                                     aya_load_ns=None if not delta else 60, aya_attach_ns=None if not delta else 30))
        summary, timing = summarize(rows, ["baseline", "aya"], [0, 200])
        self.assertEqual([r["overhead_ns"] for r in summary], [0, 2000, 0, 2000])
        self.assertEqual([r["trials"] for r in summary], [3]*4)
        self.assertEqual([r["setup_over_baseline_ns"] for r in timing], [0, 100, 0, 100])
        self.assertIsNone(timing[0]["median_aya_attach_ns"])
        self.assertEqual(timing[1]["median_aya_attach_ns"], 30)


if __name__ == "__main__":
    unittest.main()
