# PDFCleaner - Drag-and-Drop PDF/A Repair Tool

A simple, portable tool to clean and convert PDFs to PDF/A format using Ghostscript.

---

## ✅ Features

✅ Cleans broken or non-compliant PDFs  
✅ Converts to PDF/A using Ghostscript  
✅ Works offline - no installation needed  
✅ Supports batch drag-and-drop  
✅ Skips non-PDF files silently  
✅ Keeps PDFs in their original folders - no need to move them  

---

## 🖥 How to Use

1. **Download and unzip** the tool (maintain the folder structure):
   ```
   PDFCleaner/
   ├── pdf_cleaner.exe
   └── bin/
       ├── gswin64c.exe
       └── gsdll64.dll
   ```

2. Select one or more PDF files **from anywhere** on your system.

3. **Drag and drop** the PDFs onto `pdf_cleaner.exe`.

4. Each file will be:
   - Cleaned of extra metadata
   - Converted to PDF/A-1b format
   - Overwritten in place (original file is updated)

---

## 📦 Included

- `pdf_cleaner.exe` - the main executable
- `clean_pdf.py` - Python source code (for transparency and reuse)
- `bin/gswin64c.exe` - bundled Ghostscript engine
- `bin/gsdll64.dll` - Ghostscript dependency

---

## 🛑 If Windows Blocks the EXE

If you see "Windows protected your PC":
1. Click **More info**
2. Click **Run anyway**
3. Or: right-click the `.exe` → **Properties** → check **Unblock**

---
## 📄 License

This project is licensed under the [MIT License](LICENSE).


> Created by Ali Abul Hawa using Python, PikePDF, and Ghostscript.
