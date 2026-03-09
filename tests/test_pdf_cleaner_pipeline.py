"""Regression tests for structural-first PDF repair pipeline."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Tuple
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


def write_shopify_style_pdf(path: Path) -> None:
    """Write a Chromium-like single-page PDF with text and font resources."""
    path.write_bytes(MINIMAL_TEXT_PDF)
    with pikepdf.Pdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Producer"] = "Skia/PDF m120"
        pdf.docinfo["/Creator"] = "Chromium"
        pdf.docinfo["/Title"] = "Shopify invoice"
        pdf.save(
            path,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            compress_streams=True,
        )


def fake_ghostscript_copy(
    input_path: Path, output_path: Path, gs_exe: str
) -> Tuple[bool, str]:
    """Fake Ghostscript implementation for deterministic tests."""
    _ = gs_exe
    shutil.copyfile(input_path, output_path)
    return True, ""


class PdfCleanerPipelineTests(unittest.TestCase):
    """Tests for structural-first behavior and Ghostscript fallback."""

    def test_structural_repair_on_shopify_style_pdf(self) -> None:
        """Default mode should keep text/font content without Ghostscript."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_pdf = Path(temp_dir) / "shopify_invoice.pdf"
            write_shopify_style_pdf(test_pdf)

            with mock.patch("pdf_cleaner.run_ghostscript_compatibility") as gs_mock:
                diagnostics = pdf_cleaner.clean_pdf(
                    input_path=str(test_pdf),
                    gs_exe="gswin64c",
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                )

            self.assertIsNotNone(diagnostics)
            assert diagnostics is not None
            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.mode_used, pdf_cleaner.STRUCTURAL_MODE_LABEL)
            self.assertTrue(diagnostics.text_preserved)
            self.assertTrue(diagnostics.fonts_present)
            gs_mock.assert_not_called()

            inspection = pdf_cleaner.inspect_pdf(test_pdf)
            self.assertTrue(inspection.has_text)
            self.assertTrue(inspection.has_fonts)

    def test_auto_mode_falls_back_for_problematic_structural_output(self) -> None:
        """Default mode should invoke Ghostscript fallback when validation fails."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_pdf = Path(temp_dir) / "problematic.pdf"
            write_shopify_style_pdf(test_pdf)

            with mock.patch(
                "pdf_cleaner.validate_structural_output",
                return_value=pdf_cleaner.ValidationResult(
                    valid=False, reasons=("forced_validation_failure",)
                ),
            ), mock.patch(
                "pdf_cleaner.run_ghostscript_compatibility",
                side_effect=fake_ghostscript_copy,
            ) as gs_mock:
                diagnostics = pdf_cleaner.clean_pdf(
                    input_path=str(test_pdf),
                    gs_exe="gswin64c",
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                )

            self.assertIsNotNone(diagnostics)
            assert diagnostics is not None
            self.assertTrue(diagnostics.success)
            self.assertEqual(
                diagnostics.mode_used,
                pdf_cleaner.GHOSTSCRIPT_FALLBACK_MODE_LABEL,
            )
            self.assertTrue(diagnostics.text_preserved)
            self.assertTrue(diagnostics.fonts_present)
            gs_mock.assert_called_once()

    def test_default_mode_avoids_unnecessary_ghostscript(self) -> None:
        """Default mode should not call Ghostscript when structural rewrite is valid."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_pdf = Path(temp_dir) / "default_mode.pdf"
            write_shopify_style_pdf(test_pdf)

            with mock.patch("pdf_cleaner.run_ghostscript_compatibility") as gs_mock:
                diagnostics = pdf_cleaner.clean_pdf(
                    input_path=str(test_pdf),
                    gs_exe="gswin64c",
                    requested_mode=pdf_cleaner.RequestedMode.AUTO,
                )

            self.assertIsNotNone(diagnostics)
            assert diagnostics is not None
            self.assertTrue(diagnostics.success)
            self.assertEqual(diagnostics.mode_used, pdf_cleaner.STRUCTURAL_MODE_LABEL)
            gs_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
