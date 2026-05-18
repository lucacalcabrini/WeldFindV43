# -*- coding: utf-8 -*-
"""
WeldFind Launcher v2 — mostra TUTTI gli errori incluso stderr
"""
import sys, os, subprocess, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "weld_viewer_v43_4_3_4.pyw")

if not os.path.exists(MAIN):
    pyw_files = sorted([f for f in os.listdir(HERE) if f.endswith(".pyw") and "weld" in f.lower()])
    if pyw_files:
        MAIN = os.path.join(HERE, pyw_files[-1])
    else:
        print(f"ERRORE: nessun file .pyw trovato in {HERE}")
        input("\nPremi INVIO per chiudere...")
        sys.exit(1)

print("=" * 60)
print(f"  WeldFind Launcher v2")
print(f"  File: {os.path.basename(MAIN)}")
print(f"  Python: {sys.executable}")
print(f"  Cartella: {HERE}")
print("=" * 60)
print()
print("Avvio applicazione...\n")

try:
    result = subprocess.run(
        [sys.executable, MAIN],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'
    )

    if result.stdout:
        print("--- STDOUT ---")
        print(result.stdout)

    if result.stderr:
        print("--- STDERR / ERRORI ---")
        print(result.stderr)

    print()
    if result.returncode == 0:
        print("Applicazione chiusa normalmente.")
    else:
        print(f"TERMINATO con codice {result.returncode}")
        if not result.stderr and not result.stdout:
            print("\nNessun output — provo import diretto...\n")
            try:
                exec(open(MAIN, encoding='utf-8').read())
            except Exception:
                traceback.print_exc()

except KeyboardInterrupt:
    print("\n[interrotto]")
except Exception:
    traceback.print_exc()

print()
input("Premi INVIO per chiudere...")
