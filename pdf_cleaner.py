"""PDF Cleaner core pipeline and drag-and-drop CLI."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

import pikepdf
from pikepdf import Dictionary, Pdf

MIN_VALID_PDF_BYTES: int = 100
DEFAULT_MAX_SIZE_MULTIPLIER: float = 4.0
DEFAULT_OUTPUT_FOLDER_NAME: str = "fixed_pdf"
DEFAULT_MAX_WORKERS_CAP: int = 4
DEFAULT_PARALLEL_THRESHOLD: int = 8
WINDOWS_ARG_LIST_WARNING_COUNT: int = 200
WINDOWS_CMDLINE_WARNING_CHARS: int = 24000
DEFAULT_DETAILED_CONSOLE_RESULT_LIMIT: int = 200
PROGRESS_MIN_UPDATE_INTERVAL_SECONDS: float = 0.5
OVERWRITE_IGNORED_NOTICE: str = (
    "Safety mode is active: existing files are never overwritten. "
    "--overwrite-output is ignored."
)

TEXT_SHOWING_OPERATORS: set[str] = {"Tj", "TJ", "'", '"'}
VECTOR_DRAWING_OPERATORS: set[str] = {
    "m",
    "l",
    "c",
    "v",
    "y",
    "h",
    "re",
    "S",
    "s",
    "f",
    "F",
    "f*",
    "B",
    "B*",
    "b",
    "b*",
    "n",
    "W",
    "W*",
    "sh",
}

STRUCTURAL_MODE_LABEL: str = "structural"
GHOSTSCRIPT_MODE_LABEL: str = "ghostscript"
IMAGE_PASSTHROUGH_MODE_LABEL: str = "image_passthrough"
SKIPPED_MODE_LABEL: str = "skipped"
ERROR_MODE_LABEL: str = "error"


class RequestedMode(str, Enum):
    """Available processing modes for each file."""

    AUTO = "auto"
    STRUCTURAL = "structural"
    GHOSTSCRIPT = "ghostscript"


class PdfKind(str, Enum):
    """Logical type for conservative pipeline branching."""

    TEXT_VECTOR = "text_or_vector"
    IMAGE_ONLY = "image_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PdfInspection:
    """Inspection characteristics used for routing and validation."""

    page_count: int
    has_text: bool
    has_fonts: bool
    has_images: bool
    has_vector_graphics: bool
    pdf_kind: PdfKind


@dataclass(frozen=True)
class ValidationResult:
    """Validation result for a structural rewrite candidate."""

    valid: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeSettings:
    """Per-file runtime settings for deterministic output and validation.

    overwrite_existing_output is retained for API compatibility but ignored for safety.
    """

    overwrite_existing_output: bool = False
    max_size_multiplier: float = DEFAULT_MAX_SIZE_MULTIPLIER


@dataclass(frozen=True)
class BatchSettings:
    """Batch execution settings for throughput and process safety."""

    enable_parallel: bool = True
    max_workers: Optional[int] = None
    parallel_threshold: int = DEFAULT_PARALLEL_THRESHOLD


@dataclass(frozen=True)
class RepairDiagnostics:
    """Structured per-file processing result."""

    input_path: Path
    output_path: Optional[Path]
    success: bool
    skipped: bool
    mode_used: str
    pdf_kind: PdfKind
    text_preserved: bool
    fonts_present: bool
    input_size: int
    output_size: int
    elapsed_seconds: float
    message: str
    failure_reason: Optional[str]


@dataclass(frozen=True)
class BatchSummary:
    """Aggregate batch result with throughput and outcome metrics."""

    total_files: int
    succeeded: int
    failed: int
    skipped: int
    text_pdfs_processed: int
    image_only_pdfs_processed: int
    total_processing_seconds: float
    average_processing_seconds: float
    worker_count: int
    results: tuple[RepairDiagnostics, ...]


@dataclass(frozen=True)
class ProcessingTask:
    """Single file processing task with its resolved output directory."""

    input_path: Path
    output_dir: Path


@dataclass(frozen=True)
class PlannedTask:
    """Processing task including a pre-allocated output path."""

    input_path: Path
    output_dir: Path
    output_path: Optional[Path]


@dataclass(frozen=True)
class WorkerRequest:
    """Pickle-safe worker payload for process-based batch execution."""

    input_path: str
    output_dir: str
    requested_mode: str
    runtime_settings: RuntimeSettings
    gs_exe: str
    output_path: str


ProgressCallback = Callable[[int, int], None]


def _is_pdf_path(path: Path) -> bool:
    """Return True when the path points to an existing PDF file."""
    return path.is_file() and path.suffix.lower() == ".pdf"


def _build_gs_path(argv0: str) -> str:
    """Resolve bundled Ghostscript first, then system Ghostscript."""
    script_dir = Path(os.path.abspath(argv0)).parent
    local_gs_path = script_dir / "bin" / "gswin64c.exe"
    return str(local_gs_path) if local_gs_path.exists() else "gswin64c"


def _empty_docinfo(pdf: Pdf) -> None:
    """Normalize PDF document info keys to avoid malformed metadata."""
    pdf.docinfo = pdf.make_indirect(
        Dictionary(
            {
                "/Title": "",
                "/Author": "",
                "/Subject": "",
                "/Creator": "",
                "/Producer": "",
                "/Keywords": "",
            }
        )
    )


def _classify_pdf(inspection: PdfInspection) -> PdfKind:
    """Classify a PDF conservatively to avoid accidental image-only routing."""
    if inspection.has_text or inspection.has_fonts or inspection.has_vector_graphics:
        return PdfKind.TEXT_VECTOR
    if inspection.has_images:
        return PdfKind.IMAGE_ONLY
    return PdfKind.UNKNOWN


def _empty_inspection() -> PdfInspection:
    """Build a default inspection object used when inspection fails."""
    return PdfInspection(
        page_count=0,
        has_text=False,
        has_fonts=False,
        has_images=False,
        has_vector_graphics=False,
        pdf_kind=PdfKind.UNKNOWN,
    )


def _page_has_fonts(page: pikepdf.Page) -> bool:
    """Detect whether a page exposes font resources."""
    try:
        resources = page.Resources
    except Exception:
        return False

    if resources is None:
        return False

    fonts = resources.get("/Font")
    if fonts is None:
        return False

    try:
        return len(list(fonts.keys())) > 0
    except Exception:
        # If font resources exist but are malformed, treat them as present.
        return True


def _page_image_xobject_names(page: pikepdf.Page) -> set[str]:
    """Return image XObject names referenced by page resources."""
    names: set[str] = set()

    try:
        resources = page.Resources
    except Exception:
        return names

    if resources is None:
        return names

    xobjects = resources.get("/XObject")
    if xobjects is None:
        return names

    try:
        iterator = xobjects.items()
    except Exception:
        return names

    for name, xobject in iterator:
        try:
            subtype = xobject.get("/Subtype")
        except Exception:
            continue
        if str(subtype) == "/Image":
            names.add(str(name))

    return names


def _extract_page_content_flags(page: pikepdf.Page) -> tuple[bool, bool, bool]:
    """Extract text, vector, and image drawing signals from page operators."""
    has_text = False
    has_vector = False
    draws_image = False
    image_xobject_names = _page_image_xobject_names(page)

    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return has_text, has_vector, bool(image_xobject_names)

    for instruction in instructions:
        operator_name = str(instruction.operator)

        if operator_name in TEXT_SHOWING_OPERATORS:
            has_text = True
            continue

        if operator_name in VECTOR_DRAWING_OPERATORS:
            has_vector = True
            continue

        if operator_name == "Do":
            operand_name = str(instruction.operands[0]) if instruction.operands else ""
            if operand_name in image_xobject_names:
                draws_image = True
            else:
                # Non-image XObjects (such as forms) are treated as vector-like.
                has_vector = True

        if has_text and has_vector and draws_image:
            break

    if image_xobject_names:
        draws_image = True

    return has_text, has_vector, draws_image


def inspect_pdf(path: Path) -> PdfInspection:
    """Inspect text/vector/image characteristics for conservative routing."""
    has_text = False
    has_fonts = False
    has_images = False
    has_vector_graphics = False

    with Pdf.open(path) as pdf:
        page_count = len(pdf.pages)

        for page in pdf.pages:
            if not has_fonts and _page_has_fonts(page):
                has_fonts = True

            page_has_text, page_has_vector, page_has_images = _extract_page_content_flags(
                page
            )
            has_text = has_text or page_has_text
            has_vector_graphics = has_vector_graphics or page_has_vector
            has_images = has_images or page_has_images

            if has_text and has_fonts and has_images and has_vector_graphics:
                break

    inspection = PdfInspection(
        page_count=page_count,
        has_text=has_text,
        has_fonts=has_fonts,
        has_images=has_images,
        has_vector_graphics=has_vector_graphics,
        pdf_kind=PdfKind.UNKNOWN,
    )
    return PdfInspection(
        page_count=inspection.page_count,
        has_text=inspection.has_text,
        has_fonts=inspection.has_fonts,
        has_images=inspection.has_images,
        has_vector_graphics=inspection.has_vector_graphics,
        pdf_kind=_classify_pdf(inspection),
    )


def structural_normalize_pdf(input_path: Path, output_path: Path) -> None:
    """Perform a qpdf-backed structural rewrite using pikepdf only."""
    with Pdf.open(input_path, attempt_recovery=True) as pdf:
        _empty_docinfo(pdf)
        pdf.remove_unreferenced_resources()
        pdf.save(
            output_path,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
        )


def validate_structural_output(
    input_inspection: PdfInspection,
    output_inspection: PdfInspection,
    input_size: int,
    output_size: int,
    max_size_multiplier: float,
) -> ValidationResult:
    """Validate that structural normalization preserved critical characteristics."""
    reasons: list[str] = []

    if (
        input_inspection.page_count > 0
        and output_inspection.page_count != input_inspection.page_count
    ):
        reasons.append("page_count_changed")

    if input_inspection.has_text and not output_inspection.has_text:
        reasons.append("text_layer_missing_after_rewrite")

    if input_inspection.has_fonts and not output_inspection.has_fonts:
        reasons.append("font_resources_missing_after_rewrite")

    if (
        input_inspection.pdf_kind == PdfKind.TEXT_VECTOR
        and output_inspection.pdf_kind == PdfKind.IMAGE_ONLY
    ):
        reasons.append("text_vector_content_became_image_only")

    if input_size > 0 and output_size > int(input_size * max_size_multiplier):
        reasons.append("output_size_growth_exceeds_limit")

    return ValidationResult(valid=not reasons, reasons=tuple(reasons))


def run_ghostscript_compatibility(
    input_path: Path, output_path: Path, gs_exe: str
) -> tuple[bool, str]:
    """Run Ghostscript PDF/A conversion in explicit compatibility mode."""
    gs_command = [
        gs_exe,
        "-dPDFA=1",
        "-dBATCH",
        "-dNOPAUSE",
        "-dNOOUTERSAVE",
        "-sProcessColorModel=DeviceRGB",
        "-sDEVICE=pdfwrite",
        "-sPDFACompatibilityPolicy=1",
        f"-sOutputFile={output_path}",
        str(input_path),
    ]

    result = subprocess.run(
        gs_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return False, stderr if stderr else "Ghostscript exited with a non-zero return code."

    if not output_path.exists() or output_path.stat().st_size < MIN_VALID_PDF_BYTES:
        return False, "Ghostscript output was missing or too small to be a valid PDF."

    return True, ""


def _format_size_change(input_size: int, output_size: int) -> str:
    """Format output size delta as a percentage string."""
    if input_size <= 0:
        return "n/a"
    delta_percent = ((output_size - input_size) / input_size) * 100.0
    sign = "+" if delta_percent >= 0 else ""
    return f"{sign}{delta_percent:.1f}%"


def _safe_inspection(path: Path, fallback: PdfInspection) -> PdfInspection:
    """Inspect a PDF and return fallback characteristics if inspection fails."""
    try:
        return inspect_pdf(path)
    except Exception:
        return fallback


def _pause_for_windows_dragdrop(force_windows_pause: bool = False) -> None:
    """Pause before exit so users can read summary output in drag-and-drop runs."""
    if force_windows_pause and os.name == "nt":
        try:
            import msvcrt

            print("Press any key to exit...")
            msvcrt.getwch()
            return
        except Exception:
            try:
                os.system("pause > nul")
                return
            except Exception:
                # Fall back to standard input prompt if Windows pause fails.
                pass

    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input("Press Enter to exit...")
    except EOFError:
        return


def _should_force_windows_exit_pause() -> bool:
    """Return True for packaged Windows runs that should always pause after summary."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _estimate_windows_cmdline_length(args: Sequence[str]) -> int:
    """Estimate Windows command-line size, including basic quoting overhead."""
    executable_overhead = 32
    return executable_overhead + sum(len(arg) + 3 for arg in args)


