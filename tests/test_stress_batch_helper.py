"""Tests for the local stress-batch helper workflow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import run_stress_batch


class StressBatchHelperTests(unittest.TestCase):
    """Validate deterministic file generation for disposable stress runs."""

    def test_duplicate_filename_is_deterministic(self) -> None:
        """Duplicate naming should be stable and zero-padded."""
        self.assertEqual(
            run_stress_batch._build_duplicate_filename("sample", 1, 4),
            "sample_0001.pdf",
        )
        self.assertEqual(
            run_stress_batch._build_duplicate_filename("sample", 1000, 4),
            "sample_1000.pdf",
        )

    def test_prepare_stress_input_dir_requires_reset_for_non_empty_dir(self) -> None:
        """Non-empty stress folders should require explicit reset."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stress_input_dir = root / "stress_test_input"
            stress_input_dir.mkdir(parents=True, exist_ok=True)
            (stress_input_dir / "placeholder.txt").write_text("x", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_stress_batch._prepare_stress_input_dir(
                    input_dir=stress_input_dir,
                    reset_existing=False,
                )

            recreated = run_stress_batch._prepare_stress_input_dir(
                input_dir=stress_input_dir,
                reset_existing=True,
            )
            self.assertEqual(recreated, stress_input_dir.resolve())
            self.assertEqual(list(recreated.iterdir()), [])

    def test_generate_stress_input_copies_creates_unique_exact_copies(self) -> None:
        """Source PDF should be duplicated into deterministic unique copy names."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_pdf = root / "original.pdf"
            source_bytes = b"%PDF-1.4\nsample\n%%EOF\n"
            source_pdf.write_bytes(source_bytes)

            stress_input_dir = root / "stress_test_input"
            stress_input_dir.mkdir(parents=True, exist_ok=True)

            copied = run_stress_batch.generate_stress_input_copies(
                source_pdf=source_pdf,
                stress_input_dir=stress_input_dir,
                count=5,
            )

            self.assertEqual(len(copied), 5)
            copied_names = [path.name for path in copied]
            self.assertEqual(
                copied_names,
                [
                    "original_0001.pdf",
                    "original_0002.pdf",
                    "original_0003.pdf",
                    "original_0004.pdf",
                    "original_0005.pdf",
                ],
            )
            self.assertEqual(len(set(copied_names)), 5)

            for copied_path in copied:
                self.assertTrue(copied_path.exists())
                self.assertEqual(copied_path.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
