"""Optional FFmpeg delivery and unchanged-scale image comparisons."""
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from PIL import Image, ImageDraw

from .core import (WorkbenchError, digest, exclusive, execute, frame_path, now,
                   read_json, sha256, verified_frames, write_json, check_space)


def capture(args, timeout=120):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=True, **options)
    return result.stdout.decode("utf-8", errors="replace")


def check_video(path, ffmpeg, ffprobe, width, height, frames, fps, audio=False):
    raw = json.loads(capture([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(path)]))
    streams = raw["streams"]
    videos = [s for s in streams if s["codec_type"] == "video"]
    if len(videos) != 1:
        raise WorkbenchError("Expected one video stream")
    video = videos[0]
    if (video["width"], video["height"], int(video.get("nb_read_frames", -1))) != (width, height, frames):
        raise WorkbenchError("Video dimensions or decoded frame count differ from contract")
    if Fraction(video["avg_frame_rate"]) != Fraction(str(fps)):
        raise WorkbenchError("Video frame rate differs from contract")
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    if has_audio != audio:
        raise WorkbenchError("Audio stream presence differs from contract")
    capture([ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-map", "0", "-f", "null", "-"], timeout=600)
    return {"sha256": sha256(path), "frames": frames, "dimensions": [width, height],
            "fps": str(fps), "audio": "decoded" if audio else "intentionally_absent"}


def encode(project, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    ffmpeg, ffprobe = shutil.which(ffmpeg), shutil.which(ffprobe)
    if not ffmpeg or not ffprobe:
        raise WorkbenchError("Install FFmpeg/ffprobe or pass executable paths")
    with exclusive(project.output / "workbench.lock"):
        frozen = project.freeze()
        generation_id = digest(frozen)
        generation = project.output / generation_id
        receipts = read_json(generation / "receipts.json")
        valid, problems = verified_frames(project, generation, receipts)
        if problems:
            raise WorkbenchError(f"Cannot encode unverified sequence: {problems}")
        width, height = project.config["image"]
        if width % 2 or height % 2:
            raise WorkbenchError("This H.264 yuv420p adapter requires even dimensions")
        check_space(project, startup=True)
        fingerprint = digest({"frames": valid, "ffmpeg": sha256(ffmpeg), "preset": "libx264-crf18-yuv420p-v1"})
        output = generation / f"preview-{fingerprint}.mp4"
        report_file = output.with_suffix(".json")
        expected = (width, height, len(valid), project.config["fps"])
        if output.exists() and report_file.exists() and read_json(report_file).get("sha256") == sha256(output):
            return {"output": str(output), "reused": True, **check_video(output, ffmpeg, ffprobe, *expected)}
        candidate = generation / "temp" / f"preview-{time.time_ns()}.mp4"
        args = [ffmpeg, "-nostdin", "-y", "-v", "error", "-framerate", str(project.config["fps"]),
                "-start_number", str(project.start), "-i", str(generation / "frames" / "%06d.png"),
                "-frames:v", str(len(valid)), "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(candidate)]
        def heartbeat(pid):
            write_json(generation / "encode_state.json", {"phase": "encoding", "child_pid": pid, "updated_at": now()})
        try:
            execute(args, project.base, generation / "temp", generation / "logs" / "encode.log", project, heartbeat)
            result = check_video(candidate, ffmpeg, ffprobe, *expected)
            current, errors = verified_frames(project, generation, receipts)
            if errors or current != valid or project.freeze() != frozen:
                raise WorkbenchError("Sequence or inputs changed while encoding")
            os.replace(candidate, output)
            report = {"output": str(output), "generation": generation_id, "technical": "pass",
                      "visual": "not_reviewed", "adoption": "not_recorded", **result}
            write_json(report_file, report)
            write_json(generation / "encode_state.json", {"phase": "technical_pass", "updated_at": now(), "child_pid": None})
            return report
        except BaseException as exc:
            write_json(generation / "encode_state.json", {"phase": "failed", "error": str(exc), "updated_at": now(), "child_pid": None})
            raise


def compare(left, right, output, crop=None):
    paths = [Path(left), Path(right)]
    output = Path(output)
    if output.exists() or output.resolve() in [p.resolve() for p in paths]:
        raise WorkbenchError("Comparison output must be a new file")
    images = []
    for path in paths:
        with Image.open(path) as im:
            im.load()
            images.append(im.convert("RGB"))
    if images[0].size != images[1].size:
        raise WorkbenchError("Compare matching native dimensions; no silent rescaling")
    original_size = images[0].size
    if crop:
        x0, y0, x1, y1 = crop
        if not (0 <= x0 < x1 <= original_size[0] and 0 <= y0 < y1 <= original_size[1]):
            raise WorkbenchError("Crop outside source dimensions")
        images = [im.crop(crop) for im in images]
    width, height = images[0].size
    canvas = Image.new("RGB", (2*width, height+28), "#20242b")
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 6), "A / left", fill="white")
    draw.text((width+8, 6), "B / right", fill="white")
    canvas.paste(images[0], (0, 28))
    canvas.paste(images[1], (width, 28))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    write_json(output.with_suffix(output.suffix + ".json"),
               {"sources": [{"path": str(p.resolve()), "sha256": sha256(p)} for p in paths],
                "native_dimensions": original_size, "crop": crop, "rescaled": False,
                "note": "Operator must verify matching camera, frame, lighting and display transform."})
    return {"output": str(output.resolve())}