def _build_windows_bulk_drop_warning(inputs: Sequence[str]) -> Optional[str]:
    """Return guidance when Windows file-drop arguments are likely too large."""
    if os.name != "nt" or not inputs:
        return None

    input_paths = [Path(item) for item in inputs]
    has_folder_input = any(path.is_dir() for path in input_paths)
    if has_folder_input:
        return None

    individual_file_count = sum(1 for path in input_paths if path.is_file())
    estimated_cmdline_chars = _estimate_windows_cmdline_length(list(inputs))

    if (
        individual_file_count >= WINDOWS_ARG_LIST_WARNING_COUNT
        or estimated_cmdline_chars >= WINDOWS_CMDLINE_WARNING_CHARS
    ):
        return (
            "Large file-drop detected. On Windows, dragging many individual files can "
            "exceed command-line limits before the app starts. For large batches, drag "
            "the containing folder onto this EXE instead."
        )

    return None


def _prepare_output_dir(output_dir: Path) -> Path:
    """Ensure the output directory exists and return an absolute path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir.resolve()


def _build_cleaned_filename(stem: str, attempt: int) -> str:
    """Build deterministic cleaned filename with optional numeric suffix."""
    if attempt == 0:
        return f"{stem}_cleaned.pdf"
    return f"{stem}_cleaned_{attempt}.pdf"


def resolve_output_path(
    input_file: Path,
    output_dir: Path,
    overwrite_existing_output: bool = False,
    reserved_paths: Optional[set[Path]] = None,
) -> Path:
    """Resolve deterministic output path with predictable collision handling.

    overwrite_existing_output is intentionally ignored to prevent file replacement.
    """
    if overwrite_existing_output:
        # Safety invariant: never overwrite an existing file.
        overwrite_existing_output = False

    reserved = reserved_paths or set()
    attempt = 0
    while True:
        candidate = output_dir / _build_cleaned_filename(input_file.stem, attempt)
        if candidate not in reserved and not candidate.exists():
            return candidate
        attempt += 1


def _build_diagnostics(
    input_file: Path,
    output_path: Optional[Path],
    success: bool,
    skipped: bool,
    mode_used: str,
    input_size: int,
    output_size: int,
    input_inspection: PdfInspection,
    final_inspection: PdfInspection,
    message: str,
    failure_reason: Optional[str],
    elapsed_seconds: float,
) -> RepairDiagnostics:
    """Build a stable diagnostics record for file and batch reporting."""
    text_preserved = (not input_inspection.has_text) or final_inspection.has_text
    return RepairDiagnostics(
        input_path=input_file,
        output_path=output_path,
        success=success,
        skipped=skipped,
        mode_used=mode_used,
        pdf_kind=input_inspection.pdf_kind,
        text_preserved=text_preserved,
        fonts_present=final_inspection.has_fonts,
        input_size=input_size,
        output_size=output_size,
        elapsed_seconds=elapsed_seconds,
        message=message,
        failure_reason=failure_reason,
    )


def clean_pdf(
    input_path: str | Path,
    output_dir: str | Path,
    requested_mode: RequestedMode = RequestedMode.AUTO,
    runtime_settings: Optional[RuntimeSettings] = None,
    gs_exe: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> RepairDiagnostics:
    """Clean one PDF and return diagnostics without overwriting source files."""
    started_at = time.perf_counter()
    runtime = runtime_settings or RuntimeSettings()
    input_file = Path(input_path)

    if not _is_pdf_path(input_file):
        elapsed = time.perf_counter() - started_at
        message = "Skipped non-PDF or missing input."
        return _build_diagnostics(
            input_file=input_file,
            output_path=None,
            success=False,
            skipped=True,
            mode_used=SKIPPED_MODE_LABEL,
            input_size=0,
            output_size=0,
            input_inspection=_empty_inspection(),
            final_inspection=_empty_inspection(),
            message=message,
            failure_reason=None,
            elapsed_seconds=elapsed,
        )

    input_size = input_file.stat().st_size
    input_inspection = _empty_inspection()
    target_output_path = output_path

    try:
        output_dir_resolved = _prepare_output_dir(Path(output_dir))
        if target_output_path is None:
            target_output_path = resolve_output_path(
                input_file=input_file,
                output_dir=output_dir_resolved,
                overwrite_existing_output=runtime.overwrite_existing_output,
            )
        else:
            target_output_path = Path(target_output_path)
            target_output_path.parent.mkdir(parents=True, exist_ok=True)
            if target_output_path.exists():
                target_output_path = resolve_output_path(
                    input_file=input_file,
                    output_dir=target_output_path.parent,
                    overwrite_existing_output=False,
                )

        try:
            input_inspection = inspect_pdf(input_file)
        except Exception as exc:
            input_inspection = _empty_inspection()
            inspection_error = f"input_inspection_error: {exc}"
        else:
            inspection_error = ""

        with tempfile.TemporaryDirectory(prefix="pdf_cleaner_") as temp_dir:
            temp_dir_path = Path(temp_dir)
            structural_temp = temp_dir_path / "structural_output.pdf"
            ghostscript_temp = temp_dir_path / "ghostscript_output.pdf"

            if requested_mode == RequestedMode.GHOSTSCRIPT:
                gs_binary = gs_exe or _build_gs_path(sys.argv[0])
                gs_success, gs_message = run_ghostscript_compatibility(
                    input_path=input_file,
                    output_path=ghostscript_temp,
                    gs_exe=gs_binary,
                )
                if not gs_success:
                    elapsed = time.perf_counter() - started_at
                    reason = gs_message
                    if inspection_error:
                        reason = f"{reason}; {inspection_error}"
                    return _build_diagnostics(
                        input_file=input_file,
                        output_path=target_output_path,
                        success=False,
                        skipped=False,
                        mode_used=GHOSTSCRIPT_MODE_LABEL,
                        input_size=input_size,
                        output_size=input_size,
                        input_inspection=input_inspection,
                        final_inspection=input_inspection,
                        message="Ghostscript compatibility conversion failed.",
                        failure_reason=reason,
                        elapsed_seconds=elapsed,
                    )

                os.replace(ghostscript_temp, target_output_path)
                final_inspection = _safe_inspection(target_output_path, input_inspection)
                output_size = target_output_path.stat().st_size
                elapsed = time.perf_counter() - started_at
                message = "Ghostscript compatibility conversion completed."
                if inspection_error:
                    message = f"{message} Note: {inspection_error}"
                return _build_diagnostics(
                    input_file=input_file,
                    output_path=target_output_path,
                    success=True,
                    skipped=False,
                    mode_used=GHOSTSCRIPT_MODE_LABEL,
                    input_size=input_size,
                    output_size=output_size,
                    input_inspection=input_inspection,
                    final_inspection=final_inspection,
                    message=message,
                    failure_reason=None,
                    elapsed_seconds=elapsed,
                )

            if (
                requested_mode == RequestedMode.AUTO
                and input_inspection.pdf_kind == PdfKind.IMAGE_ONLY
            ):
                shutil.copyfile(input_file, target_output_path)
                output_size = target_output_path.stat().st_size
                elapsed = time.perf_counter() - started_at
                message = "Image-only PDF copied without raster rewrite."
                if inspection_error:
                    message = f"{message} Note: {inspection_error}"
                return _build_diagnostics(
                    input_file=input_file,
                    output_path=target_output_path,
                    success=True,
                    skipped=False,
                    mode_used=IMAGE_PASSTHROUGH_MODE_LABEL,
                    input_size=input_size,
                    output_size=output_size,
                    input_inspection=input_inspection,
                    final_inspection=input_inspection,
                    message=message,
                    failure_reason=None,
                    elapsed_seconds=elapsed,
                )

            try:
                structural_normalize_pdf(input_file, structural_temp)
                structural_inspection = inspect_pdf(structural_temp)
                structural_size = structural_temp.stat().st_size
                structural_validation = validate_structural_output(
                    input_inspection=input_inspection,
                    output_inspection=structural_inspection,
                    input_size=input_size,
                    output_size=structural_size,
                    max_size_multiplier=runtime.max_size_multiplier,
                )
            except Exception as exc:
                structural_inspection = input_inspection
                structural_validation = ValidationResult(
                    valid=False,
                    reasons=(f"structural_error: {exc}",),
                )
                structural_size = input_size

            if not structural_validation.valid:
                elapsed = time.perf_counter() - started_at
                reason = "; ".join(structural_validation.reasons)
                if inspection_error:
                    reason = f"{reason}; {inspection_error}" if reason else inspection_error
                return _build_diagnostics(
                    input_file=input_file,
                    output_path=target_output_path,
                    success=False,
                    skipped=False,
                    mode_used=STRUCTURAL_MODE_LABEL,
                    input_size=input_size,
                    output_size=structural_size,
                    input_inspection=input_inspection,
                    final_inspection=structural_inspection,
                    message="Structural normalization failed validation.",
                    failure_reason=reason,
                    elapsed_seconds=elapsed,
                )

            os.replace(structural_temp, target_output_path)
            output_size = target_output_path.stat().st_size
            elapsed = time.perf_counter() - started_at
            message = "Structural normalization completed."
            if inspection_error:
                message = f"{message} Note: {inspection_error}"
            return _build_diagnostics(
                input_file=input_file,
                output_path=target_output_path,
                success=True,
                skipped=False,
                mode_used=STRUCTURAL_MODE_LABEL,
                input_size=input_size,
                output_size=output_size,
                input_inspection=input_inspection,
                final_inspection=structural_inspection,
                message=message,
                failure_reason=None,
                elapsed_seconds=elapsed,
            )

    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        return _build_diagnostics(
            input_file=input_file,
            output_path=target_output_path,
            success=False,
            skipped=False,
            mode_used=ERROR_MODE_LABEL,
            input_size=input_size,
            output_size=input_size,
            input_inspection=input_inspection,
            final_inspection=input_inspection,
            message="Unexpected processing error.",
            failure_reason=f"unexpected_error: {exc}",
            elapsed_seconds=elapsed,
        )


def _resolve_worker_count(pdf_file_count: int, batch_settings: BatchSettings) -> int:
    """Resolve bounded worker count for safe process-based parallelism."""
    if pdf_file_count <= 1 or not batch_settings.enable_parallel:
        return 1

    if pdf_file_count < max(1, batch_settings.parallel_threshold):
        return 1

    cpu_count = os.cpu_count() or 1
    configured_workers = batch_settings.max_workers or min(cpu_count, DEFAULT_MAX_WORKERS_CAP)
    bounded_workers = max(1, min(configured_workers, cpu_count, DEFAULT_MAX_WORKERS_CAP))
    return min(bounded_workers, pdf_file_count)


def _iter_pdfs_for_dropped_folder(folder: Path) -> list[Path]:
    """List PDFs under a dropped folder while excluding nested fixed output folders."""
    pdfs: list[Path] = []
    for root, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(
            dirname for dirname in dirnames if dirname != DEFAULT_OUTPUT_FOLDER_NAME
        )
        for filename in sorted(filenames):
            if filename.lower().endswith(".pdf"):
                pdfs.append(Path(root) / filename)
    return pdfs


def _collect_processing_tasks(
    input_paths: Sequence[str | Path],
    explicit_output_dir: Optional[Path] = None,
) -> list[ProcessingTask]:
    """Expand dropped files/folders into processing tasks with output routing policy."""
    tasks: list[ProcessingTask] = []
    output_override = explicit_output_dir.resolve() if explicit_output_dir else None

    for raw_input in input_paths:
        source_path = Path(raw_input)

        if source_path.is_dir():
            folder_pdfs = _iter_pdfs_for_dropped_folder(source_path)
            if not folder_pdfs:
                continue

            target_output_dir = output_override or (source_path / DEFAULT_OUTPUT_FOLDER_NAME)
            for pdf_file in folder_pdfs:
                tasks.append(ProcessingTask(input_path=pdf_file, output_dir=target_output_dir))
            continue

        target_output_dir = output_override or (source_path.parent / DEFAULT_OUTPUT_FOLDER_NAME)
        tasks.append(ProcessingTask(input_path=source_path, output_dir=target_output_dir))

    return tasks


def _build_worker_request(
    input_file: Path,
    output_dir: Path,
    output_path: Path,
    requested_mode: RequestedMode,
    runtime_settings: RuntimeSettings,
    gs_exe: str,
) -> WorkerRequest:
    """Build a process worker payload."""
    return WorkerRequest(
        input_path=str(input_file),
        output_dir=str(output_dir),
        requested_mode=requested_mode.value,
        runtime_settings=runtime_settings,
        gs_exe=gs_exe,
        output_path=str(output_path),
    )


def _clean_pdf_worker(worker_request: WorkerRequest) -> RepairDiagnostics:
    """Worker adapter for process-based batch execution."""
    return clean_pdf(
        input_path=worker_request.input_path,
        output_dir=worker_request.output_dir,
        requested_mode=RequestedMode(worker_request.requested_mode),
        runtime_settings=worker_request.runtime_settings,
        gs_exe=worker_request.gs_exe,
        output_path=Path(worker_request.output_path),
    )


def clean_batch(
    input_paths: Sequence[str | Path],
    requested_mode: RequestedMode = RequestedMode.AUTO,
    batch_settings: Optional[BatchSettings] = None,
    runtime_settings: Optional[RuntimeSettings] = None,
    gs_exe: Optional[str] = None,
    output_dir: Optional[str | Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> BatchSummary:
    """Clean many PDFs and return aggregate metrics plus per-file diagnostics."""
    runtime = runtime_settings or RuntimeSettings()
    batch = batch_settings or BatchSettings()
    resolved_gs = gs_exe or _build_gs_path(sys.argv[0])

    output_override = Path(output_dir) if output_dir else None
    processing_tasks = _collect_processing_tasks(
        input_paths=input_paths,
        explicit_output_dir=output_override,
    )

    if not processing_tasks:
        return BatchSummary(
            total_files=0,
            succeeded=0,
            failed=0,
            skipped=0,
            text_pdfs_processed=0,
            image_only_pdfs_processed=0,
            total_processing_seconds=0.0,
            average_processing_seconds=0.0,
            worker_count=1,
            results=tuple(),
        )

    reserved_by_output_dir: dict[Path, set[Path]] = {}
    planned_tasks: list[PlannedTask] = []

    for task in processing_tasks:
        prepared_output_dir = _prepare_output_dir(task.output_dir)
        if not _is_pdf_path(task.input_path):
            planned_tasks.append(
                PlannedTask(
                    input_path=task.input_path,
                    output_dir=prepared_output_dir,
                    output_path=None,
                )
            )
            continue

        reserved = reserved_by_output_dir.setdefault(prepared_output_dir, set())
        candidate_output = resolve_output_path(
            input_file=task.input_path,
            output_dir=prepared_output_dir,
            overwrite_existing_output=runtime.overwrite_existing_output,
            reserved_paths=reserved,
        )
        reserved.add(candidate_output)
        planned_tasks.append(
            PlannedTask(
                input_path=task.input_path,
                output_dir=prepared_output_dir,
                output_path=candidate_output,
            )
        )

    pdf_file_count = sum(1 for task in planned_tasks if task.output_path is not None)
    worker_count = _resolve_worker_count(pdf_file_count=pdf_file_count, batch_settings=batch)

    results: list[Optional[RepairDiagnostics]] = [None] * len(planned_tasks)
    total_planned_tasks = len(planned_tasks)
    completed_tasks = 0

    def _store_result(index: int, diagnostics: RepairDiagnostics) -> None:
        """Store one task result and emit optional progress updates."""
        nonlocal completed_tasks
        if results[index] is not None:
            return
        results[index] = diagnostics
        completed_tasks += 1
        if progress_callback:
            progress_callback(completed_tasks, total_planned_tasks)

    if worker_count == 1:
        for index, task in enumerate(planned_tasks):
            _store_result(
                index,
                clean_pdf(
                    input_path=task.input_path,
                    output_dir=task.output_dir,
                    requested_mode=requested_mode,
                    runtime_settings=runtime,
                    gs_exe=resolved_gs,
                    output_path=task.output_path,
                ),
            )
    else:
        pool_broken = False
        futures = {}
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            for index, task in enumerate(planned_tasks):
                if task.output_path is None:
                    _store_result(
                        index,
                        clean_pdf(
                            input_path=task.input_path,
                            output_dir=task.output_dir,
                            requested_mode=requested_mode,
                            runtime_settings=runtime,
                            gs_exe=resolved_gs,
                            output_path=None,
                        ),
                    )
                    continue

                request = _build_worker_request(
                    input_file=task.input_path,
                    output_dir=task.output_dir,
                    output_path=task.output_path,
                    requested_mode=requested_mode,
                    runtime_settings=runtime,
                    gs_exe=resolved_gs,
                )
                futures[executor.submit(_clean_pdf_worker, request)] = (index, task)

            for future in as_completed(futures):
                index, task = futures[future]
                try:
                    _store_result(index, future.result())
                except BrokenProcessPool:
                    # Recover by rerunning unresolved tasks sequentially.
                    pool_broken = True
                except Exception as exc:
                    # Worker crashes should not fail the full batch.
                    _store_result(
                        index,
                        _build_diagnostics(
                            input_file=task.input_path,
                            output_path=task.output_path,
                            success=False,
                            skipped=False,
                            mode_used=ERROR_MODE_LABEL,
                            input_size=0,
                            output_size=0,
                            input_inspection=_empty_inspection(),
                            final_inspection=_empty_inspection(),
                            message="Worker process failed.",
                            failure_reason=f"worker_error: {exc}",
                            elapsed_seconds=0.0,
                        ),
                    )

        if pool_broken:
            for index, task in enumerate(planned_tasks):
                if results[index] is not None:
                    continue
                _store_result(
                    index,
                    clean_pdf(
                        input_path=task.input_path,
                        output_dir=task.output_dir,
                        requested_mode=requested_mode,
                        runtime_settings=runtime,
                        gs_exe=resolved_gs,
                        output_path=task.output_path,
                    ),
                )

    finalized_results = tuple(result for result in results if result is not None)

    succeeded = sum(1 for result in finalized_results if result.success)
    skipped = sum(1 for result in finalized_results if result.skipped)
    failed = sum(1 for result in finalized_results if not result.success and not result.skipped)
    text_processed = sum(
        1
        for result in finalized_results
        if not result.skipped and result.pdf_kind == PdfKind.TEXT_VECTOR
    )
    image_processed = sum(
        1
        for result in finalized_results
        if not result.skipped and result.pdf_kind == PdfKind.IMAGE_ONLY
    )

    total_processing_seconds = sum(result.elapsed_seconds for result in finalized_results)
    processed_files = sum(1 for result in finalized_results if not result.skipped)
    average_processing_seconds = (
        total_processing_seconds / processed_files if processed_files > 0 else 0.0
    )

    return BatchSummary(
        total_files=len(processing_tasks),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        text_pdfs_processed=text_processed,
        image_only_pdfs_processed=image_processed,
        total_processing_seconds=total_processing_seconds,
        average_processing_seconds=average_processing_seconds,
        worker_count=worker_count,
        results=finalized_results,
    )


def print_diagnostics(diagnostics: RepairDiagnostics) -> None:
    """Emit stable per-file diagnostics to stdout."""
    text_preserved = "yes" if diagnostics.text_preserved else "no"
    fonts_present = "yes" if diagnostics.fonts_present else "no"
    size_change = _format_size_change(diagnostics.input_size, diagnostics.output_size)

    if diagnostics.skipped:
        print(f"[SKIP] {diagnostics.input_path.name} | reason={diagnostics.message}")
        return

    status = "OK" if diagnostics.success else "ERROR"
    output_label = str(diagnostics.output_path) if diagnostics.output_path else "n/a"
    print(
        "[RESULT] "
        f"{diagnostics.input_path.name} | "
        f"status={status} | "
        f"mode={diagnostics.mode_used} | "
        f"kind={diagnostics.pdf_kind.value} | "
        f"text_preserved={text_preserved} | "
        f"fonts_present={fonts_present} | "
        f"output={output_label} | "
        f"size_change={size_change} "
        f"({diagnostics.input_size} -> {diagnostics.output_size} bytes) | "
        f"elapsed={diagnostics.elapsed_seconds:.3f}s"
    )
    if diagnostics.message:
        print(f"[DETAIL] {diagnostics.message}")
    if diagnostics.failure_reason:
        print(f"[FAILURE] {diagnostics.failure_reason}")


def print_batch_summary(summary: BatchSummary, wall_clock_seconds: float) -> None:
    """Emit user-friendly aggregate batch metrics."""
    processed_files = max(0, summary.succeeded + summary.failed)
    average_wall_time_per_processed_file = (
        wall_clock_seconds / processed_files if processed_files > 0 else 0.0
    )

    print("\n[Summary]")
    print(f"Total files: {summary.total_files}")
    print(f"Succeeded: {summary.succeeded}")
    print(f"Failed: {summary.failed}")
    print(f"Skipped: {summary.skipped}")
    print(f"Text PDFs processed: {summary.text_pdfs_processed}")
    print(f"Image-only PDFs processed: {summary.image_only_pdfs_processed}")
    print(f"Worker count: {summary.worker_count}")
    print(f"Wall time: {wall_clock_seconds:.3f} seconds")
    print(
        "Average wall time per processed file: "
        f"{average_wall_time_per_processed_file:.3f} seconds"
    )


def _select_console_results(
    summary: BatchSummary,
    show_all_results: bool,
) -> tuple[RepairDiagnostics, ...]:
    """Select per-file diagnostics to print for console performance and clarity."""
    if show_all_results:
        return summary.results

    if summary.total_files <= DEFAULT_DETAILED_CONSOLE_RESULT_LIMIT:
        return summary.results

    return tuple(
        result for result in summary.results if result.skipped or not result.success
    )


def _build_progress_reporter() -> ProgressCallback:
    """Build a throttled progress reporter for interactive CLI/EXE runs."""
    started_at = time.perf_counter()
    last_emitted_at = 0.0
    has_tty_stdout = bool(sys.stdout) and sys.stdout.isatty()

    def report(completed: int, total: int) -> None:
        nonlocal last_emitted_at
        if total <= 0:
            return

        now = time.perf_counter()
        is_final = completed >= total
        progress_step = max(1, total // 100)
        should_emit = (
            is_final
            or (completed % progress_step == 0)
            or ((now - last_emitted_at) >= PROGRESS_MIN_UPDATE_INTERVAL_SECONDS)
        )
        if not should_emit:
            return

        elapsed_seconds = max(0.0, now - started_at)
        percent = (completed / total) * 100.0
        rate = (completed / elapsed_seconds) if elapsed_seconds > 0 else 0.0
        eta_seconds = ((total - completed) / rate) if rate > 0 else 0.0
        line = (
            f"[PROGRESS] {completed}/{total} ({percent:.1f}%) "
            f"elapsed={elapsed_seconds:.1f}s eta~{eta_seconds:.1f}s"
        )

        if has_tty_stdout:
            if is_final:
                print(f"\r{line}")
            else:
                print(f"\r{line}", end="", flush=True)
        elif is_final:
            print(line)

        last_emitted_at = now

    return report


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI args while keeping drag-and-drop usage simple."""
    parser = argparse.ArgumentParser(
        description=(
            "Clean PDFs with structural normalization and image-only-safe handling."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RequestedMode],
        default=RequestedMode.AUTO.value,
        help=(
            "Processing mode: 'auto' (default) uses structural for text/vector and "
            "passthrough for image-only, 'structural' forces structural-only, "
            "'ghostscript' forces compatibility conversion."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Optional output folder override. If omitted, each source folder gets "
            "a sibling fixed_pdf folder automatically."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Max parallel worker processes for large batches. "
            "Uses a safe bounded default when omitted."
        ),
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel processing and run sequentially.",
    )
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help=(
            "Deprecated safety flag. Existing files are never overwritten; "
            "this option is ignored."
        ),
    )
    parser.add_argument(
        "--show-all-results",
        action="store_true",
        help=(
            "Print per-file success lines even for large batches. "
            "By default, large runs print only failures/skips plus summary."
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input PDF file or folder paths.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint for script and packaged executable."""
    args = _parse_args(argv)
    if not args.inputs:
        print("Drag one or more PDF files or folders onto this script or EXE.")
        print(
            "Outputs are created automatically in a fixed_pdf folder "
            "next to each dropped source."
        )
        if _should_force_windows_exit_pause():
            _pause_for_windows_dragdrop(force_windows_pause=True)
        return 0

    windows_bulk_warning = _build_windows_bulk_drop_warning(args.inputs)
    if windows_bulk_warning:
        print(f"[NOTICE] {windows_bulk_warning}")
    if args.overwrite_output:
        print(f"[NOTICE] {OVERWRITE_IGNORED_NOTICE}")

    requested_mode = RequestedMode(args.mode)
    runtime_settings = RuntimeSettings(
        overwrite_existing_output=False,
    )
    batch_settings = BatchSettings(
        enable_parallel=not args.no_parallel,
        max_workers=args.workers,
    )

    batch_started_at = time.perf_counter()
    summary = clean_batch(
        input_paths=args.inputs,
        requested_mode=requested_mode,
        batch_settings=batch_settings,
        runtime_settings=runtime_settings,
        gs_exe=_build_gs_path(sys.argv[0]),
        output_dir=args.output_dir,
        progress_callback=_build_progress_reporter(),
    )
    wall_clock_seconds = time.perf_counter() - batch_started_at

    results_to_print = _select_console_results(
        summary=summary,
        show_all_results=bool(args.show_all_results),
    )

    suppressed_successes = len(summary.results) - len(results_to_print)
    if suppressed_successes > 0:
        print(
            "[NOTICE] "
            f"Suppressed {suppressed_successes} successful per-file result lines "
            "for large-batch performance. Use --show-all-results to print every file."
        )

    for diagnostics in results_to_print:
        print_diagnostics(diagnostics)

    print_batch_summary(summary, wall_clock_seconds=wall_clock_seconds)

    if _should_force_windows_exit_pause():
        _pause_for_windows_dragdrop(force_windows_pause=True)
    elif summary.failed > 0:
        _pause_for_windows_dragdrop()

    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main(sys.argv[1:]))
