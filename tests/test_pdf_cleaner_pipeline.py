"""Regression tests for the structural-first PDF cleaning pipeline."""

from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
