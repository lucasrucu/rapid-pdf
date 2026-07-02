Bundled Tesseract language data for the "Enhance for Search (OCR)" feature.

File:    eng.traineddata (English, fast variant)
Source:  https://github.com/tesseract-ocr/tessdata_fast (main branch)
SHA256:  7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2
License: Apache License 2.0 (see the tessdata_fast repository)
Size:    ~4.0 MB

Why it is here: PyMuPDF's OCR engine (Tesseract) is compiled into the
PyMuPDF extension itself, so no tesseract.exe is needed at runtime. The
LANGUAGE DATA is not embedded though; without this file, OCR only works on
machines that happen to have Tesseract-OCR installed. core/pdf_document.py
passes this folder to pdfocr_tobytes(tessdata=...) so OCR works on any
computer the app is installed on. A user-set TESSDATA_PREFIX environment
variable still takes precedence (e.g. for extra languages).

Packaging: this folder ships automatically. rapid-pdf.spec bundles the whole
assets/ tree (datas=[("assets", "assets")]) and rapid-pdf.iss copies the
whole PyInstaller dist folder recursively.
