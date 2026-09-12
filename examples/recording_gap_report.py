"""Read-only Frigate recording coverage report using its container-local API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import subprocess
import time
from urllib.parse import quote, urlencode


def utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")


def summarize(rows: list[dict], after: float, before: float, minimum_gap: float) -> dict:
    """Merge overlaps before measuring holes; report window edges separately."""

    intervals = []
    for row in rows:
        start, end = float(row["start_time"]), float(row["end_time"])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Invalid recording interval")
        start, end = max(after, start), min(before, end)
        if end > start:
            intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    gaps = [(left[1], right[0]) for left, right in zip(merged, merged[1:])]
    covered = sum(end - start for start, end in merged)
    span = merged[-1][1] - merged[0][0] if merged else 0.0
    significant = [(start, end) for start, end in gaps if end - start >= minimum_gap]
    return {
        "window_start_utc": utc(after),
        "window_end_utc": utc(before),
        "segments": len(intervals),
        "covered_seconds": round(covered, 3),
        "coverage_within_recorded_span_percent": round(covered / span * 100, 3) if span else None,
        "coverage_of_requested_window_percent": round(covered / (before - after) * 100, 3),
        "leading_unrecorded_seconds": round(merged[0][0] - after, 3) if merged else None,
        "trailing_unrecorded_seconds": round(before - merged[-1][1], 3) if merged else None,
        "minimum_reported_gap_seconds": minimum_gap,
        "reported_gap_count": len(significant),
        "reported_gap_seconds": round(sum(end - start for start, end in significant), 3),
        "max_internal_gap_seconds": round(max((end - start for start, end in gaps), default=0), 3),
        "gaps": [
            {"start_utc": utc(start), "end_utc": utc(end), "seconds": round(end - start, 3)}
            for start, end in significant
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="frigate")
    parser.add_argument("--camera", required=True)
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--minimum-gap", type=float, default=1.0)
    args = parser.parse_args()
    if not math.isfinite(args.hours) or args.hours <= 0:
        parser.error("--hours must be finite and positive")
    if not math.isfinite(args.minimum_gap) or args.minimum_gap < 0:
        parser.error("--minimum-gap must be finite and nonnegative")
    before = time.time()
    after = before - args.hours * 3600
    url = (
        f"http://127.0.0.1:5000/api/{quote(args.camera, safe='')}/recordings?"
        + urlencode({"after": after, "before": before})
    )
    try:
        result = subprocess.run(
            ["docker", "exec", args.container, "python3", "-c",
             "import sys,urllib.request; "
             "print(urllib.request.urlopen(sys.argv[1],timeout=20).read().decode())", url],
            capture_output=True, text=True, check=True, timeout=30,
        )
        report = summarize(json.loads(result.stdout), after, before, args.minimum_gap)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Recording report failed: {exc}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
