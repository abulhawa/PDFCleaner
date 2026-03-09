"""PDF Cleaner entrypoint with structural-first repair and Ghostscript fallback."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pikepdf
from pikepdf import Dictionary, Pdf

MIN_VALID_PDF_BYTES: int = 100
MAX_REASONABLE_SIZE_MULTIPLIER: float = 4.0
TEXT_SHOWING_OPERATORS: set[str] = {"Tj", "TJ", "'", '"'}
STRUCTURAL_MODE_LABEL: str = "structural"
GHOSTSCRIPT_MODE_LABEL: str = "ghostscript"
GHOSTSCRIPT_FALLBACK_MODE_LABEL: str = "ghostscript_fallback"


class RequestedMode(str, Enum):
    """Available repair modes for each file."""

    AUTO = "auto"
    STRUCTURAL = "structural"
    GHOSTSCRIPT = "ghostscript"


@dataclass(frozen=True)
class PdfInspection:
    """Lightweight characteristics used for validation and diagnostics."""

    page_count: int
    has_text: bool
    has_fonts: bool


@dataclass(frozen=True)
class ValidationResult:
    """Validation result for a structural rewrite candidate."""

    valid: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepairDiagnostics:
    """Per-file report emitted to the terminal and tests."""

    input_path: Path
    success: bool
    mode_used: str
    text_preserved: bool
    fonts_present: bool
    input_size: int
    output_size: int
    message: str


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
        # If font resources exist but are odd, treat them as present.
        return True


def _page_has_text(page: pikepdf.Page) -> bool:
    """Detect text drawing operators in page content streams."""
    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return False

    for instruction in instructions:
        operator_name = str(instruction.operator)
        if operator_name in TEXT_SHOWING_OPERATORS:
            return True
    return False


def inspect_pdf(path: Path) -> PdfInspection:
    """Inspect page count, text operators, and font resources for a PDF."""
    has_text = False
    has_fonts = False

    with Pdf.open(path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            if not has_fonts and _page_has_fonts(page):
                has_fonts = True
            if not has_text and _page_has_text(page):
                has_text = True
            if has_text and has_fonts:
                break

    return PdfInspection(page_count=page_count, has_text=has_text, has_fonts=has_fonts)


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
) -> ValidationResult:
    """Validate that a structural rewrite did not regress key PDF traits."""
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
        input_size > 0
        and output_size > int(input_size * MAX_REASONABLE_SIZE_MULTIPLIER)
    ):
        reasons.append("output_size_growth_exceeds_limit")

    return ValidationResult(valid=not reasons, reasons=tuple(reasons))


def run_ghostscript_compatibility(
    input_path: Path, output_path: Path, gs_exe: str
) -> tuple[bool, str]:
    """Run Ghostscript PDF/A conversion as compatibility fallback mode."""
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


def _cleanup_temp_files(paths: Iterable[Path]) -> None:
    """Best-effort cleanup of temporary output files."""
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def _safe_inspection(path: Path, fallback: PdfInspection) -> PdfInspection:
    """Inspect a PDF and return fallback characteristics if inspection fails."""
    try:
        return inspect_pdf(path)
    except Exception:
        return fallback


def _pause_for_windows_dragdrop() -> None:
    """Pause only in interactive sessions so users can read errors."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input("Press Enter to exit...")
    except EOFError:
        return


def _build_diagnostics(
    input_file: Path,
    success: bool,
    mode_used: str,
    input_size: int,
    output_size: int,
    input_inspection: PdfInspection,
    final_inspection: PdfInspection,
    message: str,
) -> RepairDiagnostics:
    """Build a consistent per-file diagnostics record."""
    text_preserved = (not input_inspection.has_text) or final_inspection.has_text
    return RepairDiagnostics(
        input_path=input_file,
        success=success,
        mode_used=mode_used,
        text_preserved=text_preserved,
        fonts_present=final_inspection.has_fonts,
        input_size=input_size,
        output_size=output_size,
        message=message,
    )


