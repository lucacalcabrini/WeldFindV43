"""
Legge manual.html, lo converte in base64 e aggiorna _MANUAL_B64 nel .pyw.
Usato dal workflow GitHub Actions prima di compilare l'exe.
"""
import base64, re, sys
from pathlib import Path

PYW = Path("weld_viewer.pyw")
HTML = Path("manual.html")

html_b64 = base64.b64encode(HTML.read_bytes()).decode("ascii")

src = PYW.read_text(encoding="utf-8")
new_src = re.sub(
    r'(_MANUAL_B64\s*=\s*b?")[A-Za-z0-9+/=]+"',
    f'\\g<1>{html_b64}"',
    src,
)

if new_src == src:
    print("ERRORE: _MANUAL_B64 non trovato nel file .pyw")
    sys.exit(1)

PYW.write_text(new_src, encoding="utf-8")
print(f"OK: _MANUAL_B64 aggiornato ({len(html_b64)} chars base64)")
