"""Standalone review command."""

import argparse
import json
import signal
from dataclasses import asdict
from pathlib import Path

from .review_runner import RUBRICS, ReviewRequest, ReviewRunner


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an independent read-only Codex review")
    parser.add_argument("kind", choices=RUBRICS)
    parser.add_argument("inputs", nargs="+", type=Path, help="Files relative to workspace")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--instructions-file", required=True, type=Path)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--output", type=Path, help="Markdown output path relative to workspace")
    args = parser.parse_args()
    previous_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        runner = ReviewRunner(args.workspace, model=args.model,
                              reasoning_effort=args.reasoning_effort, timeout=args.timeout)
        result = runner.run(ReviewRequest(args.kind,
            args.instructions_file.read_text(encoding="utf-8"), tuple(args.inputs), args.output))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"paper-review: {exc}\n")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    print(json.dumps(asdict(result), default=str, ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
