import argparse
import json
from pathlib import Path
import sys

from .core import Project, Protected, Stopped, WorkbenchError, read_json, run, verify
from .media import compare, encode


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evidence-driven CG production tools")
    commands = parser.add_subparsers(dest="action", required=True)
    render = commands.add_parser("run", help="Render missing/invalid frames and verify")
    render.add_argument("config")
    render.add_argument("--wait-seconds", type=float, default=0, help="Total bounded storage wait budget")
    render.add_argument("--max-recoveries", type=int, default=3)
    render.add_argument("--max-new-frames", type=int)
    render.add_argument("--stop-file", type=Path)
    commands.add_parser("verify", help="Independently recheck current input generation").add_argument("config")
    status = commands.add_parser("status", help="Read compact saved state; does not certify a live process")
    status.add_argument("output", type=Path)
    video = commands.add_parser("encode", help="Optional silent H.264 preview, full decode verification")
    video.add_argument("config")
    video.add_argument("--ffmpeg", default="ffmpeg")
    video.add_argument("--ffprobe", default="ffprobe")
    comparison = commands.add_parser("compare", help="Same-size side-by-side with source hashes")
    comparison.add_argument("left")
    comparison.add_argument("right")
    comparison.add_argument("output")
    comparison.add_argument("--crop", nargs=4, type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            result = run(Project(args.config), args.wait_seconds, args.max_recoveries, args.max_new_frames, args.stop_file)
        elif args.action == "verify":
            result = verify(Project(args.config))
        elif args.action == "encode":
            result = encode(Project(args.config), args.ffmpeg, args.ffprobe)
        elif args.action == "compare":
            result = compare(args.left, args.right, args.output, tuple(args.crop) if args.crop else None)
        else:
            result = read_json(args.output / "state.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("technical") == "fail" else 0
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2 if isinstance(exc, Protected) else 3 if isinstance(exc, (Stopped, KeyboardInterrupt)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
