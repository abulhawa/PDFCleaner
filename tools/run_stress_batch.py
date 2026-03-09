"""Generate disposable PDF copies and run batch stress processing."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Sequence

DEFAULT_STRESS_FILE_COUNT: int = 1000
DEFAULT_STRESS_INPUT_DIR: str = "stress_test_input"
REQUESTED_MODE_CHOICES: tuple[str, ...] = ("auto", "structural", "ghostscript")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _is_pdf_file(path: Path) -> bool:
    """Return True when the path points to an existing PDF file."""
    return path.is_file() and path.suffix.lower() == ".pdf"


def _build_duplicate_filename(source_stem: str, index: int, padding_width: int) -> str:
    """Build deterministic duplicate names using a fixed numeric suffix width."""
    return f"{source_stem}_{index:0{padding_width}d}.pdf"


def _prepare_stress_input_dir(input_dir: Path, reset_existing: bool) -> Path:
    """Create an isolated stress input directory and optionally reset prior runs."""
    resolved_input_dir = input_dir.resolve()
    if resolved_input_dir.exists():
        if reset_existing:
            shutil.rmtree(resolved_input_dir)
        elif any(resolved_input_dir.iterdir()):
            raise FileExistsError(
                "stress input directory is not empty; "
                "delete it manually or rerun with --reset: "
                f"{resolved_input_dir}"
            )

    resolved_input_dir.mkdir(parents=True, exist_ok=True)
    return resolved_input_dir


def generate_stress_input_copies(
    source_pdf: Path,
    stress_input_dir: Path,
    count: int,
) -> list[Path]:
    """Duplicate one source PDF into deterministic, uniquely named stress inputs."""
    if count <= 0:
        raise ValueError("count must be greater than zero")

    copied_files: list[Path] = []
    padding_width = max(4, len(str(count)))
    for index in range(1, count + 1):
        target_path = stress_input_dir / _build_duplicate_filename(
            source_stem=source_pdf.stem,
            index=index,
            padding_width=padding_width,
        )
        shutil.copyfile(source_pdf, target_path)
        copied_files.append(target_path)

    return copied_files


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse stress-test helper arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Create disposable duplicate PDFs and run the normal batch pipeline "
            "for local volume stress testing."
        )
    )
    parser.add_argument(
        "source_pdf",
        help="Single real sample PDF used for duplication.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_STRESS_FILE_COUNT,
        help=f"Number of duplicates to generate (default: {DEFAULT_STRESS_FILE_COUNT}).",
    )
    parser.add_argument(
        "--input-dir",
        default=DEFAULT_STRESS_INPUT_DIR,
        help=(
            "Disposable stress input folder. Outputs are written to its sibling "
            "fixed_pdf path via normal dropped-folder routing."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing stress input folder before generating new copies.",
    )
    parser.add_argument(
        "--mode",
        choices=REQUESTED_MODE_CHOICES,
        default="auto",
        help="Processing mode for the batch run.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Optional worker-process cap for the batch run.",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable process-based parallel execution.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """Entrypoint for local disposable stress-test runs."""
    args = _parse_args(argv)
    source_pdf = Path(args.source_pdf).resolve()

    if args.count <= 0:
        print("count must be greater than zero")
        return 1

    if not _is_pdf_file(source_pdf):
        print(f"source_pdf is not an existing PDF file: {source_pdf}")
        return 1

    try:
        stress_input_dir = _prepare_stress_input_dir(
            input_dir=Path(args.input_dir),
            reset_existing=bool(args.reset),
        )
    except FileExistsError as exc:
        print(str(exc))
        return 1

    generated_inputs = generate_stress_input_copies(
        source_pdf=source_pdf,
        stress_input_dir=stress_input_dir,
        count=args.count,
    )

    # Use the same batch entrypoint used by drag-and-drop/CLI orchestration.
    import pdf_cleaner

    start_time = time.perf_counter()
    summary = pdf_cleaner.clean_batch(
        input_paths=[stress_input_dir],
        requested_mode=pdf_cleaner.RequestedMode(args.mode),
        batch_settings=pdf_cleaner.BatchSettings(
            enable_parallel=not args.no_parallel,
            max_workers=args.workers,
        ),
    )
    total_duration_seconds = time.perf_counter() - start_time

    total_processed = summary.succeeded + summary.failed
    average_time_per_file = (
        total_duration_seconds / total_processed if total_processed > 0 else 0.0
    )
    output_dir = stress_input_dir / pdf_cleaner.DEFAULT_OUTPUT_FOLDER_NAME

    print("[STRESS_SUMMARY]")
    print(f"total_generated={len(generated_inputs)}")
    print(f"total_processed={total_processed}")
    print(f"succeeded={summary.succeeded}")
    print(f"failed={summary.failed}")
    print(f"total_duration_seconds={total_duration_seconds:.3f}")
    print(f"average_time_per_file_seconds={average_time_per_file:.6f}")
    print(f"output_directory={output_dir}")

    if summary.skipped > 0:
        print(f"skipped={summary.skipped}")

    return 0 if summary.failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
