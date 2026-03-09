"""Regression tests for the structural-first PDF cleaning pipeline."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import pikepdf

import pdf_cleaner


MINIMAL_TEXT_PDF: bytes = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Count 1 /Kids [3 0 R] >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT /F1 24 Tf 100 100 Td (Hello PDF) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000059 00000 n 
0000000116 00000 n 
0000000242 00000 n 
0000000336 00000 n 
trailer
<< /Root 1 0 R /Size 6 >>
startxref
406
%%EOF
"""


def _build_minimal_pdf(objects: list[bytes]) -> bytes:
    """Build a minimal PDF document from object payloads."""
    header = b"%PDF-1.4\n"
    body_parts: list[bytes] = [header]
    offsets: list[int] = [0]
    cursor = len(header)

    for index, obj_payload in enumerate(objects, start=1):
        obj_block = b"".join(
            [f"{index} 0 obj\n".encode("ascii"), obj_payload, b"\nendobj\n"]
        )
        offsets.append(cursor)
        body_parts.append(obj_block)
        cursor += len(obj_block)

    xref_offset = cursor
    xref_lines: list[bytes] = [f"xref\n0 {len(objects) + 1}\n".encode("ascii")]
    xref_lines.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        xref_lines.append(f"{offset:010d} 00000 n \n".encode("ascii"))

    trailer = (
        f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return b"".join(body_parts + xref_lines + [trailer])


def write_text_pdf(path: Path) -> None:
    """Write a single-page text PDF fixture with font resources."""
    path.write_bytes(MINIMAL_TEXT_PDF)
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Producer"] = "Skia/PDF m120"
        pdf.docinfo["/Creator"] = "Chromium"
        pdf.docinfo["/Title"] = "Invoice"
        pdf.save(
            path,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
        )


def write_image_only_pdf(path: Path) -> None:
    """Write a single-page PDF fixture that draws only an embedded image XObject."""
    image_bytes = b"\x00\x00\x00"
    content_stream = b"q 300 0 0 144 0 0 cm /Im0 Do Q"
    image_object = b"".join(
        [
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 ",
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\n",
            b"stream\n",
            image_bytes,
            b"\nendstream",
        ]
    )
    content_object = b"".join(
        [
            f"<< /Length {len(content_stream)} >>\n".encode("ascii"),
            b"stream\n",
            content_stream,
            b"\nendstream",
        ]
    )
    image_pdf = _build_minimal_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
            content_object,
            image_object,
        ]
    )
    path.write_bytes(image_pdf)

    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Producer"] = "ImageFixture"
        pdf.save(
            path,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
        )


