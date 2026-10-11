"""Turn docs/PLAYER-GUIDE.md into the HTML that docs/AION2-Bot-Guide.pdf is printed from.

    pip install markdown
    python docs/make_pdf.py docs/PLAYER-GUIDE.md guide.html
    chrome --headless=new --no-pdf-header-footer --print-to-pdf=docs/AION2-Bot-Guide.pdf guide.html
"""
import sys, markdown, pathlib
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
body = markdown.markdown(src.read_text(encoding="utf-8"), extensions=["tables", "sane_lists"])
css = """
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
out.write_text(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>AION 2 Bot Guide</title><style>{css}</style></head><body>{body}</body></html>", encoding="utf-8")
