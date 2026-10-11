"""Print docs/AION2-Bot-Guide.pdf from docs/PLAYER-GUIDE.md. Run after every change to the guide:

    pip install markdown
    python docs/make_pdf.py

It needs Chrome, Chromium or Edge. It also records which version of the guide the PDF was printed
from (docs/AION2-Bot-Guide.source-sha256); tests/test16.py fails while the two don't match, so a
guide change can't be merged with a stale PDF.
"""
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

DOCS = pathlib.Path(__file__).resolve().parent
SOURCE, PDF, STAMP = DOCS / "PLAYER-GUIDE.md", DOCS / "AION2-Bot-Guide.pdf", DOCS / "AION2-Bot-Guide.source-sha256"

CSS = """
@page { size: Letter; margin: 0.75in; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1c1c1c; }
h1 { font-size: 22pt; margin: 0 0 10pt; border-bottom: 2px solid #a9741a; padding-bottom: 6pt; }
h2 { font-size: 15pt; margin: 20pt 0 6pt; color: #7a520f; break-after: avoid; }
h3 { font-size: 12pt; margin: 14pt 0 4pt; break-after: avoid; }
p, li { orphans: 3; widows: 3; }
ul, ol { padding-left: 18pt; } li { margin: 3pt 0; }
code { font-family: Consolas, monospace; font-size: 9.5pt; background: #f1ede4; padding: 1px 4px; border-radius: 3px; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; font-size: 9.5pt; }
th, td { border: 1px solid #cfc7b6; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #efe6d2; } tr { break-inside: avoid; }
a { color: #7a520f; text-decoration: none; overflow-wrap: anywhere; }
"""

BROWSERS = (
    "chrome", "google-chrome", "chromium", "chromium-browser", "msedge",
    r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def source_hash() -> str:
    """Hash of the guide's text, the same on Windows and Linux checkouts."""
    return hashlib.sha256(SOURCE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    import markdown

    browser = next((b for b in BROWSERS if shutil.which(b) or pathlib.Path(b).is_file()), None)
    if not browser:
        print("Chrome, Chromium or Edge is needed to print the PDF.", file=sys.stderr)
        return 1
    body = markdown.markdown(SOURCE.read_text(encoding="utf-8"), extensions=["tables", "sane_lists"])
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>AION 2 Bot Guide</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        page, out = pathlib.Path(tmp) / "guide.html", pathlib.Path(tmp) / "guide.pdf"
        page.write_text(html, encoding="utf-8")
        subprocess.run(
            [shutil.which(browser) or browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={out}", page.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        if not out.is_file() or out.stat().st_size < 10_000:
            print("The browser did not produce a PDF.", file=sys.stderr)
            return 1
        shutil.copyfile(out, PDF)
    STAMP.write_text(source_hash() + "\n", encoding="ascii", newline="\n")
    print(f"Wrote {PDF.name} ({PDF.stat().st_size:,} bytes) from {SOURCE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