class PdfCleanerPipelineTests(unittest.TestCase):
    """Tests for structural-first behavior, output routing, and batch handling."""

    def _sequential_batch_settings(self) -> pdf_cleaner.BatchSettings:
        """Return deterministic batch settings for unit tests."""
        return pdf_cleaner.BatchSettings(enable_parallel=False, max_workers=1)

    def test_clean_pdf_writes_to_explicit_output_folder(self) -> None:
        """clean_pdf should not overwrite input and should write _cleaned output."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "invoice.pdf"
            output_dir = temp_root / "out"
            write_text_pdf(source_pdf)
            source_before = source_pdf.read_bytes()

            diagnostics = pdf_cleaner.clean_pdf(
                input_path=source_pdf,
                output_dir=output_dir,
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
            )

            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.mode_used, pdf_cleaner.STRUCTURAL_MODE_LABEL)
            self.assertEqual(diagnostics.output_path, output_dir / "invoice_cleaned.pdf")
            self.assertTrue((output_dir / "invoice_cleaned.pdf").exists())
            self.assertEqual(source_pdf.read_bytes(), source_before)

            output_inspection = pdf_cleaner.inspect_pdf(output_dir / "invoice_cleaned.pdf")
            self.assertTrue(output_inspection.has_text)
            self.assertTrue(output_inspection.has_fonts)

    def test_auto_mode_text_pdf_does_not_fallback_to_ghostscript(self) -> None:
        """Auto mode should fail structural validation instead of invoking Ghostscript."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "problematic.pdf"
            output_dir = temp_root / "out"
            write_text_pdf(source_pdf)

            with mock.patch(
                "pdf_cleaner.validate_structural_output",
                return_value=pdf_cleaner.ValidationResult(
                    valid=False,
                    reasons=("forced_validation_failure",),
                ),
            ), mock.patch("pdf_cleaner.run_ghostscript_compatibility") as gs_mock:
                diagnostics = pdf_cleaner.clean_pdf(
                    input_path=source_pdf,
                    output_dir=output_dir,
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                )

            self.assertFalse(diagnostics.success)
            self.assertFalse(diagnostics.skipped)
            self.assertEqual(diagnostics.mode_used, pdf_cleaner.STRUCTURAL_MODE_LABEL)
            assert diagnostics.failure_reason is not None
            self.assertIn("forced_validation_failure", diagnostics.failure_reason)
            gs_mock.assert_not_called()

    def test_output_collision_uses_incrementing_suffix(self) -> None:
        """Collision handling should use deterministic numeric suffixes."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "invoice.pdf"
            output_dir = temp_root / "out"
            output_dir.mkdir(parents=True, exist_ok=True)
            write_text_pdf(source_pdf)

            (output_dir / "invoice_cleaned.pdf").write_bytes(b"x")
            (output_dir / "invoice_cleaned_1.pdf").write_bytes(b"y")

            diagnostics = pdf_cleaner.clean_pdf(
                input_path=source_pdf,
                output_dir=output_dir,
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
            )

            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.output_path, output_dir / "invoice_cleaned_2.pdf")
            self.assertTrue((output_dir / "invoice_cleaned_2.pdf").exists())

    def test_overwrite_flag_is_ignored_and_never_replaces_existing_file(self) -> None:
        """Overwrite runtime setting should be ignored to protect existing outputs."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "invoice.pdf"
            output_dir = temp_root / "out"
            output_dir.mkdir(parents=True, exist_ok=True)
            write_text_pdf(source_pdf)

            original_existing_bytes = b"do-not-overwrite"
            existing_output = output_dir / "invoice_cleaned.pdf"
            existing_output.write_bytes(original_existing_bytes)

            diagnostics = pdf_cleaner.clean_pdf(
                input_path=source_pdf,
                output_dir=output_dir,
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
                runtime_settings=pdf_cleaner.RuntimeSettings(
                    overwrite_existing_output=True
                ),
            )

            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.output_path, output_dir / "invoice_cleaned_1.pdf")
            self.assertEqual(existing_output.read_bytes(), original_existing_bytes)

    def test_existing_explicit_output_path_is_not_replaced(self) -> None:
        """Explicit pre-existing output paths should be rerouted to a collision-safe name."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "invoice.pdf"
            output_dir = temp_root / "out"
            output_dir.mkdir(parents=True, exist_ok=True)
            write_text_pdf(source_pdf)

            explicit_output_path = output_dir / "invoice_cleaned.pdf"
            explicit_output_path.write_bytes(b"keep-existing")

            diagnostics = pdf_cleaner.clean_pdf(
                input_path=source_pdf,
                output_dir=output_dir,
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
                output_path=explicit_output_path,
            )

            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.output_path, output_dir / "invoice_cleaned_1.pdf")
            self.assertEqual(explicit_output_path.read_bytes(), b"keep-existing")

    def test_auto_mode_image_only_uses_passthrough(self) -> None:
        """Auto mode should use image passthrough for image-only inspections."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "scan.pdf"
            output_dir = temp_root / "out"
            write_text_pdf(source_pdf)

            image_only_inspection = pdf_cleaner.PdfInspection(
                page_count=1,
                has_text=False,
                has_fonts=False,
                has_images=True,
                has_vector_graphics=False,
                pdf_kind=pdf_cleaner.PdfKind.IMAGE_ONLY,
            )

            with mock.patch(
                "pdf_cleaner.inspect_pdf",
                return_value=image_only_inspection,
            ):
                diagnostics = pdf_cleaner.clean_pdf(
                    input_path=source_pdf,
                    output_dir=output_dir,
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                )

            self.assertTrue(diagnostics.success)
            self.assertEqual(
                diagnostics.mode_used,
                pdf_cleaner.IMAGE_PASSTHROUGH_MODE_LABEL,
            )
            assert diagnostics.output_path is not None
            self.assertEqual(diagnostics.output_path.read_bytes(), source_pdf.read_bytes())

    def test_auto_mode_real_image_only_pdf_uses_passthrough(self) -> None:
        """Auto mode should passthrough a real image-only PDF fixture."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_pdf = temp_root / "scan.pdf"
            output_dir = temp_root / "out"
            write_image_only_pdf(source_pdf)

            inspection = pdf_cleaner.inspect_pdf(source_pdf)
            self.assertEqual(inspection.pdf_kind, pdf_cleaner.PdfKind.IMAGE_ONLY)
            self.assertFalse(inspection.has_text)
            self.assertFalse(inspection.has_fonts)
            self.assertTrue(inspection.has_images)

            diagnostics = pdf_cleaner.clean_pdf(
                input_path=source_pdf,
                output_dir=output_dir,
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
            )

            self.assertTrue(diagnostics.success)
            self.assertEqual(
                diagnostics.mode_used,
                pdf_cleaner.IMAGE_PASSTHROUGH_MODE_LABEL,
            )
            assert diagnostics.output_path is not None
            self.assertEqual(diagnostics.output_path.read_bytes(), source_pdf.read_bytes())

    def test_batch_continues_after_per_file_failure(self) -> None:
        """A failing file should not stop the batch and summary should be accurate."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            first_pdf = temp_root / "first.pdf"
            second_pdf = temp_root / "second.pdf"
            non_pdf = temp_root / "notes.txt"
            write_text_pdf(first_pdf)
            write_text_pdf(second_pdf)
            non_pdf.write_text("not a pdf", encoding="utf-8")

            original_structural = pdf_cleaner.structural_normalize_pdf

            def conditional_structural(input_path: Path, output_path: Path) -> None:
                if Path(input_path).name == "second.pdf":
                    raise RuntimeError("forced_failure")
                original_structural(input_path, output_path)

            with mock.patch(
                "pdf_cleaner.structural_normalize_pdf",
                side_effect=conditional_structural,
            ):
                summary = pdf_cleaner.clean_batch(
                    input_paths=[first_pdf, second_pdf, non_pdf],
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                    batch_settings=self._sequential_batch_settings(),
                )

            self.assertEqual(summary.total_files, 3)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.text_pdfs_processed, 2)
            self.assertEqual(summary.image_only_pdfs_processed, 0)

            second_result = next(
                result for result in summary.results if result.input_path.name == "second.pdf"
            )
            self.assertFalse(second_result.success)
            assert second_result.failure_reason is not None
            self.assertIn("forced_failure", second_result.failure_reason)

    def test_parallel_batch_worker_failure_does_not_stop_other_files(self) -> None:
        """Parallel batch processing should isolate failures and avoid output collisions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_a = temp_root / "source_a"
            source_b = temp_root / "source_b"
            source_bad = temp_root / "source_bad"
            source_a.mkdir(parents=True, exist_ok=True)
            source_b.mkdir(parents=True, exist_ok=True)
            source_bad.mkdir(parents=True, exist_ok=True)

            first_pdf = source_a / "invoice.pdf"
            second_pdf = source_b / "invoice.pdf"
            broken_pdf = source_bad / "broken.pdf"
            write_text_pdf(first_pdf)
            write_text_pdf(second_pdf)
            broken_pdf.write_bytes(b"%PDF-1.4\nthis is not a valid PDF body\n")

            shared_output_dir = temp_root / "shared_out"

            with mock.patch("pdf_cleaner.os.cpu_count", return_value=4):
                summary = pdf_cleaner.clean_batch(
                    input_paths=[first_pdf, second_pdf, broken_pdf],
                    output_dir=shared_output_dir,
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                    batch_settings=pdf_cleaner.BatchSettings(
                        enable_parallel=True,
                        max_workers=3,
                        parallel_threshold=1,
                    ),
                )

            self.assertEqual(summary.worker_count, 3)
            self.assertEqual(summary.total_files, 3)
            self.assertEqual(summary.succeeded, 2)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.skipped, 0)
            self.assertEqual(summary.text_pdfs_processed, 2)
            self.assertEqual(summary.image_only_pdfs_processed, 0)

            output_paths = [result.output_path for result in summary.results]
            self.assertEqual(len(output_paths), 3)
            self.assertEqual(len(output_paths), len(set(output_paths)))

            successful_results = [result for result in summary.results if result.success]
            self.assertEqual(len(successful_results), 2)
            for successful in successful_results:
                assert successful.output_path is not None
                self.assertTrue(successful.output_path.exists())

            successful_output_names = sorted(
                successful.output_path.name
                for successful in successful_results
                if successful.output_path is not None
            )
            self.assertEqual(
                successful_output_names,
                ["invoice_cleaned.pdf", "invoice_cleaned_1.pdf"],
            )

            failed_result = next(result for result in summary.results if not result.success)
            assert failed_result.failure_reason is not None
            self.assertIn("structural_error", failed_result.failure_reason)

    def test_dropped_folder_routes_outputs_to_folder_fixed_pdf(self) -> None:
        """Dropped folder inputs should write under <folder>/fixed_pdf."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            dropped_folder = temp_root / "incoming"
            nested = dropped_folder / "nested"
            nested.mkdir(parents=True, exist_ok=True)

            root_pdf = dropped_folder / "root.pdf"
            nested_pdf = nested / "child.pdf"
            write_text_pdf(root_pdf)
            write_text_pdf(nested_pdf)

            summary = pdf_cleaner.clean_batch(
                input_paths=[dropped_folder],
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
                batch_settings=self._sequential_batch_settings(),
            )

            expected_output_dir = dropped_folder / pdf_cleaner.DEFAULT_OUTPUT_FOLDER_NAME
            self.assertEqual(summary.total_files, 2)
            self.assertEqual(summary.succeeded, 2)
            for result in summary.results:
                assert result.output_path is not None
                self.assertEqual(result.output_path.parent, expected_output_dir)
                self.assertTrue(result.output_path.exists())

    def test_mixed_source_files_use_per_source_fixed_pdf(self) -> None:
        """Mixed-source dropped files should use per-source fixed_pdf folders."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_a = temp_root / "a"
            source_b = temp_root / "b"
            source_a.mkdir(parents=True, exist_ok=True)
            source_b.mkdir(parents=True, exist_ok=True)

            pdf_a = source_a / "a.pdf"
            pdf_b = source_b / "b.pdf"
            write_text_pdf(pdf_a)
            write_text_pdf(pdf_b)

            summary = pdf_cleaner.clean_batch(
                input_paths=[pdf_a, pdf_b],
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
                batch_settings=self._sequential_batch_settings(),
            )

            self.assertEqual(summary.total_files, 2)
            self.assertEqual(summary.succeeded, 2)
            expected_a = source_a / pdf_cleaner.DEFAULT_OUTPUT_FOLDER_NAME
            expected_b = source_b / pdf_cleaner.DEFAULT_OUTPUT_FOLDER_NAME

            result_map = {result.input_path.name: result for result in summary.results}
            assert result_map["a.pdf"].output_path is not None
            assert result_map["b.pdf"].output_path is not None
            self.assertEqual(result_map["a.pdf"].output_path.parent, expected_a)
            self.assertEqual(result_map["b.pdf"].output_path.parent, expected_b)

    def test_windows_bulk_drop_warning_for_large_individual_file_list(self) -> None:
        """Windows should warn for very large individual file-drop argument lists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            dropped_files: list[str] = []
            for index in range(pdf_cleaner.WINDOWS_ARG_LIST_WARNING_COUNT):
                candidate = temp_root / f"drop_{index:04d}.pdf"
                candidate.write_bytes(b"x")
                dropped_files.append(str(candidate))

            with mock.patch("pdf_cleaner.os.name", "nt"):
                warning = pdf_cleaner._build_windows_bulk_drop_warning(dropped_files)

            self.assertIsNotNone(warning)
            assert warning is not None
            self.assertIn("drag the containing folder", warning)

    def test_windows_bulk_drop_warning_not_shown_for_folder_input(self) -> None:
        """Windows warning should not appear when a folder is dropped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            dropped_folder = temp_root / "incoming"
            dropped_folder.mkdir(parents=True, exist_ok=True)

            with mock.patch("pdf_cleaner.os.name", "nt"):
                warning = pdf_cleaner._build_windows_bulk_drop_warning(
                    [str(dropped_folder)]
                )

            self.assertIsNone(warning)

    def test_force_windows_exit_pause_detection(self) -> None:
        """Packaged Windows runs should force a final keypress pause."""
        with mock.patch("pdf_cleaner.os.name", "nt"), mock.patch.object(
            pdf_cleaner.sys, "frozen", True, create=True
        ):
            self.assertTrue(pdf_cleaner._should_force_windows_exit_pause())

        with mock.patch("pdf_cleaner.os.name", "nt"), mock.patch.object(
            pdf_cleaner.sys, "frozen", True, create=True
        ), mock.patch.object(pdf_cleaner.sys, "stdin", None):
            self.assertTrue(pdf_cleaner._should_force_windows_exit_pause())

        with mock.patch("pdf_cleaner.os.name", "nt"), mock.patch.object(
            pdf_cleaner.sys, "frozen", False, create=True
        ):
            self.assertFalse(pdf_cleaner._should_force_windows_exit_pause())

        with mock.patch("pdf_cleaner.os.name", "posix"), mock.patch.object(
            pdf_cleaner.sys, "frozen", True, create=True
        ):
            self.assertFalse(pdf_cleaner._should_force_windows_exit_pause())

    def test_parallel_batch_recovers_from_broken_process_pool(self) -> None:
        """Broken process pools should fallback to sequential processing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            first_pdf = temp_root / "first.pdf"
            second_pdf = temp_root / "second.pdf"
            output_dir = temp_root / "out"
            write_text_pdf(first_pdf)
            write_text_pdf(second_pdf)

            def success_diagnostics(
                input_path: Path,
                output_path: Path,
            ) -> pdf_cleaner.RepairDiagnostics:
                return pdf_cleaner.RepairDiagnostics(
                    input_path=input_path,
                    output_path=output_path,
                    success=True,
                    skipped=False,
                    mode_used=pdf_cleaner.STRUCTURAL_MODE_LABEL,
                    pdf_kind=pdf_cleaner.PdfKind.TEXT_VECTOR,
                    text_preserved=True,
                    fonts_present=True,
                    input_size=100,
                    output_size=80,
                    elapsed_seconds=0.01,
                    message="ok",
                    failure_reason=None,
                )

            fallback_inputs: list[str] = []

            def fake_clean_pdf(
                input_path: str | Path,
                output_dir: str | Path,
                requested_mode: pdf_cleaner.RequestedMode = pdf_cleaner.RequestedMode.AUTO,
                runtime_settings: pdf_cleaner.RuntimeSettings | None = None,
                gs_exe: str | None = None,
                output_path: Path | None = None,
            ) -> pdf_cleaner.RepairDiagnostics:
                del output_dir, requested_mode, runtime_settings, gs_exe
                input_file = Path(input_path)
                assert output_path is not None
                fallback_inputs.append(input_file.name)
                return success_diagnostics(
                    input_path=input_file,
                    output_path=output_path,
                )

            class FakeExecutor:
                """Minimal executor that simulates one broken pool future."""

                def __init__(self, max_workers: int) -> None:
                    del max_workers
                    self.submit_count = 0

                def __enter__(self) -> "FakeExecutor":
                    return self

                def __exit__(self, exc_type, exc, tb) -> bool:
                    del exc_type, exc, tb
                    return False

                def submit(self, fn, worker_request: pdf_cleaner.WorkerRequest) -> Future:
                    del fn
                    future: Future = Future()
                    self.submit_count += 1
                    if self.submit_count == 1:
                        future.set_exception(BrokenProcessPool("simulated_pool_break"))
                    else:
                        future.set_result(
                            success_diagnostics(
                                input_path=Path(worker_request.input_path),
                                output_path=Path(worker_request.output_path),
                            )
                        )
                    return future

            with mock.patch("pdf_cleaner.os.cpu_count", return_value=4), mock.patch(
                "pdf_cleaner.ProcessPoolExecutor", FakeExecutor
            ), mock.patch("pdf_cleaner.clean_pdf", side_effect=fake_clean_pdf) as clean_mock:
                summary = pdf_cleaner.clean_batch(
                    input_paths=[first_pdf, second_pdf],
                    output_dir=output_dir,
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                    batch_settings=pdf_cleaner.BatchSettings(
                        enable_parallel=True,
                        max_workers=2,
                        parallel_threshold=1,
                    ),
                )

            self.assertEqual(summary.total_files, 2)
            self.assertEqual(summary.succeeded, 2)
            self.assertEqual(summary.failed, 0)
            self.assertEqual(summary.worker_count, 2)
            self.assertEqual(clean_mock.call_count, 1)
            self.assertEqual(fallback_inputs, ["first.pdf"])

    def test_select_console_results_suppresses_successes_for_large_batches(self) -> None:
        """Large batches should print failures/skips only unless explicitly requested."""
        success = pdf_cleaner.RepairDiagnostics(
            input_path=Path("ok.pdf"),
            output_path=Path("ok_cleaned.pdf"),
            success=True,
            skipped=False,
            mode_used=pdf_cleaner.STRUCTURAL_MODE_LABEL,
            pdf_kind=pdf_cleaner.PdfKind.TEXT_VECTOR,
            text_preserved=True,
            fonts_present=True,
            input_size=100,
            output_size=80,
            elapsed_seconds=0.01,
            message="ok",
            failure_reason=None,
        )
        failure = pdf_cleaner.RepairDiagnostics(
            input_path=Path("bad.pdf"),
            output_path=Path("bad_cleaned.pdf"),
            success=False,
            skipped=False,
            mode_used=pdf_cleaner.ERROR_MODE_LABEL,
            pdf_kind=pdf_cleaner.PdfKind.UNKNOWN,
            text_preserved=True,
            fonts_present=False,
            input_size=100,
            output_size=100,
            elapsed_seconds=0.01,
            message="failed",
            failure_reason="simulated_failure",
        )
        summary = pdf_cleaner.BatchSummary(
            total_files=pdf_cleaner.DEFAULT_DETAILED_CONSOLE_RESULT_LIMIT + 1,
            succeeded=1,
            failed=1,
            skipped=0,
            text_pdfs_processed=1,
            image_only_pdfs_processed=0,
            total_processing_seconds=0.02,
            average_processing_seconds=0.01,
            worker_count=1,
            results=(success, failure),
        )

        selected = pdf_cleaner._select_console_results(summary=summary, show_all_results=False)

        self.assertEqual(selected, (failure,))

    def test_select_console_results_show_all_keeps_successes(self) -> None:
        """Explicit show-all mode should keep all diagnostics."""
        success = pdf_cleaner.RepairDiagnostics(
            input_path=Path("ok.pdf"),
            output_path=Path("ok_cleaned.pdf"),
            success=True,
            skipped=False,
            mode_used=pdf_cleaner.STRUCTURAL_MODE_LABEL,
            pdf_kind=pdf_cleaner.PdfKind.TEXT_VECTOR,
            text_preserved=True,
            fonts_present=True,
            input_size=100,
            output_size=80,
            elapsed_seconds=0.01,
            message="ok",
            failure_reason=None,
        )
        summary = pdf_cleaner.BatchSummary(
            total_files=pdf_cleaner.DEFAULT_DETAILED_CONSOLE_RESULT_LIMIT + 10,
            succeeded=1,
            failed=0,
            skipped=0,
            text_pdfs_processed=1,
            image_only_pdfs_processed=0,
            total_processing_seconds=0.01,
            average_processing_seconds=0.01,
            worker_count=1,
            results=(success,),
        )

        selected = pdf_cleaner._select_console_results(summary=summary, show_all_results=True)

        self.assertEqual(selected, (success,))

    def test_clean_batch_progress_callback_reports_completion(self) -> None:
        """Batch progress callback should reach full completion count."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            first_pdf = temp_root / "first.pdf"
            second_pdf = temp_root / "second.pdf"
            write_text_pdf(first_pdf)
            write_text_pdf(second_pdf)

            updates: list[tuple[int, int]] = []
            summary = pdf_cleaner.clean_batch(
                input_paths=[first_pdf, second_pdf],
                requested_mode=pdf_cleaner.RequestedMode.AUTO,
                batch_settings=self._sequential_batch_settings(),
                progress_callback=lambda completed, total: updates.append(
                    (completed, total)
                ),
            )

            self.assertEqual(summary.total_files, 2)
            self.assertGreaterEqual(len(updates), 2)
            self.assertEqual(updates[-1], (2, 2))

    def test_print_batch_summary_uses_sentence_case_and_wall_time(self) -> None:
        """Summary output should use user-friendly labels and wall-clock timing."""
        summary = pdf_cleaner.BatchSummary(
            total_files=10,
            succeeded=8,
            failed=1,
            skipped=1,
            text_pdfs_processed=9,
            image_only_pdfs_processed=0,
            total_processing_seconds=12.0,
            average_processing_seconds=1.5,
            worker_count=4,
            results=tuple(),
        )

        output_buffer = StringIO()
        with redirect_stdout(output_buffer):
            pdf_cleaner.print_batch_summary(summary, wall_clock_seconds=5.0)
        output = output_buffer.getvalue()

        self.assertIn("[Summary]", output)
        self.assertIn("Total files: 10", output)
        self.assertIn("Wall time: 5.000 seconds", output)
        self.assertIn("Average wall time per processed file: 0.556 seconds", output)
        self.assertNotIn("total_processing_seconds", output)
        self.assertNotIn("wall_clock_seconds", output)
        self.assertNotIn("total_files=", output)


if __name__ == "__main__":
    unittest.main()
