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

## Packaging a Portable Standalone Windows EXE

Recommended build approach: **PyInstaller one-folder (`--onedir`)**

Reason:

- More reliable for native dependencies (`pikepdf`/qpdf libs)
- Faster startup than one-file extraction
- Easier to bundle Ghostscript binaries (`bin/gswin64c.exe`, `bin/gsdll64.dll`)
- Better behavior for a drag-and-drop utility distributed as a portable folder

### Build command (example)

From repository root:

```bash
pyinstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --name pdf_cleaner ^
  --add-binary "bin\\gswin64c.exe;bin" ^
  --add-binary "bin\\gsdll64.dll;bin" ^
  pdf_cleaner.py
```

Resulting deliverable:

- `dist\pdf_cleaner\pdf_cleaner.exe` (plus bundled runtime files in same folder)

### One-file EXE note

`--onefile` is possible, but not preferred for this tool because startup extraction overhead and native dependency loading can be less predictable for high-volume drag-and-drop usage.

## License

MIT License. See [LICENSE](LICENSE).
