"""The PDF guide must be printed from the current player guide (python docs/make_pdf.py)."""
import importlib.util, os, pathlib
docs = pathlib.Path(os.path.dirname(os.path.abspath(__file__))).parent / "docs"
spec = importlib.util.spec_from_file_location("make_pdf", docs / "make_pdf.py")
make_pdf = importlib.util.module_from_spec(spec); spec.loader.exec_module(make_pdf)
recorded = make_pdf.STAMP.read_text(encoding="ascii").strip()
assert recorded == make_pdf.source_hash(), (
    "docs/PLAYER-GUIDE.md changed after docs/AION2-Bot-Guide.pdf was printed. Run: python docs/make_pdf.py"
)
assert make_pdf.PDF.read_bytes()[:5] == b"%PDF-" and make_pdf.PDF.stat().st_size > 10_000
print("PDF GUIDE OK")
