"""One-frame transactions, immutable input generations, bounded supervision.

Only declared input files are tracked. Commands are trusted local programs,
not sandboxed plugins. No automatic deletion and no retry of unknown errors.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import PIL
from PIL import Image


class WorkbenchError(RuntimeError):
    pass


class Protected(WorkbenchError):
    pass


class Stopped(WorkbenchError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


@contextmanager
def exclusive(path):
    """OS-released lock; lock-file existence alone never means a live owner."""
    with Path(path).open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkbenchError("Another operation owns this output root") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkbenchError(f"{name} must be a finite number")
    if value < 0 or (not zero and value == 0):
        raise WorkbenchError(f"Invalid {name}")
    return value


class Project:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.base = self.path.parent
        self.config = c = read_json(self.path)
        allowed = {"schema_version", "name", "inputs", "command", "output", "frames", "image",
                   "fps", "timeout_seconds", "poll_seconds", "storage", "content_check"}
        if set(c) - allowed:
            raise WorkbenchError(f"Unknown config keys: {sorted(set(c) - allowed)}")
        if c.get("schema_version") != 1:
            raise WorkbenchError("Expected schema_version 1")
        self.start, self.end = c["frames"]
        if any(type(x) is not int for x in (self.start, self.end)) or self.start < 1 or self.end < self.start:
            raise WorkbenchError("frames must be inclusive positive [start, end]")
        if len(c["image"]) != 2 or any(type(x) is not int or x < 1 for x in c["image"]):
            raise WorkbenchError("image must be [width, height]")
        positive(c["fps"], "fps")
        positive(c.get("timeout_seconds", 600), "timeout_seconds")
        positive(c.get("poll_seconds", 1), "poll_seconds")
        if not isinstance(c["command"], list) or not c["command"] or not all(isinstance(x, str) for x in c["command"]):
            raise WorkbenchError("command must be a nonempty argument array")
        if not any("{output}" in arg for arg in c["command"]):
            raise WorkbenchError("command must use {output}")
        self.output = (self.base / c["output"]).resolve()
        if not c["inputs"] or not all(isinstance(x, str) for x in c["inputs"]):
            raise WorkbenchError("Declare every file used by the renderer in inputs")
        self.inputs = [(name, (self.base / name).resolve()) for name in c["inputs"]]
        for name, p in self.inputs:
            if not p.is_file():
                raise WorkbenchError(f"Missing input: {name}")
            if p.is_relative_to(self.output):
                raise WorkbenchError("Inputs may not be inside output")
        self.storage = c["storage"]
        if set(self.storage) != {"reserve_bytes", "job_budget_bytes"}:
            raise WorkbenchError("storage requires reserve_bytes and job_budget_bytes")
        for key, value in self.storage.items():
            positive(value, key, zero=True)
        if self.storage["reserve_bytes"] == 0:
            raise WorkbenchError("Set a nonzero disk reserve for this machine")
        check = c.get("content_check")
        if check:
            if set(check) != {"kind", "roi", "channel", "min_value", "min_pixels"} or check["kind"] != "channel_presence":
                raise WorkbenchError("Only explicit calibrated channel_presence is supported")
            x0, y0, x1, y1 = check["roi"]
            w, h = c["image"]
            if not all(type(v) is int for v in check["roi"]) or not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
                raise WorkbenchError("Invalid content ROI")
            if type(check["channel"]) is not int or check["channel"] not in (0, 1, 2):
                raise WorkbenchError("content channel must be 0, 1 or 2")
            if type(check["min_value"]) is not int or not 0 <= check["min_value"] <= 255:
                raise WorkbenchError("Invalid content threshold")
            if type(check["min_pixels"]) is not int or not 1 <= check["min_pixels"] <= (x1-x0)*(y1-y0):
                raise WorkbenchError("Invalid pixel count")
        exe = c["command"][0].replace("{python}", sys.executable).replace("{project}", str(self.base))
        self.executable = shutil.which(exe)
        if not self.executable:
            raise WorkbenchError(f"Executable not found: {exe}")
        self.output.mkdir(parents=True, exist_ok=True)

    def freeze(self):
        # Full executable hash catches changed render builds without running untrusted --version.
        return {"config": self.config,
                "inputs": {name: sha256(path) for name, path in self.inputs},
                "executable_sha256": sha256(self.executable),
                "runner": {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
                "pillow": PIL.__version__, "python": sys.version}


def check_space(project, startup=False):
    free = shutil.disk_usage(project.output).free
    required = project.storage["reserve_bytes"]
    if startup:
        required += project.storage["job_budget_bytes"]
    if free < required:
        raise Protected(f"Storage: {free} free bytes < {required} required bytes")
    return {"free_bytes": free, "required_bytes": required}


def validate_image(path, project):
    """Check container integrity AND decoded pixels, optionally calibrated content."""
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise WorkbenchError("Expected PNG")
            image.verify()
        with Image.open(path) as image:
            image.load()
            if image.size != tuple(project.config["image"]):
                raise WorkbenchError(f"Unexpected image size {image.size}")
            check = project.config.get("content_check")
            count = None
            if check:
                channel = image.convert("RGB").crop(tuple(check["roi"])).getchannel(check["channel"])
                count = sum(channel.histogram()[check["min_value"]:])
                if count < check["min_pixels"]:
                    raise WorkbenchError(f"Calibrated content check failed: {count} pixels")
    except (OSError, ValueError, SyntaxError) as exc:
        raise WorkbenchError(f"Cannot decode {Path(path).name}: {exc}") from exc
    return {"sha256": sha256(path), "bytes": Path(path).stat().st_size,
            "dimensions": project.config["image"], "content_pixels": count}


def frame_path(generation, frame):
    return generation / "frames" / f"{frame:06d}.png"


def verified_frames(project, generation, receipts):
    valid = {}
    problems = {}
    for frame in range(project.start, project.end + 1):
        key = str(frame)
        path = frame_path(generation, frame)
        try:
            if key not in receipts:
                raise WorkbenchError("No committed receipt")
            record = validate_image(path, project)
            if record["sha256"] != receipts[key]["sha256"]:
                raise WorkbenchError("Output hash changed")
            valid[key] = record
        except (WorkbenchError, OSError) as exc:
            problems[key] = str(exc)
    return valid, problems


def terminate(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=False)
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
        child.wait(timeout=10)


def execute(args, cwd, temporary, log, project, heartbeat, stop_file=None):
    env = os.environ.copy()
    env.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
    env["PYTHONIOENCODING"] = "utf-8"
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    with log.open("ab") as stream:
        child = subprocess.Popen(args, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, **options)
        try:
            while True:
                heartbeat(child.pid)
                if stop_file and stop_file.exists():
                    raise Stopped("Stop file requested; remove it before resuming")
                check_space(project)
                if time.monotonic() - started > project.config.get("timeout_seconds", 600):
                    raise WorkbenchError(f"Process timed out; see {log.name}")
                code = child.poll()
                if code is not None:
                    if code:
                        raise WorkbenchError(f"Process exited {code}; see {log.name}")
                    return
                time.sleep(project.config.get("poll_seconds", 1))
        finally:
            terminate(child)


def run(project, wait_seconds=0, max_recoveries=3, max_new_frames=None, stop_file=None):
    positive(wait_seconds, "wait_seconds", zero=True)
    if type(max_recoveries) is not int or max_recoveries < 0:
        raise WorkbenchError("max_recoveries must be nonnegative")
    if max_new_frames is not None and (type(max_new_frames) is not int or max_new_frames < 1):
        raise WorkbenchError("max_new_frames must be positive")
    with exclusive(project.output / "workbench.lock"):
        frozen = project.freeze()
        generation_id = digest(frozen)
        generation = project.output / generation_id
        for folder in ("frames", "logs", "temp"):
            (generation / folder).mkdir(parents=True, exist_ok=True)
        write_json(generation / "freeze.json", frozen)
        receipts_file = generation / "receipts.json"
        receipts = read_json(receipts_file) if receipts_file.exists() else {}
        valid, problems = verified_frames(project, generation, receipts)
        receipts = valid
        state = {"generation": generation_id, "started_at": now(), "total_frames": project.end-project.start+1,
                 "technical": "pending", "visual": "not_reviewed", "adoption": "not_recorded",
                 "completed_frames": len(valid), "pid": os.getpid()}

        def save(phase, **extra):
            state.update(phase=phase, updated_at=now(), **extra)
            write_json(generation / "state.json", state)
            write_json(project.output / "state.json", state)

        new = 0
        recoveries = 0
        wait_remaining = wait_seconds

        def space_ready():
            nonlocal wait_remaining
            while True:
                if stop_file and stop_file.exists():
                    raise Stopped("Stop file requested")
                try:
                    check_space(project, startup=True)
                    return
                except Protected as exc:
                    save("waiting_for_space", error=str(exc), child_pid=None)
                    if wait_remaining <= 0:
                        raise
                    delay = min(2, wait_remaining)
                    time.sleep(delay)
                    wait_remaining -= delay

        try:
            save("validating", discarded_receipts=problems, child_pid=None)
            for frame in range(project.start, project.end+1):
                if str(frame) in valid:
                    continue
                if stop_file and stop_file.exists():
                    raise Stopped("Stop file requested")
                while True:
                    space_ready()
                    if project.freeze() != frozen:
                        raise WorkbenchError("Inputs changed during the run; restart into a new generation")
                    temporary = generation / "temp"
                    candidate = temporary / f"{frame:06d}.png"
                    # This is an uncommitted file from this generation, never a user's source.
                    candidate.unlink(missing_ok=True)
                    values = {"python": sys.executable, "project": str(project.base), "frame": str(frame),
                              "output": str(candidate), "temp": str(temporary)}
                    args = [arg.format_map(values) for arg in project.config["command"]]
                    args[0] = project.executable
                    try:
                        execute(args, project.base, temporary, generation / "logs" / f"{frame:06d}.log", project,
                                lambda pid: save("rendering", frame=frame, child_pid=pid), stop_file)
                        record = validate_image(candidate, project)
                        if project.freeze() != frozen:
                            raise WorkbenchError("Inputs changed while rendering; candidate not committed")
                        # Preserve any invalid prior output as evidence rather than silently deleting it.
                        final = frame_path(generation, frame)
                        if final.exists():
                            evidence = generation / "rejected"
                            evidence.mkdir(exist_ok=True)
                            os.replace(final, evidence / f"{frame:06d}-{time.time_ns()}.png")
                        os.replace(candidate, final)
                        receipts[str(frame)] = record
                        write_json(receipts_file, receipts)
                        new += 1
                        save("frame_verified", completed_frames=len(receipts), child_pid=None)
                        break
                    except Protected:
                        recoveries += 1
                        if recoveries > max_recoveries:
                            raise Protected("Storage recovery limit reached")
                        save("waiting_for_space", child_pid=None, recoveries=recoveries)
                if max_new_frames and new >= max_new_frames and len(receipts) < state["total_frames"]:
                    raise Stopped("Requested frame budget reached; resume with the same command")
            valid, problems = verified_frames(project, generation, receipts)
            if problems or project.freeze() != frozen:
                raise WorkbenchError(f"Final verification failed: {problems}")
            save("technical_pass", technical="pass", completed_frames=len(valid), child_pid=None, error=None)
            return state
        except (Exception, KeyboardInterrupt) as exc:
            phase = "protected" if isinstance(exc, Protected) else "paused" if isinstance(exc, (Stopped, KeyboardInterrupt)) else "failed"
            save(phase, technical="pending", error=str(exc) or "Interrupted", child_pid=None)
            raise


def verify(project):
    with exclusive(project.output / "workbench.lock"):
        frozen = project.freeze()
        generation_id = digest(frozen)
        generation = project.output / generation_id
        receipt_path = generation / "receipts.json"
        receipts = read_json(receipt_path) if receipt_path.exists() else {}
        valid, problems = verified_frames(project, generation, receipts)
        if project.freeze() != frozen:
            problems["inputs"] = "Inputs changed during verification; rerun against the current generation"
        report = {"generation": generation_id, "checked_at": now(), "technical": "fail" if problems else "pass",
                  "verified_frames": len(valid), "problems": problems,
                  "visual": "not_reviewed", "adoption": "not_recorded"}
        write_json(generation / "verification.json", report)
        return report
