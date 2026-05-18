"""
╔══════════════════════════════════════╗
║   PY Library Checker & Installer     ║
║   Analizza file .py e installa       ║
║   le librerie mancanti               ║
╚══════════════════════════════════════╝
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sys
import os
import re
import subprocess
import threading
import importlib.util

# ─── Mappa nome-import → nome-pip (quando diversi) ───────────────────────────
PIP_NAME_MAP = {
    "cv2":             "opencv-python",
    "PIL":             "Pillow",
    "sklearn":         "scikit-learn",
    "skimage":         "scikit-image",
    "yaml":            "PyYAML",
    "bs4":             "beautifulsoup4",
    "serial":          "pyserial",
    "usb":             "pyusb",
    "gi":              "PyGObject",
    "wx":              "wxPython",
    "snap7":           "python-snap7",
    "opcua":           "opcua",
    "asyncua":         "asyncua",
    "dotenv":          "python-dotenv",
    "dateutil":        "python-dateutil",
    "Crypto":          "pycryptodome",
    "OpenSSL":         "pyOpenSSL",
    "jwt":             "PyJWT",
    "attr":            "attrs",
    "matplotlib":      "matplotlib",
    "numpy":           "numpy",
    "pandas":          "pandas",
    "scipy":           "scipy",
    "tensorflow":      "tensorflow",
    "torch":           "torch",
    "flask":           "Flask",
    "django":          "Django",
    "fastapi":         "fastapi",
    "sqlalchemy":      "SQLAlchemy",
    "requests":        "requests",
    "aiohttp":         "aiohttp",
    "paramiko":        "paramiko",
    "cryptography":    "cryptography",
    "pydantic":        "pydantic",
    "click":           "click",
    "rich":            "rich",
    "tqdm":            "tqdm",
    "colorama":        "colorama",
    "xlrd":            "xlrd",
    "xlwt":            "xlwt",
    "openpyxl":        "openpyxl",
    "docx":            "python-docx",
    "pptx":            "python-pptx",
    "fpdf":            "fpdf2",
    "reportlab":       "reportlab",
    "PyPDF2":          "PyPDF2",
    "fitz":            "PyMuPDF",
    "pyinstaller":     "pyinstaller",
    "cx_Freeze":       "cx_Freeze",
    "pyautogui":       "pyautogui",
    "pynput":          "pynput",
    "psutil":          "psutil",
    "pymongo":         "pymongo",
    "redis":           "redis",
    "boto3":           "boto3",
    "google":          "google-cloud",
    "azure":           "azure",
    "plotly":          "plotly",
    "seaborn":         "seaborn",
    "altair":          "altair",
    "bokeh":           "bokeh",
    "dash":            "dash",
    "streamlit":       "streamlit",
    "gradio":          "gradio",
    "transformers":    "transformers",
    "langchain":       "langchain",
    "openai":          "openai",
    "anthropic":       "anthropic",
}

# Moduli standard Python (non installabili via pip)
STDLIB = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else {
    "abc","ast","asyncio","base64","binascii","builtins","cgi","cmd","code",
    "codecs","collections","concurrent","configparser","contextlib","copy",
    "csv","ctypes","dataclasses","datetime","decimal","difflib","email",
    "enum","fileinput","fnmatch","fractions","ftplib","functools","gc",
    "getopt","getpass","glob","gzip","hashlib","heapq","hmac","html",
    "http","imaplib","inspect","io","ipaddress","itertools","json","keyword",
    "linecache","locale","logging","lzma","math","mimetypes","multiprocessing",
    "netrc","numbers","operator","os","pathlib","pickle","platform","pprint",
    "profile","queue","random","re","select","shelve","shlex","shutil",
    "signal","smtplib","socket","socketserver","sqlite3","ssl","stat",
    "statistics","string","struct","subprocess","sys","tarfile","telnetlib",
    "tempfile","textwrap","threading","time","timeit","tkinter","token",
    "tokenize","traceback","types","typing","unicodedata","unittest","urllib",
    "uuid","venv","warnings","weakref","webbrowser","xml","xmlrpc","zipfile",
    "zipimport","zlib","_thread","__future__","abc","atexit","bisect",
    "calendar","cmath","compileall","contextlib","copy","copyreg","cProfile",
    "dis","doctest","encodings","errno","faulthandler","fcntl","formatter",
    "gettext","grp","imghdr","importlib","marshal","mmap","modulefinder",
    "msvcrt","nt","ntpath","opcode","optparse","parser","pdb","pickletools",
    "pipes","pkgutil","poplib","posix","posixpath","pstats","pty","pwd",
    "pyclbr","pydoc","quopri","readline","reprlib","resource","rlcompleter",
    "runpy","sched","secrets","select","selectors","shelve","site","sndhdr",
    "spwd","sre_compile","sre_constants","sre_parse","stringprep","sunau",
    "symtable","sysconfig","syslog","tabnanny","termios","test","trace",
    "tracemalloc","tty","turtle","turtledemo","uu","wave","winreg","winsound",
    "wsgiref","xdrlib","zipapp","zoneinfo","struct","array","audioop",
    "chunk","crypt","curses","dbm","dummy_threading","formatter","imghdr",
    "mailbox","mailcap","msilib","nis","nntplib","ossaudiodev","parser",
    "pipes","readline","sndhdr","spwd","sunau","telnetlib","uu","xdrlib",
}

# Sottomoduli e nomi noti che fanno parte di pacchetti già in STDLIB o built-in
# e che NON vanno cercati su pip
STDLIB_EXTRA = {
    # tkinter e suoi sottomoduli
    "ttk", "filedialog", "messagebox", "simpledialog", "colorchooser",
    "scrolledtext", "font", "commondialog", "dialog", "dnd",
    # altri sottomoduli comuni
    "pyplot", "backends", "figure", "patches", "lines", "axes",
    "path", "pathlib", "typing_extensions",
}


# ─── Parsing import da file .py ──────────────────────────────────────────────

def extract_imports(filepath):
    """Estrae tutti i moduli top-level importati da un file .py."""
    imports = set()
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return imports

    # Pattern 1: "import X" o "import X.Y.Z" → prende solo X
    for match in re.finditer(r'^\s*import\s+([\w][\w.]*)', content, re.MULTILINE):
        top = match.group(1).split(".")[0]
        imports.add(top)

    # Pattern 2: "from X import ..." → prende solo X (il modulo, NON le classi dopo import)
    # Es: "from tkinter import ttk"  → aggiunge "tkinter", NON "ttk"
    # Es: "from matplotlib.backends..." → aggiunge "matplotlib"
    for match in re.finditer(r'^\s*from\s+([\w][\w.]*)\s+import', content, re.MULTILINE):
        top = match.group(1).split(".")[0]
        imports.add(top)

    # Rimuove nomi che sono chiaramente classi o costanti (iniziano con maiuscola)
    imports = {m for m in imports if m and m[0].islower()}

    return imports


def classify_imports(imports):
    """Classifica import: stdlib / installato / mancante."""
    stdlib = []
    installed = []
    missing = []

    for mod in sorted(imports):
        if mod in STDLIB or mod in STDLIB_EXTRA or mod.startswith("_"):
            stdlib.append(mod)
            continue
        spec = importlib.util.find_spec(mod)
        if spec is not None:
            installed.append(mod)
        else:
            missing.append(mod)

    return stdlib, installed, missing


def pip_name(mod):
    return PIP_NAME_MAP.get(mod, mod)


# ─── GUI ─────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    # Palette
    BG      = "#0f1117"
    PANEL   = "#1a1d27"
    BORDER  = "#2a2d3e"
    ACCENT  = "#4f8ef7"
    GREEN   = "#3dd68c"
    YELLOW  = "#f7c94f"
    RED     = "#f75f5f"
    GRAY    = "#6b7280"
    FG      = "#e2e8f0"
    FG2     = "#94a3b8"
    MONO    = ("Consolas", 9)
    FONT    = ("Segoe UI", 10)
    FONTB   = ("Segoe UI", 10, "bold")
    FONTS   = ("Segoe UI", 9)

    def __init__(self):
        super().__init__()
        self.title("PY Library Checker")
        self.geometry("820x680")
        self.minsize(700, 500)
        self.configure(bg=self.BG)
        self.resizable(True, True)

        self.files = []          # lista path file analizzati
        self.results = {}        # {filepath: {stdlib, installed, missing}}
        self.check_vars = {}     # {mod: BooleanVar} per checkbox
        self.check_canvases = {} # {mod: (canvas, var, row_bg)}
        self._install_thread = None

        self._build_ui()

    def _info_cell(self, parent, label, value, color, row, col, colspan=1):
        """Cella etichetta + valore per il pannello info Python."""
        cell = tk.Frame(parent, bg=self.PANEL)
        cell.grid(row=row, column=col, columnspan=colspan,
                  sticky="w", padx=(0, 24), pady=2)
        tk.Label(cell, text=label + ":",
                 font=self.FONTS, bg=self.PANEL, fg=self.FG2).pack(side="left")
        tk.Label(cell, text="  " + value,
                 font=("Consolas", 9), bg=self.PANEL, fg=color).pack(side="left")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=self.ACCENT, height=3)
        hdr.pack(fill="x")

        top = tk.Frame(self, bg=self.BG, pady=16, padx=20)
        top.pack(fill="x")

        tk.Label(top, text="⬡  PY Library Checker",
                 font=("Segoe UI", 16, "bold"),
                 bg=self.BG, fg=self.FG).pack(side="left")

        tk.Label(top, text="analizza · verifica · installa",
                 font=("Segoe UI", 10),
                 bg=self.BG, fg=self.FG2).pack(side="left", padx=12, pady=2)

        # ── Pannello info ambiente Python ─────────────────────────────────────
        py_panel = tk.Frame(self, bg=self.PANEL, padx=20, pady=10,
                            highlightbackground=self.BORDER, highlightthickness=1)
        py_panel.pack(fill="x", padx=20, pady=(0, 8))

        # Titolo riga
        tk.Label(py_panel, text="🐍  Ambiente Python attivo",
                 font=self.FONTB, bg=self.PANEL, fg=self.FG).grid(
                 row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        # Versione
        ver = sys.version_info
        ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
        ver_color = self.GREEN if ver.major == 3 else self.RED
        self._info_cell(py_panel, "Versione",  ver_str,          ver_color, row=1, col=0)

        # Architettura
        import platform
        arch = platform.architecture()[0]
        self._info_cell(py_panel, "Architettura", arch,          self.FG,   row=1, col=1)

        # Percorso eseguibile
        exe = sys.executable
        self._info_cell(py_panel, "Eseguibile", exe,             self.YELLOW, row=2, col=0, colspan=3)

        # Cartella site-packages
        import site
        try:
            sp = site.getsitepackages()[0]
        except Exception:
            sp = site.getusersitepackages()
        self._info_cell(py_panel, "Librerie installate in", sp,  self.ACCENT, row=3, col=0, colspan=3)

        # Avviso se pythonw
        if "pythonw" in exe.lower():
            nota = "✅  Avviato con pythonw.exe (nessuna finestra CMD)"
            nc = self.GREEN
        else:
            nota = "⚠  Avviato con python.exe — usa il file .pyw per evitare la finestra CMD"
            nc = self.YELLOW
        tk.Label(py_panel, text=nota, font=self.FONTS,
                 bg=self.PANEL, fg=nc).grid(row=4, column=0, columnspan=4,
                 sticky="w", pady=(6, 0))

        # Drop zone / pulsanti
        dz = tk.Frame(self, bg=self.PANEL, padx=20, pady=14,
                      highlightbackground=self.BORDER, highlightthickness=1)
        dz.pack(fill="x", padx=20, pady=(0, 10))

        tk.Label(dz, text="File .py da analizzare",
                 font=self.FONTB, bg=self.PANEL, fg=self.FG).grid(row=0, column=0, sticky="w")

        btn_frame = tk.Frame(dz, bg=self.PANEL)
        btn_frame.grid(row=0, column=1, sticky="e", padx=(0,0))
        dz.columnconfigure(1, weight=1)

        self._btn(btn_frame, "＋  Aggiungi file", self.add_files,
                  self.ACCENT, "#1a1d27").pack(side="left", padx=(0,8))
        self._btn(btn_frame, "✕  Rimuovi selezionato", self.remove_file,
                  self.BORDER, self.FG2).pack(side="left", padx=(0,8))
        self._btn(btn_frame, "↺  Pulisci tutto", self.clear_all,
                  self.BORDER, self.FG2).pack(side="left")

        # Lista file
        fl = tk.Frame(dz, bg=self.BG, height=80,
                      highlightbackground=self.BORDER, highlightthickness=1)
        fl.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10,0))
        dz.rowconfigure(1, weight=1)

        self.file_list = tk.Listbox(fl, bg=self.BG, fg=self.FG,
                                    font=self.MONO, selectbackground=self.ACCENT,
                                    selectforeground="#fff", relief="flat",
                                    activestyle="none", height=4,
                                    highlightthickness=0, borderwidth=0)
        sb = tk.Scrollbar(fl, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.file_list.pack(fill="both", expand=True, padx=6, pady=4)

        # Pulsante analizza
        analyze_bar = tk.Frame(self, bg=self.BG)
        analyze_bar.pack(fill="x", padx=20, pady=(0,10))
        self._btn(analyze_bar, "🔍  Analizza",
                  self.analyze, self.GREEN, "#0f1117",
                  font=("Segoe UI", 11, "bold"), padx=30, pady=8).pack(side="left")
        self.status_lbl = tk.Label(analyze_bar, text="",
                                   font=self.FONTS, bg=self.BG, fg=self.FG2)
        self.status_lbl.pack(side="left", padx=16)

        # Notebook risultati
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Dark.TNotebook", background=self.BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab",
                        background=self.PANEL, foreground=self.FG2,
                        font=self.FONT, padding=[14, 6],
                        borderwidth=0)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", self.ACCENT)],
                  foreground=[("selected", "#fff")])

        nb = ttk.Notebook(self, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True, padx=20, pady=(0,10))

        self.tab_missing  = self._make_tab(nb, "❌  Mancanti")
        self.tab_install  = self._make_tab(nb, "✅  Installate")
        self.tab_stdlib   = self._make_tab(nb, "📦  Standard lib")
        self.tab_log      = self._make_tab(nb, "📋  Log installazione")

        nb.add(self.tab_missing,  text="❌  Mancanti")
        nb.add(self.tab_install,  text="✅  Installate")
        nb.add(self.tab_stdlib,   text="📦  Standard lib")
        nb.add(self.tab_log,      text="📋  Log")

        self._build_missing_tab()
        self._build_list_tab(self.tab_install,  "installed")
        self._build_list_tab(self.tab_stdlib,   "stdlib")
        self._build_log_tab()

    def _make_tab(self, nb, text):
        f = tk.Frame(nb, bg=self.PANEL)
        return f

    def _btn(self, parent, text, cmd, bg, fg,
             font=None, padx=14, pady=5):
        font = font or self.FONTS
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg, fg=fg, font=font,
                      relief="flat", cursor="hand2",
                      padx=padx, pady=pady,
                      activebackground=bg, activeforeground=fg,
                      borderwidth=0)
        b.bind("<Enter>", lambda e: b.configure(bg=self._lighten(bg)))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def _lighten(self, hex_color):
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            r = min(255, r + 25)
            g = min(255, g + 25)
            b = min(255, b + 25)
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return hex_color

    # ── Tab Mancanti ──────────────────────────────────────────────────────────

    def _build_missing_tab(self):
        t = self.tab_missing
        for w in t.winfo_children():
            w.destroy()

        # Toolbar
        bar = tk.Frame(t, bg=self.PANEL, pady=8, padx=12)
        bar.pack(fill="x")

        self._btn(bar, "☑  Seleziona tutto",
                  self._select_all_missing, self.BORDER, self.FG2).pack(side="left", padx=(0,6))
        self._btn(bar, "☐  Deseleziona tutto",
                  self._deselect_all_missing, self.BORDER, self.FG2).pack(side="left", padx=(0,16))

        self.install_btn = self._btn(bar, "⬇  Installa selezionate",
                                     self._install_selected,
                                     self.ACCENT, "#fff",
                                     font=("Segoe UI", 10, "bold"),
                                     padx=18)
        self.install_btn.pack(side="left")

        # Scroll area
        outer = tk.Frame(t, bg=self.PANEL)
        outer.pack(fill="both", expand=True, padx=12, pady=(0,12))

        canvas = tk.Canvas(outer, bg=self.PANEL,
                           highlightthickness=0, borderwidth=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.missing_frame = tk.Frame(canvas, bg=self.PANEL)
        win = canvas.create_window((0, 0), window=self.missing_frame, anchor="nw")

        def on_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(win, width=canvas.winfo_width())
        self.missing_frame.bind("<Configure>", on_configure)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win, width=e.width))

        # Mousewheel
        def _scroll(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _scroll)

        self._missing_canvas = canvas
        self._populate_missing()

    def _draw_check(self, canvas, var, row_bg):
        """Disegna il toggle checkbox su canvas in base al valore di var."""
        canvas.delete("all")
        if var.get():
            canvas.create_rectangle(2, 2, 20, 20,
                fill=self.ACCENT, outline=self.ACCENT, width=0)
            canvas.create_text(11, 11, text="✓",
                fill="#fff", font=("Segoe UI", 10, "bold"))
        else:
            canvas.create_rectangle(2, 2, 20, 20,
                fill=row_bg, outline=self.GRAY, width=2)

    def _redraw_all_checks(self):
        """Ridisegna tutti i canvas senza ricreare i widget."""
        for mod, (canvas, var, row_bg) in self.check_canvases.items():
            self._draw_check(canvas, var, row_bg)

    def _populate_missing(self):
        for w in self.missing_frame.winfo_children():
            w.destroy()
        self.check_vars.clear()
        self.check_canvases = {}  # mod -> (canvas, var, row_bg)

        # Raccoglie tutti i mancanti da tutti i file
        all_missing = {}  # mod -> set di file che lo richiedono
        for fp, res in self.results.items():
            fname = os.path.basename(fp)
            for mod in res.get("missing", []):
                all_missing.setdefault(mod, set()).add(fname)

        if not all_missing:
            tk.Label(self.missing_frame,
                     text="✅  Nessuna libreria mancante rilevata.",
                     font=("Segoe UI", 12), bg=self.PANEL, fg=self.GREEN,
                     pady=30).pack()
            return

        # Header colonne
        hdr = tk.Frame(self.missing_frame, bg=self.BORDER, pady=4)
        hdr.pack(fill="x", pady=(0, 2))
        tk.Label(hdr, text="  ", bg=self.BORDER, width=3).pack(side="left")
        tk.Label(hdr, text="Modulo import",
                 font=self.FONTB, bg=self.BORDER, fg=self.FG2,
                 width=22, anchor="w").pack(side="left")
        tk.Label(hdr, text="Nome pip",
                 font=self.FONTB, bg=self.BORDER, fg=self.FG2,
                 width=22, anchor="w").pack(side="left")
        tk.Label(hdr, text="Richiesto da",
                 font=self.FONTB, bg=self.BORDER, fg=self.FG2,
                 anchor="w").pack(side="left", fill="x", expand=True)

        for i, mod in enumerate(sorted(all_missing)):
            row_bg = self.PANEL if i % 2 == 0 else "#1e2130"
            row = tk.Frame(self.missing_frame, bg=row_bg, pady=6)
            row.pack(fill="x")

            var = tk.BooleanVar(value=True)
            self.check_vars[mod] = var

            # Toggle custom su Canvas
            cv = tk.Canvas(row, width=22, height=22, bg=row_bg,
                           highlightthickness=0, bd=0, cursor="hand2")
            cv.pack(side="left", padx=(8, 4))

            # Salva riferimento canvas per poterlo ridisegnare dopo
            self.check_canvases[mod] = (cv, var, row_bg)

            def _toggle(event, canvas=cv, v=var, bg=row_bg):
                v.set(not v.get())
                self._draw_check(canvas, v, bg)

            cv.bind("<Button-1>", _toggle)
            self._draw_check(cv, var, row_bg)

            tk.Label(row, text=mod,
                     font=("Consolas", 10), bg=row_bg, fg=self.YELLOW,
                     width=22, anchor="w").pack(side="left")

            pk = pip_name(mod)
            pk_color = self.ACCENT if pk != mod else self.FG2
            tk.Label(row, text=pk,
                     font=("Consolas", 10), bg=row_bg, fg=pk_color,
                     width=22, anchor="w").pack(side="left")

            files_str = ", ".join(sorted(all_missing[mod]))
            lbl = tk.Label(row, text=files_str,
                     font=self.FONTS, bg=row_bg, fg=self.FG2,
                     anchor="w", wraplength=300, justify="left")
            lbl.pack(side="left", fill="x", expand=True, padx=(0, 8))

            # Clic su tutta la riga fa toggle
            for widget in (row, lbl):
                widget.bind("<Button-1>", _toggle)

    # ── Tab Installate / Stdlib ───────────────────────────────────────────────

    def _build_list_tab(self, tab, key):
        for w in tab.winfo_children():
            w.destroy()

        outer = tk.Frame(tab, bg=self.PANEL)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        txt = tk.Text(outer, bg=self.BG, fg=self.FG,
                      font=self.MONO, relief="flat",
                      state="disabled", wrap="word",
                      highlightthickness=0, borderwidth=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        if key == "installed":
            self._txt_installed = txt
        else:
            self._txt_stdlib = txt

    def _update_list_tab(self, txt_widget, key):
        items = set()
        for res in self.results.values():
            items.update(res.get(key, []))

        txt_widget.configure(state="normal")
        txt_widget.delete("1.0", "end")
        if items:
            txt_widget.insert("end", "  ".join(sorted(items)))
        else:
            txt_widget.insert("end", "(nessuno)")
        txt_widget.configure(state="disabled")

    # ── Tab Log ───────────────────────────────────────────────────────────────

    def _build_log_tab(self):
        outer = tk.Frame(self.tab_log, bg=self.PANEL)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        # Toolbar con pulsante copia
        bar = tk.Frame(outer, bg=self.PANEL)
        bar.pack(fill="x", pady=(0, 6))
        self._btn(bar, "📋  Copia log", self._copia_log,
                  self.BORDER, self.FG2).pack(side="left")
        self._btn(bar, "🗑  Pulisci log", self._pulisci_log,
                  self.BORDER, self.FG2).pack(side="left", padx=(6, 0))

        self.log_txt = tk.Text(outer, bg=self.BG, fg=self.FG,
                               font=("Consolas", 9), relief="flat",
                               state="disabled", wrap="word",
                               highlightthickness=0, borderwidth=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True)

        self.log_txt.tag_configure("ok",   foreground=self.GREEN)
        self.log_txt.tag_configure("err",  foreground=self.RED)
        self.log_txt.tag_configure("info", foreground=self.ACCENT)
        self.log_txt.tag_configure("warn", foreground=self.YELLOW)

    def _copia_log(self):
        testo = self.log_txt.get("1.0", "end").strip()
        if testo:
            self.clipboard_clear()
            self.clipboard_append(testo)
            messagebox.showinfo("Log copiato", "Il log è stato copiato negli appunti.")
        else:
            messagebox.showinfo("Log vuoto", "Non c'è nulla da copiare.")

    def _pulisci_log(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    def _log(self, msg, tag="info"):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", msg + "\n", tag)
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # ── Azioni ────────────────────────────────────────────────────────────────

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleziona file Python",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")]
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.insert("end", p)

    def remove_file(self):
        sel = self.file_list.curselection()
        if sel:
            idx = sel[0]
            fp = self.files[idx]
            self.files.pop(idx)
            self.file_list.delete(idx)
            self.results.pop(fp, None)

    def clear_all(self):
        self.files.clear()
        self.results.clear()
        self.file_list.delete(0, "end")
        self._refresh_ui()

    def analyze(self):
        if not self.files:
            messagebox.showinfo("Nessun file", "Aggiungi almeno un file .py da analizzare.")
            return

        self.results.clear()
        total_missing = 0

        for fp in self.files:
            imports = extract_imports(fp)
            stdlib, installed, missing = classify_imports(imports)
            self.results[fp] = {
                "stdlib":    stdlib,
                "installed": installed,
                "missing":   missing,
            }
            total_missing += len(missing)

        self._refresh_ui()

        if total_missing == 0:
            self.status_lbl.configure(
                text=f"✅  Analisi completata — tutto OK su {len(self.files)} file",
                fg=self.GREEN)
        else:
            self.status_lbl.configure(
                text=f"⚠  {total_missing} librerie mancanti trovate",
                fg=self.YELLOW)

    def _refresh_ui(self):
        self._populate_missing()
        self._update_list_tab(self._txt_installed, "installed")
        self._update_list_tab(self._txt_stdlib,    "stdlib")

    def _select_all_missing(self):
        for var in self.check_vars.values():
            var.set(True)
        self._redraw_all_checks()

    def _deselect_all_missing(self):
        for var in self.check_vars.values():
            var.set(False)
        self._redraw_all_checks()

    def _install_selected(self):
        to_install = [pip_name(mod)
                      for mod, var in self.check_vars.items()
                      if var.get()]
        if not to_install:
            messagebox.showinfo("Nessuna selezione",
                                "Seleziona almeno una libreria da installare.")
            return

        if self._install_thread and self._install_thread.is_alive():
            messagebox.showwarning("In corso",
                                   "Installazione già in corso, attendi.")
            return

        self.install_btn.configure(state="disabled",
                                   text="⏳  Installazione in corso…")
        self._install_thread = threading.Thread(
            target=self._run_install, args=(to_install,), daemon=True)
        self._install_thread.start()

    def _traduci_errore(self, testo):
        """Traduce i messaggi di errore pip dall'inglese all'italiano.
        ATTENZIONE: usare solo su testo di log, MAI sui nomi dei pacchetti."""
        traduzioni = [
            ("Could not find a version that satisfies the requirement",
             "Nessuna versione trovata per il pacchetto"),
            ("No matching distribution found for",
             "Nessuna distribuzione compatibile trovata per"),
            ("No matching distribution found",
             "Nessuna distribuzione compatibile trovata"),
            ("No module named",
             "Modulo non trovato"),
            ("Connection error",
             "Errore di connessione — verifica la rete"),
            ("Network is unreachable",
             "Rete non raggiungibile — verifica la connessione"),
            ("timed out",
             "Connessione scaduta — verifica la rete"),
            ("Permission denied",
             "Permesso negato — prova ad eseguire come amministratore"),
            ("Requirement already satisfied",
             "Requirement già installato, nessuna azione necessaria"),
            ("Successfully installed",
             "Installato correttamente"),
            ("already satisfied",
             "già installato, nessuna azione necessaria"),
            ("ERROR:",   "ERRORE:"),
            ("WARNING:", "ATTENZIONE:"),
            ("error:",   "errore:"),
            ("warning:", "attenzione:"),
            # NOTA: NON tradurre parole generiche come "failed", "invalid",
            # "not found", "denied" — potrebbero comparire nei nomi pacchetti
        ]
        for eng, ita in traduzioni:
            testo = testo.replace(eng, ita)
        return testo

    def _run_install(self, packages):
        self._log(f"\n{'─'*50}", "info")
        self._log(f"▶  Installazione di {len(packages)} librer{'ia' if len(packages)==1 else 'ie'}…", "info")
        self._log(f"{'─'*50}", "info")

        ok_count = 0
        fail_count = 0

        for pkg in packages:
            self._log(f"\n📦  Installazione: {pkg}", "warn")
            try:
                # Nasconde la finestra CMD su Windows
                startupinfo = None
                if os.name == "nt":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = subprocess.SW_HIDE

                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg,
                     "--no-warn-script-location"],
                    capture_output=True, text=True, timeout=120,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                )

                # Mostra sempre stdout (output pip completo)
                if result.stdout.strip():
                    for riga in result.stdout.strip().splitlines():
                        riga = riga.strip()
                        if riga:
                            self._log(f"  {self._traduci_errore(riga)}", "info")

                if result.returncode == 0:
                    self._log(f"  ✅  {pkg} installato con successo", "ok")
                    ok_count += 1
                else:
                    self._log(f"  ❌  Installazione fallita (codice errore: {result.returncode})", "err")
                    # Mostra stderr completo grezzo per diagnostica
                    if result.stderr.strip():
                        self._log("  ── Dettaglio errore ──", "warn")
                        for riga in result.stderr.strip().splitlines():
                            riga = riga.strip()
                            if riga:
                                self._log(f"  {self._traduci_errore(riga)}", "err")
                    fail_count += 1
            except subprocess.TimeoutExpired:
                self._log(f"  ❌  Tempo scaduto durante l'installazione di {pkg} — verifica la connessione", "err")
                fail_count += 1
            except Exception as e:
                self._log(f"  ❌  Errore imprevisto: {self._traduci_errore(str(e))}", "err")
                fail_count += 1

        self._log(f"\n{'─'*50}", "info")
        if fail_count == 0:
            self._log(f"✔  Tutto completato: {ok_count} librer{'ia installata' if ok_count==1 else 'ie installate'} con successo", "ok")
        else:
            self._log(f"✔  Completato: {ok_count} riuscite,  {fail_count} fallite", "info")
        self._log(f"{'─'*50}\n", "info")

        # Ri-analizza per aggiornare lo stato
        self.after(200, self._post_install)

    def _post_install(self):
        self.install_btn.configure(state="normal",
                                   text="⬇  Installa selezionate")
        # Ri-analizza automaticamente
        if self.files:
            self.analyze()


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
