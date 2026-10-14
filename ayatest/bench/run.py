#!/usr/bin/env python3
"""Sequential, randomized uprobe trials; no external Python dependencies."""
import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import selectors
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class ShutdownTimeout(RuntimeError):
    pass


def stop(proc, timeout=30, repeat_sigint=False):
    if proc is None or proc.poll() is not None:
        return 0
    deadline = time.monotonic() + timeout
    signals = 0
    while proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        signals += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            proc.wait(timeout=min(1, remaining) if repeat_sigint else remaining)
            return signals
        except subprocess.TimeoutExpired:
            if not repeat_sigint or time.monotonic() >= deadline:
                break
    if proc.poll() is not None:
        return signals
    proc.kill()
    proc.wait()
    raise ShutdownTimeout("tracer did not stop within %g seconds; killed, sample invalid" % timeout)


def wait_measurement(target_proc, tracer, log, timeout):
    """Block on output instead of polling during the timed workload."""
    deadline = time.monotonic() + timeout
    output = ""
    with selectors.DefaultSelector() as selector:
        selector.register(target_proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("target measurement timeout")
            if tracer is not None and tracer.poll() is not None:
                raise RuntimeError("tracer exited during workload (%s)" % exit_reason(tracer))
            if not selector.select(min(remaining, 1)):
                continue
            data = os.read(target_proc.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError("target exited before measurement; see target log")
            chunk = data.decode(errors="replace")
            log.write(chunk)
            log.flush()
            output += chunk
            if re.search(r"^BENCH \{[^\n]*\}\n", output, re.M):
                return


def exit_reason(proc):
    code = proc.poll()
    if code is not None and code < 0:
        return "signal %s (%d)" % (signal.Signals(-code).name, -code)
    return "exit code %s" % code


def snapshot_ready(snapshot, target):
    """Require an enabled target event linked to a loaded BPF program."""
    if not isinstance(snapshot, dict):
        return False
    for link in snapshot.values():
        if link.get("type") != "bpf_link_handler":
            continue
        attr = link.get("attr", {})
        event = snapshot.get(str(attr.get("target_fd")), {})
        program = snapshot.get(str(attr.get("prog_fd")), {})
        if (event.get("type") == "bpf_perf_event_handler"
                and event.get("enabled") is True
                and event.get("attr", {}).get("_module_name") == str(target)
                and program.get("type") == "bpf_prog_handler"):
            return True
    return False


def bpftime_ready(args, env, stem, target):
    # Inspect only: do not preload server/agent into the inspection process.
    inspect_env = env.copy()
    inspect_env.pop("LD_PRELOAD", None)
    inspect_env["SPDLOG_LEVEL"] = "off"
    snapshot = stem.with_suffix(".handlers.json")
    with stem.with_suffix(".inspect.log").open("a") as log:
        result = subprocess.run([args.bpftimetool, "export", str(snapshot)],
                                env=inspect_env, stdout=log, stderr=subprocess.STDOUT,
                                timeout=min(args.timeout, 10))
    if result.returncode or not snapshot.exists():
        return False
    try:
        return snapshot_ready(json.loads(snapshot.read_text()), target)
    except (ValueError, OSError):
        return False


def trial(mode, index, args, target, out):
    stem = out / ("%02d-%s" % (index, mode))
    tracer_log = stem.with_suffix(".tracer.log")
    target_log = stem.with_suffix(".target.log")
    env = os.environ.copy()
    # Ambient preload/JIT options must not silently change the comparison.
    for key in list(env):
        if key == "LD_PRELOAD" or key.startswith("BPFTIME_"):
            del env[key]
    target_env = env.copy()
    command = None
    if mode == "aya":
        command = [str(ROOT / "target/release/aya-bench"),
                   str(ROOT / "ebpf/target/bpfel-unknown-none/release/bench-ebpf"), str(target)]
    elif mode in ("bpftrace", "bpftime"):
        script = stem.with_suffix(".bt")
        # Old bpftrace implements BEGIN as a uprobe in bpftrace itself.
        # A server-only bpftrace process cannot execute that userspace hook.
        begin = '' if mode == 'bpftime' else 'BEGIN { printf("READY\\n"); }\n'
        script.write_text(begin + 'uprobe:%s:bench_add { @hits[0] = count(); }\n' % target)
        # bpftrace 0.9.x has neither -q nor line buffering. Unbuffered
        # output also makes READY visible immediately on old and new versions.
        # Startup messages are harmless: both parsers match anchored lines.
        command = [args.bpftrace, "-B", "none", str(script)]
        if mode == "bpftime":
            # These switches are presence-based in bpftime: even "false"
            # enables kernel co-run / disables JIT. The cleanup above must
            # leave BPFTIME_RUN_WITH_KERNEL and BPFTIME_DISABLE_JIT absent.
            env.update(LD_PRELOAD=str(Path(args.bpftime_server).resolve()),
                       BPFTIME_USE_JIT="true", SPDLOG_LEVEL="info",
                       BPFTIME_GLOBAL_SHM_NAME="aya_bench_" + uuid.uuid4().hex,
                       BPFTIME_LOG_OUTPUT=str(stem.with_suffix(".runtime.log")))
            target_env.update(env)
            target_env["LD_PRELOAD"] = str(Path(args.bpftime_agent).resolve())
    proc = None
    target_proc = None
    shutdown_timeout = getattr(args, "shutdown_timeout", 30)
    result = None
    tracer_ready_ns = 0
    aya_timing = {}
    try:
        with tracer_log.open("w") as log:
            if command:
                tracer_started_ns = time.monotonic_ns()
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
                deadline = time.monotonic() + args.timeout
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError("tracer exited before workload (%s); see %s" %
                                           (exit_reason(proc), tracer_log))
                    ready = (bpftime_ready(args, env, stem, target) if mode == "bpftime"
                             else re.search(r"^READY$", tracer_log.read_text(errors="replace"), re.M))
                    if ready:
                        tracer_ready_ns = time.monotonic_ns() - tracer_started_ns
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("tracer readiness timeout; see " + str(tracer_log))
                    time.sleep(0.005)
                # Allow startup work to settle outside the timed target region.
                time.sleep(0.2)
            # taskset would itself inherit the agent. Instead set affinity in the
            # child before exec, so the preload only applies to the actual target.
            with target_log.open("w") as log_target:
                target_started_ns = time.monotonic_ns()
                target_proc = subprocess.Popen(
                    [str(target), str(args.iterations), str(args.warmup), str(args.sleep_ns), "--wait-exit"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=target_env,
                    preexec_fn=lambda: os.sched_setaffinity(0, {args.cpu}))
                wait_measurement(target_proc, proc, log_target, args.timeout)
            if target_proc.poll() is not None:
                raise RuntimeError("target did not wait for tracer shutdown; rebuild bench/build.sh")
            shutdown_signals = stop(proc, shutdown_timeout, repeat_sigint=(mode == "bpftime"))
            if proc is not None and proc.returncode != 0:
                raise RuntimeError("tracer exited unsuccessfully; see " + str(tracer_log))
            target_proc.stdin.close()
            # Drain post-measurement agent output while waiting for shutdown.
            target_proc.stdin = None
            tail, _ = target_proc.communicate(timeout=shutdown_timeout)
            with target_log.open("a") as log_target:
                log_target.write(tail.decode(errors="replace"))
            if target_proc.returncode:
                raise RuntimeError("target failed during shutdown; see " + str(target_log))
        matches = re.findall(r"^BENCH (\{.*\})$", target_log.read_text(errors="replace"), re.M)
        if len(matches) != 1:
            raise RuntimeError("missing/ambiguous target measurement")
        result = json.loads(matches[0])
        ready_matches = re.findall(r"^TARGET_READY (\{.*\})$", target_log.read_text(errors="replace"), re.M)
        if len(ready_matches) != 1:
            raise RuntimeError("missing target startup timestamp; rebuild with bash bench/build.sh")
        target_ready = json.loads(ready_matches[0])
        target_startup_ns = target_ready["monotonic_ns"] - target_started_ns
        if target_startup_ns <= 0:
            raise RuntimeError("invalid target startup timestamp")
        expected_sum = ((args.iterations * (args.iterations + 1) // 2
                         + args.warmup * (args.warmup + 1) // 2) % (1 << 64))
        if (result["checksum"] != expected_sum or result["iterations"] != args.iterations
                or result["warmup"] != args.warmup or result["sleep_ns"] != args.sleep_ns
                or result["elapsed_ns"] <= 0):
            raise RuntimeError("target result validation failed")
        hits = None
        if command:
            pattern = r"^HITS (\d+)$" if mode == "aya" else r"^@hits\[0\]:\s*(\d+)\s*$"
            counts = re.findall(pattern, tracer_log.read_text(errors="replace"), re.M)
            if len(counts) != 1:
                raise RuntimeError("missing/ambiguous probe count; see " + str(tracer_log))
            hits = int(counts[0])
            if hits != args.iterations + args.warmup:
                raise RuntimeError("probe count mismatch: got %d, expected %d" %
                                   (hits, args.iterations + args.warmup))
            if mode == "aya":
                timings = re.findall(r"^AYA_TIMING (\{.*\})$", tracer_log.read_text(), re.M)
                if len(timings) != 1:
                    raise RuntimeError("missing Aya attach timings; rebuild with bash bench/build.sh")
                aya_timing = json.loads(timings[0])
        return dict(mode=mode, round=index, status="ok", hits=hits,
                    shutdown_sigint_count=shutdown_signals,
                    tracer_startup_ready_ns=tracer_ready_ns,
                    target_startup_ready_ns=target_startup_ns,
                    setup_to_target_ready_ns=tracer_ready_ns + target_startup_ns,
                    aya_load_ns=aya_timing.get("load_ns"),
                    aya_attach_ns=aya_timing.get("attach_ns"),
                    timer_slack_ns=target_ready["timer_slack_ns"],
                    ns_per_call=result["elapsed_ns"] / args.iterations, **result)
    finally:
        try:
            stop(proc, shutdown_timeout, repeat_sigint=(mode == "bpftime"))
        finally:
            if target_proc is not None:
                if target_proc.poll() is None:
                    target_proc.kill()
                target_proc.wait()
                if target_proc.stdin is not None:
                    target_proc.stdin.close()
                target_proc.stdout.close()


def summarize(results, modes, sleeps):
    summary, setup = [], []
    for sleep_ns in sleeps:
        group = [r for r in results if r["sleep_ns"] == sleep_ns and r["status"] == "ok"]
        baseline = statistics.median(r["ns_per_call"] for r in group if r["mode"] == "baseline")
        baseline_start = statistics.median(r["target_startup_ready_ns"] for r in group if r["mode"] == "baseline")
        for mode in modes:
            rows = [r for r in group if r["mode"] == mode]
            values = [r["ns_per_call"] for r in rows]
            med = statistics.median(values)
            summary.append(dict(sleep_ns=sleep_ns, mode=mode, trials=len(values),
                                iterations=rows[0]["iterations"], warmup=rows[0]["warmup"],
                                median_ns_per_call=med, min_ns_per_call=min(values),
                                max_ns_per_call=max(values), overhead_ns=med-baseline,
                                slowdown=med/baseline, calls_per_second=1e9/med))
            timing = dict(sleep_ns=sleep_ns, mode=mode, trials=len(rows))
            for key in ("tracer_startup_ready_ns", "target_startup_ready_ns", "setup_to_target_ready_ns",
                        "aya_load_ns", "aya_attach_ns"):
                vals = [r[key] for r in rows if r[key] is not None]
                timing["median_" + key] = statistics.median(vals) if vals else None
                timing["min_" + key] = min(vals) if vals else None
                timing["max_" + key] = max(vals) if vals else None
            timing["setup_over_baseline_ns"] = timing["median_setup_to_target_ready_ns"] - baseline_start
            setup.append(timing)
    return summary, setup


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--modes", default="baseline,aya,bpftrace,bpftime")
    p.add_argument("--iterations", type=int, default=1000000)
    p.add_argument("--warmup", type=int, default=100000)
    p.add_argument("--sleep-ns", default="0", help="comma-separated nanosleep requests, e.g. 0,200,500,1000,5000")
    p.add_argument("--sleep-iterations", type=int, default=10000, help="iterations for nonzero sleep cases")
    p.add_argument("--sleep-warmup", type=int, default=1000, help="warmup for nonzero sleep cases")
    p.add_argument("--rounds", type=int, default=7)
    p.add_argument("--cpu", type=int, default=min(os.sched_getaffinity(0)))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--shutdown-timeout", type=float, default=30)
    p.add_argument("--shutdown-retries", type=int, default=1,
                   help="retry only tracer shutdown timeouts with fresh processes/shared memory")
    p.add_argument("--bpftrace", default="bpftrace")
    p.add_argument("--bpftime-server", default=os.getenv("BPFTIME_SERVER"))
    p.add_argument("--bpftime-agent", default=os.getenv("BPFTIME_AGENT"))
    p.add_argument("--bpftimetool", help="defaults to bpftimetool next to the server library")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    try:
        sleeps = [int(value) for value in args.sleep_ns.split(",")]
    except ValueError:
        p.error("sleep-ns must be comma-separated integer nanoseconds")
    if len(set(sleeps)) != len(sleeps) or any(s < 0 or s > 10**9 for s in sleeps):
        p.error("sleep-ns must contain unique values between 0 and 1000000000")
    modes = args.modes.split(",")
    if (len(set(modes)) != len(modes) or not set(modes) <= {"baseline", "aya", "bpftrace", "bpftime"}
            or "baseline" not in modes):
        p.error("modes must be unique, include baseline, and use baseline,aya,bpftrace,bpftime")
    if not (0 < args.iterations <= 10**12 and 0 < args.warmup <= 10**12
            and 0 < args.sleep_iterations <= 10**12 and 0 < args.sleep_warmup <= 10**12
            and args.rounds > 0 and args.timeout > 0 and args.shutdown_timeout > 0
            and 0 <= args.shutdown_retries <= 10):
        p.error("invalid iterations, warmup, rounds or timeout")
    if args.cpu not in os.sched_getaffinity(0):
        p.error("CPU is outside this process's allowed affinity")
    required = [ROOT / "bench/build/target"]
    if "aya" in modes:
        required += [ROOT / "target/release/aya-bench", ROOT / "ebpf/target/bpfel-unknown-none/release/bench-ebpf"]
    if set(modes) & {"bpftrace", "bpftime"} and not shutil.which(args.bpftrace):
        p.error("bpftrace not found; install it or choose --modes baseline,aya")
    if "bpftime" in modes:
        if not args.bpftime_server or not args.bpftime_agent:
            p.error("bpftime needs --bpftime-server and --bpftime-agent (absolute .so paths)")
        required += [Path(args.bpftime_server), Path(args.bpftime_agent)]
        args.bpftimetool = str(Path(args.bpftimetool).resolve() if args.bpftimetool
                              else Path(args.bpftime_server).resolve().parent / "bpftimetool")
        required.append(Path(args.bpftimetool))
    for path in required:
        if not path.is_file():
            p.error("missing file: " + str(path))
    out = (args.output or ROOT / "bench/results" / time.strftime("%Y%m%d-%H%M%S")).resolve()
    # bpftrace probe strings deliberately use simple paths.
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", str(out)):
        p.error("output path must use ASCII letters, numbers, _, -, /, . only")
    out.mkdir(parents=True, exist_ok=False)
    target = out / "target"
    shutil.copy2(ROOT / "bench/build/target", target)
    versions = {}
    for name, cmd in {"gcc": ["gcc", "--version"], "rustc": ["rustc", "--version"],
                      "bpftrace": [args.bpftrace, "--version"]}.items():
        try:
            versions[name] = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=10).decode().strip()
        except (OSError, subprocess.SubprocessError) as exc:
            versions[name] = str(exc)
    metadata = dict(arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                    uname=list(platform.uname()), versions=versions,
                    affinity=sorted(os.sched_getaffinity(0)), cpuinfo=Path("/proc/cpuinfo").read_text())
    metadata["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    metadata["system_settings"] = {}
    for path in ("/proc/loadavg", "/proc/sys/net/core/bpf_jit_enable",
                 "/sys/devices/system/cpu/intel_pstate/no_turbo",
                 "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor" % args.cpu,
                 "/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list" % args.cpu):
        try:
            metadata["system_settings"][path] = Path(path).read_text().strip()
        except OSError as exc:
            metadata["system_settings"][path] = str(exc)
    metadata["sha256"] = {}
    for path in required:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        metadata["sha256"][str(path)] = digest.hexdigest()
    source = out / "source"
    source.mkdir()
    for path in ("bench/run.py", "bench/target.c", "bench/build.sh", "src/bin/aya-bench.rs",
                 "ebpf/src/bin/bench-ebpf.rs", "Cargo.lock", "ebpf/Cargo.lock"):
        destination = source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    (out / "environment.json").write_text(json.dumps(metadata, indent=2))
    rng = random.Random(args.seed)
    results = []
    for sleep_ns in sleeps:
        (out / ("sleep-%dns" % sleep_ns)).mkdir()
    for index in range(1, args.rounds + 1):
        order = [(sleep_ns, mode) for sleep_ns in sleeps for mode in modes]
        rng.shuffle(order)
        for sleep_ns, mode in order:
            case = argparse.Namespace(**vars(args))
            case.sleep_ns = sleep_ns
            if sleep_ns:
                case.iterations, case.warmup = args.sleep_iterations, args.sleep_warmup
            print("round %d/%d: sleep=%dns %s (N=%d, warmup=%d)" %
                  (index, args.rounds, sleep_ns, mode, case.iterations, case.warmup), flush=True)
            for attempt in range(1, args.shutdown_retries + 2):
                case_out = out / ("sleep-%dns" % sleep_ns)
                if attempt > 1:
                    case_out /= "%02d-%s-retry%d" % (index, mode, attempt)
                    case_out.mkdir()
                try:
                    row = trial(mode, index, case, target, case_out)
                    row["attempt"] = attempt
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    row = dict(mode=mode, round=index, sleep_ns=sleep_ns, attempt=attempt,
                               status="error", error=str(exc))
                    results.append(row)
                    (out / "raw.json").write_text(json.dumps(results, indent=2))
                    if isinstance(exc, ShutdownTimeout) and attempt <= args.shutdown_retries:
                        print("Shutdown timed out; sample invalid, retrying with fresh processes", flush=True)
                        continue
                    raise RuntimeError("%s failed: %s; results: %s" % (mode, exc, out)) from exc
                results.append(row)
                (out / "raw.json").write_text(json.dumps(results, indent=2))
                break
    summary, setup = summarize(results, modes, sleeps)
    for name, rows in (("summary.csv", summary), ("attach_summary.csv", setup)):
        with (out / name).open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps(summary, indent=2))
    print("Startup/attach readiness (includes process startup/loading; not pure attach):")
    for row in setup:
        print("sleep=%dns %-8s tracer-ready=%.3fms target-startup=%.3fms total=%.3fms aya-attach=%s" %
              (row["sleep_ns"], row["mode"], row["median_tracer_startup_ready_ns"] / 1e6,
               row["median_target_startup_ready_ns"] / 1e6, row["median_setup_to_target_ready_ns"] / 1e6,
               "n/a" if row["median_aya_attach_ns"] is None else "%.3fms" % (row["median_aya_attach_ns"] / 1e6)))
    print("Results:", out)


if __name__ == "__main__":
    # Serializes this harness on this machine (bpftime may use global shared memory).
    with (ROOT / "bench/.run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
        except (RuntimeError, BlockingIOError) as exc:
            sys.exit(str(exc))
