# PDFCleaner - Structural-First PDF Repair

PDFCleaner is a Python-based PDF repair tool designed for drag-and-drop usage and packaging as a standalone Windows executable.

## Behavior Overview

The default pipeline is structural-first and non-destructive:

1. Inspect PDF structure/content to classify as `text_or_vector` or `image_only`
2. For text/vector PDFs: run structural normalization only (pikepdf/qpdf-backed)
3. Validate structural output against key characteristics (page count, text layer, fonts, size growth)
4. For image-only PDFs in `auto`: copy through without raster rewrite
5. Write cleaned output to a separate output folder (never overwrite input)

`ghostscript` mode remains explicit opt-in only.

## Default Output Policy

Default output folder name is:

`fixed_pdf`

Default routing rules:

- If you drop one or more files from a folder: outputs go to `<source_folder>/fixed_pdf/`
- If you drop a folder: outputs go to `<dropped_folder>/fixed_pdf/`
- If you drop mixed-source files in one run: each source folder gets its own `fixed_pdf` folder (per-source policy)

Output filename format:

`<original_stem>_cleaned.pdf`

Collision handling:

- If the target name already exists, numeric suffixes are appended deterministically:
  - `<stem>_cleaned_1.pdf`
  - `<stem>_cleaned_2.pdf`
  - etc.

## Running the Tool

### Drag-and-drop (primary workflow)

- Drag one or more PDF files or folders onto `pdf_cleaner.exe`
- The tool processes all discovered PDFs and writes outputs using the default policy above

### CLI usage

```bash
python pdf_cleaner.py input1.pdf input2.pdf
python pdf_cleaner.py --mode structural input.pdf
python pdf_cleaner.py --mode ghostscript input.pdf
python pdf_cleaner.py --workers 4 C:\path\to\folder
```

Optional flags:

- `--output-dir <path>`: override default per-source `fixed_pdf` routing
- `--no-parallel`: force sequential batch processing
- `--overwrite-output`: reuse `<stem>_cleaned.pdf` instead of creating suffixes

## Modes

- `auto` (default):
  - Text/vector PDFs: structural normalization only
  - Image-only PDFs: passthrough copy
  - No Ghostscript fallback for text/vector PDFs
- `structural`:
  - Structural normalization only
- `ghostscript`:
  - Explicit compatibility conversion

## Batch Summary

Batch execution reports:

- total files
- succeeded
- failed
- skipped
- text PDFs processed
- image-only PDFs processed
- total processing time
- average processing time
- per-file output path and failure reason

One file failure does not stop the batch.

## Tests

Run:

```bash
python -m unittest discover -s tests -v
```

Targeted validation for new high-risk paths:

```bash
python -m unittest tests.test_pdf_cleaner_pipeline.PdfCleanerPipelineTests.test_parallel_batch_worker_failure_does_not_stop_other_files -v
python -m unittest tests.test_pdf_cleaner_pipeline.PdfCleanerPipelineTests.test_auto_mode_real_image_only_pdf_uses_passthrough -v
```

## Local Sample Comparison Report

Run a directory-level comparison report (recursive) for real sample sets such as Shopify exports:

```bash
python tools/run_sample_report.py C:\samples\shopify --mode auto --workers 4 --csv reports\shopify_auto.csv
```

Report columns:

- `classification`
- `mode_used`
- `input_size`
- `output_size`
- `size_ratio` (`output_size / input_size`)
- `duration_seconds`
- `success`
- `reason` (failure reason/message when not successful)

## Packaging a Portable Standalone Windows EXE

Recommended build approach: **PyInstaller one-folder (`--onedir`)**

Reason:

- More reliable for native dependencies (`pikepdf`/qpdf libs)
- Faster startup than one-file extraction
- Easier to bundle Ghostscript binaries (`bin/gswin64c.exe`, `bin/gsdll64.dll`)
- Better behavior for a drag-and-drop utility distributed as a portable folder

### Prerequisites

```bash
python -m pip install --upgrade pyinstaller
```

### Portable folder build (preferred)

From repository root (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows_portable.ps1
```

Equivalent direct command:

```bash
python -m PyInstaller --noconfirm --clean --onedir --name pdf_cleaner --add-binary "bin\\gswin64c.exe;bin" --add-binary "bin\\gsdll64.dll;bin" pdf_cleaner.py
```

Resulting deliverable:

- `dist\pdf_cleaner\pdf_cleaner.exe` (plus bundled runtime files in same folder)

Bundling requirement:

- `bin\gswin64c.exe`
- `bin\gsdll64.dll`

### One-file EXE note

Optional one-file build (run only after validating one-folder build):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows_portable.ps1 -OneFile
```

`--onefile` remains less predictable for startup and native dependency loading.

### Drag-and-drop validation after packaging

1. Build using the one-folder command above.
2. Drag a PDF file or a folder of PDFs onto `dist\pdf_cleaner\pdf_cleaner.exe`.
3. Confirm outputs are written under `fixed_pdf` per source routing.
4. Confirm mixed success/failure inputs still complete with a batch summary.

## License

MIT License. See [LICENSE](LICENSE).
