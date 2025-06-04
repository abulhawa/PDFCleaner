import sys
import subprocess
import os
import platform
import shutil
from pikepdf import Pdf, Dictionary


def clean_pdf(input_path, gs_exe):
    base_name = os.path.basename(input_path)
    print(f"📄 Processing: {base_name}")

    if not os.path.isfile(input_path) or not input_path.lower().endswith('.pdf'):
        return  # silently skip non-PDFs

    temp_cleaned_path = input_path + ".temp_cleaned.pdf"
    output_pdfa_path = input_path + ".temp_pdfa.pdf"

    try:
        # Step 1: Sanitize PDF with pikepdf
        with Pdf.open(input_path) as pdf:
            pdf.docinfo = pdf.make_indirect(Dictionary({
                '/Title': '',
                '/Author': '',
                '/Subject': '',
                '/Creator': '',
                '/Producer': '',
                '/Keywords': ''
            }))
            pdf.remove_unreferenced_resources()
            pdf.save(temp_cleaned_path)

        # Step 2: Ghostscript PDF/A conversion
        gs_command = [
            gs_exe,
            "-dPDFA=1",
            "-dBATCH",
            "-dNOPAUSE",
            "-dNOOUTERSAVE",
            "-sProcessColorModel=DeviceRGB",
            "-sDEVICE=pdfwrite",
            "-sPDFACompatibilityPolicy=1",
            f"-sOutputFile={output_pdfa_path}",
            temp_cleaned_path
        ]

        result = subprocess.run(gs_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            print(f"❌ Ghostscript failed on: {base_name}")
            print(result.stderr)
            input("🔴 Press Enter to exit...")
            return

        if not os.path.exists(output_pdfa_path) or os.path.getsize(output_pdfa_path) < 100:
            print(f"❌ Failed to generate PDF/A for: {base_name}")
            input("🔴 Press Enter to exit...")
            return

        os.replace(output_pdfa_path, input_path)
        print(f"✅ Fixed: {base_name}")

    except Exception as e:
        print(f"❌ Error processing {base_name}: {e}")
        input("🔴 Press Enter to exit...")
    finally:
        for f in [temp_cleaned_path, output_pdfa_path]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("ℹ️ Drag one or more PDF files onto this script or EXE to fix them.")
        sys.exit(0)

    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    local_gs_path = os.path.join(script_dir, "bin", "gswin64c.exe")
    gs_exe = local_gs_path if os.path.exists(local_gs_path) else "gswin64c"

    for input_file in sys.argv[1:]:
        if input_file.lower().endswith('.pdf'):
            clean_pdf(input_file, gs_exe)

    print("\n🔹 All done.")
