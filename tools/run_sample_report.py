"""Run local comparison reports for a directory of sample PDFs."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pdf_cleaner


@dataclass(frozen=True)
class SampleReportRow:
    """Flattened per-file report fields used for console and CSV output."""

    input_path: Path
    classification: str
    mode_used: str
    input_size: int
    output_size: int
    size_ratio: str
    duration_seconds: float
    success: bool
    reason: str


def _format_size_ratio(input_size: int, output_size: int) -> str:
    """Return output/input ratio as a fixed decimal string."""
    if input_size <= 0:
        return "n/a"
    return f"{output_size / input_size:.3f}"


def _build_rows(summary: pdf_cleaner.BatchSummary) -> list[SampleReportRow]:
    """Build report rows from batch diagnostics."""
    rows: list[SampleReportRow] = []
    for diagnostics in summary.results:
        if diagnostics.success:
            reason = ""
        else:
            reason = diagnostics.failure_reason or diagnostics.message

        rows.append(
            SampleReportRow(
                input_path=diagnostics.input_path,
                classification=diagnostics.pdf_kind.value,
                mode_used=diagnostics.mode_used,
                input_size=diagnostics.input_size,
                output_size=diagnostics.output_size,
                size_ratio=_format_size_ratio(
                    input_size=diagnostics.input_size,
                    output_size=diagnostics.output_size,
                ),
                duration_seconds=diagnostics.elapsed_seconds,
                success=diagnostics.success and not diagnostics.skipped,
                reason=reason,
            )
        )
    return rows


def _print_rows(rows: Sequence[SampleReportRow]) -> None:
    """Print a stable, human-readable table for local comparison runs."""
    print(
        "input,classification,mode_used,input_size,output_size,size_ratio,"
        "duration_seconds,success,reason"
    )
    for row in rows:
        print(
            f"{row.input_path},{row.classification},{row.mode_used},"
            f"{row.input_size},{row.output_size},{row.size_ratio},"
            f"{row.duration_seconds:.3f},{str(row.success).lower()},{row.reason}"
        )


def _write_csv(rows: Sequence[SampleReportRow], csv_path: Path) -> None:
    """Write report rows to CSV for spreadsheet-friendly review."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "input",
                "classification",
                "mode_used",
                "input_size",
                "output_size",
                "size_ratio",
                "duration_seconds",
                "success",
                "reason",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.input_path,
                    row.classification,
                    row.mode_used,
                    row.input_size,
                    row.output_size,
                    row.size_ratio,
                    f"{row.duration_seconds:.3f}",
                    str(row.success).lower(),
                    row.reason,
                ]
            )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse local reporting arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Run PDFCleaner over a directory and print a comparison report per file."
        )
    )
    parser.add_argument(
        "sample_dir",
        help="Directory containing sample PDFs (recursive).",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in pdf_cleaner.RequestedMode],
        default=pdf_cleaner.RequestedMode.AUTO.value,
        help="Pipeline mode to run for report generation.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Optional worker-process cap for parallel runs.",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable process-based parallel execution.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory override.",
    )
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Reuse <stem>_cleaned.pdf instead of suffix allocation.",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Optional CSV output path.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """Entrypoint for local sample comparison reporting."""
    args = _parse_args(argv)
    sample_dir = Path(args.sample_dir)
    if not sample_dir.is_dir():
        print(f"sample_dir does not exist or is not a directory: {sample_dir}")
        return 1

    summary = pdf_cleaner.clean_batch(
        input_paths=[sample_dir],
        requested_mode=pdf_cleaner.RequestedMode(args.mode),
        batch_settings=pdf_cleaner.BatchSettings(
            enable_parallel=not args.no_parallel,
            max_workers=args.workers,
            parallel_threshold=1,
        ),
        runtime_settings=pdf_cleaner.RuntimeSettings(
            overwrite_existing_output=bool(args.overwrite_output)
        ),
        output_dir=args.output_dir,
    )

    rows = _build_rows(summary)
    _print_rows(rows)
    if args.csv:
        _write_csv(rows=rows, csv_path=Path(args.csv))
        print(f"\nCSV report written to: {Path(args.csv).resolve()}")

    print(
        "\nsummary:"
        f" total={summary.total_files}"
        f" succeeded={summary.succeeded}"
        f" failed={summary.failed}"
        f" skipped={summary.skipped}"
        f" workers={summary.worker_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
