"""Coverage measurements must not mistake overlapping segments for outages."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "recording_gap_report",
    Path(__file__).resolve().parents[1] / "examples" / "recording_gap_report.py",
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class CoverageTests(unittest.TestCase):
    def test_nested_duplicate_and_clipped_segments(self):
        rows = [
            {"start_time": start, "end_time": end}
            for start, end in [(0, 30), (10, 20), (0, 30), (40, 60), (70, 80)]
        ]
        result = report.summarize(rows, 5, 50, 1)
        self.assertEqual(result["covered_seconds"], 35)
        self.assertEqual(result["reported_gap_count"], 1)
        self.assertEqual(result["reported_gap_seconds"], 10)
        self.assertEqual(result["trailing_unrecorded_seconds"], 0)

    def test_missing_window_edges_are_not_internal_gaps(self):
        result = report.summarize([{"start_time": 10, "end_time": 20}], 0, 30, 1)
        self.assertEqual(result["leading_unrecorded_seconds"], 10)
        self.assertEqual(result["trailing_unrecorded_seconds"], 10)
        self.assertEqual(result["reported_gap_count"], 0)
        self.assertEqual(result["coverage_within_recorded_span_percent"], 100)

    def test_empty_window_does_not_report_perfect_coverage(self):
        result = report.summarize([], 0, 30, 1)
        self.assertIsNone(result["coverage_within_recorded_span_percent"])
        self.assertEqual(result["coverage_of_requested_window_percent"], 0)

    def test_threshold_does_not_hide_short_holes_from_coverage(self):
        rows = [{"start_time": 0, "end_time": 10}, {"start_time": 11, "end_time": 20}]
        result = report.summarize(rows, 0, 20, 5)
        self.assertEqual(result["reported_gap_count"], 0)
        self.assertEqual(result["coverage_within_recorded_span_percent"], 95)
