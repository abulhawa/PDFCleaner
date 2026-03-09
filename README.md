# PDFCleaner - Structural PDF Normalization (Python)

PDFCleaner keeps Python as the batch/orchestration layer and now uses a **structural-first** repair pipeline:

1. Inspect input PDF characteristics
2. Normalize/rewrite structure with `pikepdf` (qpdf-backed)
3. Validate key output characteristics
4. Fall back to Ghostscript compatibility conversion only when required

## Why this change

The previous default path always ran Ghostscript PDF/A conversion, which can rasterize/rewrite aggressively and increase file size. The default path now avoids full-page rasterization whenever structural normalization is sufficient.

## Features

- Python drag-and-drop batch processing stays intact
- Default `auto` mode: structural-first, fallback only if needed
- `structural` mode: structural normalization only (no fallback)
- `ghostscript` mode: explicit compatibility conversion
- Per-file diagnostics:
  - mode used
  - text preserved (`yes/no`)
  - fonts present (`yes/no`)
  - output size change

## Usage

Drag one or more PDFs onto `pdf_cleaner.exe` (or run script directly).

### CLI modes

```bash
python pdf_cleaner.py --mode auto input1.pdf input2.pdf
python pdf_cleaner.py --mode structural input.pdf
python pdf_cleaner.py --mode ghostscript input.pdf
```

## Included files

- `pdf_cleaner.py` - Python source
- `pdf_cleaner.exe` - packaged executable
- `bin/gswin64c.exe` and `bin/gsdll64.dll` - Ghostscript runtime

## Tests

Run tests with your `pdfclean` conda environment:

```bash
C:\Users\ali_a\miniconda3\envs\pdfclean\python.exe -m unittest discover -s tests -v
```

## License

MIT License. See [LICENSE](LICENSE).