def clean_pdf(
    input_path: str, gs_exe: str, requested_mode: RequestedMode = RequestedMode.AUTO
) -> Optional[RepairDiagnostics]:
    """Repair a PDF with structural-first normalization and optional fallback."""
    input_file = Path(input_path)
    if not _is_pdf_path(input_file):
        return None

    print(f"[FILE] Processing: {input_file.name}")

    structural_temp = input_file.with_name(f"{input_file.name}.temp_structural.pdf")
    ghostscript_temp = input_file.with_name(f"{input_file.name}.temp_pdfa.pdf")

    input_size = input_file.stat().st_size
    input_inspection = PdfInspection(page_count=0, has_text=False, has_fonts=False)
    fallback_reason = ""

    try:
        try:
            input_inspection = inspect_pdf(input_file)
        except Exception as exc:
            fallback_reason = f"input_inspection_error: {exc}"

        if requested_mode != RequestedMode.GHOSTSCRIPT:
            try:
                structural_normalize_pdf(input_file, structural_temp)
                structural_inspection = inspect_pdf(structural_temp)
                structural_size = structural_temp.stat().st_size
                structural_validation = validate_structural_output(
                    input_inspection=input_inspection,
                    output_inspection=structural_inspection,
                    input_size=input_size,
                    output_size=structural_size,
                )
            except Exception as exc:
                structural_validation = ValidationResult(
                    valid=False, reasons=(f"structural_error: {exc}",)
                )
                structural_inspection = input_inspection

            if structural_validation.valid:
                os.replace(structural_temp, input_file)
                output_size = input_file.stat().st_size
                diagnostics = _build_diagnostics(
                    input_file=input_file,
                    success=True,
                    mode_used=STRUCTURAL_MODE_LABEL,
                    input_size=input_size,
                    output_size=output_size,
                    input_inspection=input_inspection,
                    final_inspection=structural_inspection,
                    message="Structural normalization completed.",
                )
                return diagnostics

            fallback_reason = "; ".join(structural_validation.reasons)
            if requested_mode == RequestedMode.STRUCTURAL:
                diagnostics = _build_diagnostics(
                    input_file=input_file,
                    success=False,
                    mode_used=STRUCTURAL_MODE_LABEL,
                    input_size=input_size,
                    output_size=input_size,
                    input_inspection=input_inspection,
                    final_inspection=input_inspection,
                    message=f"Structural validation failed: {fallback_reason}",
                )
                return diagnostics

        gs_success, gs_message = run_ghostscript_compatibility(
            input_path=input_file,
            output_path=ghostscript_temp,
            gs_exe=gs_exe,
        )
        if not gs_success:
            diagnostics = _build_diagnostics(
                input_file=input_file,
                success=False,
                mode_used=GHOSTSCRIPT_MODE_LABEL
                if requested_mode == RequestedMode.GHOSTSCRIPT
                else GHOSTSCRIPT_FALLBACK_MODE_LABEL,
                input_size=input_size,
                output_size=input_size,
                input_inspection=input_inspection,
                final_inspection=input_inspection,
                message=gs_message,
            )
            return diagnostics

        os.replace(ghostscript_temp, input_file)
        final_inspection = _safe_inspection(path=input_file, fallback=input_inspection)
        output_size = input_file.stat().st_size
        mode_label = (
            GHOSTSCRIPT_MODE_LABEL
            if requested_mode == RequestedMode.GHOSTSCRIPT
            else GHOSTSCRIPT_FALLBACK_MODE_LABEL
        )
        message = "Ghostscript compatibility conversion completed."
        if fallback_reason:
            message = f"{message} Fallback reason: {fallback_reason}"
        diagnostics = _build_diagnostics(
            input_file=input_file,
            success=True,
            mode_used=mode_label,
            input_size=input_size,
            output_size=output_size,
            input_inspection=input_inspection,
            final_inspection=final_inspection,
            message=message,
        )
        return diagnostics

    except Exception as exc:
        diagnostics = _build_diagnostics(
            input_file=input_file,
            success=False,
            mode_used="error",
            input_size=input_size,
            output_size=input_size,
            input_inspection=input_inspection,
            final_inspection=input_inspection,
            message=f"Unexpected processing error: {exc}",
        )
        return diagnostics
    finally:
        _cleanup_temp_files([structural_temp, ghostscript_temp])


def print_diagnostics(diagnostics: RepairDiagnostics) -> None:
    """Emit stable per-file diagnostics to stdout."""
    text_preserved = "yes" if diagnostics.text_preserved else "no"
    fonts_present = "yes" if diagnostics.fonts_present else "no"
    size_change = _format_size_change(
        diagnostics.input_size, diagnostics.output_size
    )
    status = "OK" if diagnostics.success else "ERROR"
    print(
        "[RESULT] "
        f"{diagnostics.input_path.name} | "
        f"status={status} | "
        f"mode={diagnostics.mode_used} | "
        f"text_preserved={text_preserved} | "
        f"fonts_present={fonts_present} | "
        f"size_change={size_change} "
        f"({diagnostics.input_size} -> {diagnostics.output_size} bytes)"
    )
    if diagnostics.message:
        print(f"[DETAIL] {diagnostics.message}")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI args while keeping drag-and-drop file usage simple."""
    parser = argparse.ArgumentParser(
        description=(
            "Clean PDFs with structural normalization first and Ghostscript fallback."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RequestedMode],
        default=RequestedMode.AUTO.value,
        help=(
            "Repair mode: 'auto' (default) uses structural first then fallback, "
            "'structural' disables fallback, "
            "'ghostscript' forces compatibility conversion."
        ),
    )
    parser.add_argument("inputs", nargs="*", help="Input PDF file paths.")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint for script and packaged executable."""
    args = _parse_args(argv)
    if not args.inputs:
        print("Drag one or more PDF files onto this script or EXE to fix them.")
        return 0

    requested_mode = RequestedMode(args.mode)
    gs_exe = _build_gs_path(sys.argv[0])
    had_errors = False

    for input_file in args.inputs:
        diagnostics = clean_pdf(
            input_path=input_file, gs_exe=gs_exe, requested_mode=requested_mode
        )
        if diagnostics is None:
            continue
        print_diagnostics(diagnostics)
        if not diagnostics.success:
            had_errors = True

    print("\n[DONE] All files processed.")
    if had_errors:
        _pause_for_windows_dragdrop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
