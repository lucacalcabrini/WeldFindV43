# -*- coding: utf-8 -*-
"""
WeldDetector DB Analyzer  v4.3.8
Analizzatore per file .db TIA Portal + simulatore offline algoritmo PLC.

v4.3.6 (2026-03-18):
  - NUOVO: Tab OPC UA per lettura DB WeldFind via OPC UA integrato S7-1500
  - Browse automatico: scoperta DB WeldFind tramite variabili "firma"
  - Flag abilitazione OPC UA con pagina dedicata impostazioni comunicazione
  - Selezione DB da lista, lettura e caricamento diretto nel viewer
  - Supporto autenticazione user/password OPC UA
  - Compatibile con librerie python-opcua e asyncua

v4.3.5 (2026-03-18):
  - Integrato PLC DB Reader: tab dedicata per lettura diretta da PLC via Snap7
  - Connessione S7-1500/1200, lettura DB istanza, decodifica e caricamento nel viewer
  - Supporto array [0..2000] (v4.3 attuale) e [0..5000] (legacy)
  - Bottone "Carica nel Viewer" per analisi immediata senza salvare file

v4.3.4 (2026-03-18):
  - Sezione Baseline sinistra: read-only, auto-popolata dal DB (no edit)
  - Caricamento file riapre sempre tab Segnale & Baseline
  - Tab Simulatore nasconde pannello sinistro globale (mostra solo colonna sim)

v4.3.3 (2026-03-16):
  - Rimossa normalizzazione angoli: quota reale encoder preservata
    (coerente con SCL v4.3.2 - angoli negativi CCW restano negativi)
  - Simulatore, grafici e compute_adaptive_baseline usano angoli grezzi

v4.3.2 (2026-03-16):
  - Fix confronto parametri: '2' == '2.0' ora riconosciuto come uguale
    (confronto numerico invece che stringa in logica automatica DB)
  - Evita switch involontario da Soglie=DB a Soglie=CALC

v4.3.1 (2026-03-16):
  - Supporto rotazione CCW (angoli negativi/decrescenti)
  - Normalizzazione angoli a [0,360) in tutti i percorsi dati
  - Finestra baseline bidirezionale (ABS + wrap 180)
  - Viaggio angolare totale (total_travel) come rTotalTravel SCL
  - Tab grafici con limiti assi adattivi
  - compute_adaptive_baseline fixato per CCW

v4.3.0:
  - Supporto picchi POSITIVI / NEGATIVI / ENTRAMBI (I_PeakPolarity)
  - Logica automatica Filtro=DB / Soglie=DB
  - use_float32 per replica aritmetica REAL PLC
  - use_db_thresholds per soglie precalcolate
  - Preload parametri + pulsante Ripristina Default DB
  - Banner con modalita [Filtro=DB/Soglie=DB] + MATCH PLC
  - Fix I_AxisTandSteel forzato False al preload
  - Fix int("5.0") per min_cons/max_cons

Requisiti: pip install matplotlib numpy
Opzionale: pip install python-snap7  (per tab PLC Reader)
Opzionale: pip install opcua  (oppure asyncua, per tab OPC UA)
Build EXE: pyinstaller --onefile --windowed weld_viewer.py
"""

# ── VERSIONE ──────────────────────────────────────────────
# v4.3.12 (2026-03-25):
#   - Grid search: architettura initializer-cached (44x speedup)
#   - Worker carica file UNA SOLA VOLTA via ProcessPoolExecutor initializer
#   - IPC ridotto a (fi, combo_tuple) ~55 bytes — zero I/O disco durante simulazioni
#   - _stat_sim_worker_v2: nessun parse dentro il loop, solo simulate()
# v4.3.10 (2026-03-25):
#   - simulate_plc_realtime: look-back vettorizzato numpy (24x speedup)
#   - Worker processi: priorità BELOW_NORMAL (CPU non più al 100%)
#   - Strumento Separatore File integrato nel tab Statistiche
# v4.3.9 (2026-03-25):
#   - Statistiche: grid-search parallelo ProcessPoolExecutor non bloccante
#   - Worker top-level picklable per multiprocessing
#   - UI completamente reattiva durante ricerca
# v4.3.8 (2026-03-25):
#   - Ottimizzazione _rt_poll: pre-filtraggio scalari + costante tipo_size
#   - Lazy draw in _recompute: ridisegna solo il tab analisi attivo
#   - canvas.draw() → draw_idle() in tutti i _draw_* passivi
#   - parse_db_file unificato (rimossa duplicazione 62%)
#   - import itertools, matplotlib.colors spostati a top-level
#   - _plc_log_msg: rimosso update_idletasks() per-riga (overhead UI)
#   - _rt_poll: hasattr() → attributo inizializzato in _rt_start
APP_VERSION = "5.0.00"
APP_BUILD   = "2026-03-26"
APP_RELEASE = f"v{APP_VERSION} build {APP_BUILD}"

# ── Nascondi console CMD su Windows ──────────────────────
import sys
if sys.platform == "win32":
    try:
        import ctypes
        # Nascondi la finestra console
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        # Sgancia completamente il processo dalla console
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass  # pythonw.exe o pyinstaller --windowed

# ── Pulizia processi: uccide tutto il sottoalbero alla chiusura ──
import os, signal, atexit

_MAIN_PID = os.getpid()  # PID main process; worker hanno pid diverso

def _kill_process_tree():
    # Esegui solo nel processo principale, non nei worker
    if os.getpid() != _MAIN_PID:
        return
    try:
        if sys.platform == "win32":
            import subprocess
            CREATE_NO_WINDOW = 0x08000000
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(_MAIN_PID)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        else:
            pgid = os.getpgid(_MAIN_PID)
            os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try: os.kill(_MAIN_PID, signal.SIGKILL)
        except Exception: pass

atexit.register(_kill_process_tree)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re
import os
import logging

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import itertools
import matplotlib.colors as mcolors
from matplotlib.figure import Figure
from matplotlib.widgets import Cursor
import numpy as np

# ── SNAP7 (opzionale, per lettura diretta da PLC) ──────────
SNAP7_AVAILABLE = False
try:
    import snap7
    SNAP7_AVAILABLE = True
except ImportError:
    pass  # Tab PLC Reader mostra avviso

# ── OPC UA (opzionale, per lettura via OPC UA integrato) ────
OPCUA_AVAILABLE = False
OPCUA_LIB = None
try:
    pass  # opcua rimossa
    OPCUA_AVAILABLE = True
    OPCUA_LIB = "opcua"
except ImportError:
    try:
        from asyncua.sync import Client as OpcClient
        OPCUA_AVAILABLE = True
        OPCUA_LIB = "asyncua"
    except ImportError:
        pass  # Tab OPC UA mostra avviso

import struct
import datetime
import time
import concurrent.futures as _cf
import multiprocessing
import queue as _queue

# ──────────────────────────────────────────────────────────────
#  PARSER FILE .DB
# ──────────────────────────────────────────────────────────────

# ── Regex pre-compilate (modulo-level: compilate una volta sola) ─────────
_RE_ARRAY_VAL  = re.compile(
    r'(\w+)\[(\d+)\]\s*:=\s*([+-]?[\d]*\.?[\d]+(?:[eE][+-]?\d+)?)\s*;')
_RE_SCALAR_VAL = re.compile(r'^[ \t]*([\w.]+)\s*:=\s*([^;]+?)\s*;', re.MULTILINE)
_RE_BEGIN      = re.compile(r'\bBEGIN\b', re.IGNORECASE)


def _parse_db_body(text: str, result: dict) -> dict:
    """Corpo comune di parsing: popola result da testo .db (dopo BEGIN)."""
    begin_match = _RE_BEGIN.search(text)
    if not begin_match:
        raise ValueError("Blocco BEGIN non trovato nel file .db")
    body = text[begin_match.end():]

    arrays_raw: dict = {}
    for m in _RE_ARRAY_VAL.finditer(body):
        name, idx, val = m.group(1), int(m.group(2)), float(m.group(3))
        arrays_raw.setdefault(name, {})[idx] = val
    for name, idx_dict in arrays_raw.items():
        max_idx = max(idx_dict.keys())
        result["arrays"][name] = [idx_dict.get(i, float("nan")) for i in range(max_idx + 1)]

    array_names = set(arrays_raw.keys())
    for m in _RE_SCALAR_VAL.finditer(body):
        name, raw_val = m.group(1), m.group(2).strip()
        if name in array_names:
            continue
        rv_up = raw_val.upper()
        try:
            if rv_up == "TRUE":    result["scalars"][name] = True
            elif rv_up == "FALSE": result["scalars"][name] = False
            else:                  result["scalars"][name] = float(raw_val)
        except ValueError:
            result["scalars"][name] = raw_val
    return result


def parse_db_file(filepath: str) -> dict:
    """Legge e analizza un file .db TIA Portal da disco."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    result = {"scalars": {}, "arrays": {}, "raw_text": text,
              "filename": os.path.basename(filepath)}
    return _parse_db_body(text, result)


def parse_db_file_from_text(text: str, filename: str = "PLC_direct.db") -> dict:
    """Analizza un .db da testo già in memoria (per lettura diretta da PLC)."""
    result = {"scalars": {}, "arrays": {}, "raw_text": text, "filename": filename}
    return _parse_db_body(text, result)
#
#  Per ogni campione accettato dal doppio filtro:
#    1. Calcola soglia con FOR look-back (singolo, crescente)
#    2. Aggiorna cluster online
#    3. Se cluster >= min_consecutive → detection immediata
#
#  Restituisce anche il frame esatto in cui scatta il flag.
# ──────────────────────────────────────────────────────────────

def simulate_plc_realtime(
        raw_samples: list,
        raw_angles: list,
        # Filtro acquisizione
        min_angle_delta: float = 0.5,
        min_laser_delta: float = 0.1,
        max_samples: int = 5000,
        min_laser_valid: float = 0.0,
        max_laser_valid: float = 9999.0,
        axis_stand_still: bool = False,   # I_AxisTandSteel: TRUE=asse fermo -> rifiuta
        # Soglie
        window_deg: float = 10.0,
        sigma_factor: float = 3.0,
        min_abs_dev: float = 1.5,
        hyst_sigmas: float = 0.5,
        # Cluster
        min_consecutive: int = 3,
        max_consecutive: int = 60,
        stop_on_weld: bool = True,
        # *** NUOVO v4.3 ***
        peak_polarity: int = 0,  # 0=positivo, 1=negativo, 2=entrambi
        # *** NUOVO: Soglie precalcolate dal DB ***
        db_thresh_hi: list = None,    # arThreshHigh dal DB
        db_thresh_lo: list = None,    # arThreshLow dal DB
        db_thresh_hi_neg: list = None,  # arThreshHighNeg dal DB
        db_thresh_lo_neg: list = None,  # arThreshLowNeg dal DB
        db_mean: list = None,         # arMean dal DB
        db_sigma: list = None,        # arSigmaArr dal DB
        use_db_thresholds: bool = False,  # True = usa soglie dal DB invece di ricalcolarle
        # *** IMPORTANTE: usa float32 per replicare esattamente i calcoli del PLC ***
        use_float32: bool = True,
        # *** Skip filtro: i dati nel DB sono già filtrati dal PLC ***
        skip_filter: bool = False,
        use_fast_numpy: bool = True,   # look-back vettorizzato numpy (24x più veloce)
        # *** v4.6 ***
        adapt_baseline_enable: bool = False,
        adapt_baseline_offset: float = 3.0,
        flat_wait_enable: bool = False,
        flat_wait_samples: int = 5,
        flat_wait_toll: float = 0.5,
        detection_start_deg: float = 0.0,  # *** v4.9 *** dead zone separata; 0=usa window_deg
) -> dict:
    """
    Simula OFFLINE l'algoritmo SCL Blocco_1 v4.3 (rilevamento in tempo reale).

    NOVITA v4.3:
      Parametro peak_polarity per scegliere la polarita del picco:
        0 = Solo picchi POSITIVI (valore > baseline) [default]
        1 = Solo picchi NEGATIVI (valore < baseline)
        2 = ENTRAMBI (rileva quello con |deviazione| maggiore)

    Filtro acquisizione a 4 condizioni (replica SCL esatta):
      rAngDiff >= I_MinAngleDelta
      AND rLaserDiff >= I_MinLaserDelta
      AND ValidRangeValue (I_MinLaserValidValue <= laser <= I_MaxLaserValidValue)
      AND NOT I_AxisTandSteel (asse in rotazione)

    Ogni campione accettato: soglia adattiva + cluster online + check detection.

    Campi chiave nel risultato:
      detection_sample  : indice campione in cui bWeldFound e scattato (-1 = non trovato)
      detection_angle   : angolo corrispondente [deg]
      detection_delay_samples : quanti campioni dopo il fronte di salita del cluster
      detected_polarity : polarita del picco rilevato (0=pos, 1=neg)
      filt_* : campioni dopo filtro (Stato 1 doppio filtro)
      thresh_hi/lo, thresh_hi_neg/lo_neg, mean_arr, sigma_arr : soglie per campione
      clusters : lista cluster trovati
      weld_* : risultato finale
    """
    raw_samples = np.array(raw_samples, dtype=float)
    # *** v4.3.2 *** Angoli NON normalizzati: riporta la quota reale encoder
    #   come fa l'SCL. I calcoli interni usano ABS + wrap 180.
    raw_angles  = np.array(raw_angles,  dtype=float)
    n_raw = len(raw_samples)

    # ── Stato 1: doppio filtro + threshold + cluster (tutto live) ──
    # *** v4.3.97 *** float32 come arSamples/arAngles/arMean/arSigmaArr nel PLC
    _f32 = np.float32
    buf_s  = np.zeros(max_samples, dtype=np.float32)
    buf_a  = np.zeros(max_samples, dtype=np.float32)
    buf_th = np.zeros(max_samples, dtype=np.float32)
    buf_tl = np.zeros(max_samples, dtype=np.float32)
    buf_th_neg = np.zeros(max_samples, dtype=np.float32)  # *** v4.3 ***
    buf_tl_neg = np.zeros(max_samples, dtype=np.float32)  # *** v4.3 ***
    buf_m  = np.zeros(max_samples, dtype=np.float32)
    buf_sg = np.zeros(max_samples, dtype=np.float32)
    buf_wn = np.zeros(max_samples, dtype=int)   # win_n per campione (copertura finestra)

    # Accumulator float32: pre-allocato una volta, riusato senza ricreare oggetti
    # Usare accumulatori np.float32 già inizializzati elimina le conversioni esplicite
    # np.float32(v) per ogni elemento del look-back loop (era ~12 object alloc / campione)
    _f32_zero = np.float32(0.0)

    # *** v4.3: Flag polarita ***
    check_positive = peak_polarity in (0, 2)
    check_negative = peak_polarity in (1, 2)

    n_acq = 0
    last_a = -999.0
    last_l = raw_samples[0] - 2.0 * min_laser_delta if n_raw > 0 else 0.0

    # Statistiche filtro — dizionario per motivo (evita list di tuple da 2000+ elementi)
    n_rej_ang = 0;  n_rej_las = 0;  n_rej_both = 0;  n_rej_range = 0;  n_rej_axis = 0
    rej_idx = [];  rej_ang = [];  rej_las = [];  rej_why = []  # array separati

    # Cluster stato
    in_cluster = False
    cl_start = 0;  cl_count = 0
    cl_peak = 0.0; cl_peak_dev = 0.0; cl_peak_idx = 0
    cl_polarity = 0  # *** v4.3 *** 0=pos, 1=neg

    # Risultati
    clusters = []
    best_peak_dev = -1e10
    best_peak_dev_abs = -1.0  # *** v4.3 *** Per confronto assoluto
    best = None
    best_polarity = 0  # *** v4.3 ***

    weld_found = False
    detection_sample = -1
    detection_angle  = 0.0
    detection_cluster_start = -1   # indice campione inizio cluster -> per calcolo delay
    detected_polarity = 0  # *** v4.3 ***

    # *** v4.3.1 *** Viaggio angolare totale (supporta CW e CCW, come rTotalTravel in SCL)
    total_travel  = 0.0
    angle_exceeded = False  # *** v4.7 *** come bAngleExceeded SCL

    # *** v4.6 *** Stato baseline adattiva e superficie piatta
    adapt_bl_set   = False
    # V49: init con range fisico
    adapt_bl_min   = min_laser_valid
    adapt_bl_max   = max_laser_valid
    # *** v4.9 *** dead zone effettiva
    _det_start = detection_start_deg if detection_start_deg > 0.0 else window_deg
    flat_consec         = 0
    flat_found          = False
    flat_found_at_sample = -1
    flat_found_at_angle  = 0.0

    # Tracciamento frame-by-frame (per grafico animazione)
    frames = []   # lista di dict per ogni campione accettato

    for raw_i in range(n_raw):
        if n_acq >= max_samples:
            break

        ang = float(raw_angles[raw_i])
        las = float(raw_samples[raw_i])

        # V49: calibrazione one-shot sul primo campione nel range fisico
        if adapt_baseline_enable and not adapt_bl_set:
            if min_laser_valid <= las <= max_laser_valid:
                adapt_bl_min = las - adapt_baseline_offset
                adapt_bl_max = las + adapt_baseline_offset
                adapt_bl_set = True

        # *** Se skip_filter=True, i dati sono già filtrati dal PLC, accetta tutto ***
        if not skip_filter:
            # Filtro angolare
            if n_acq == 0:
                ang_diff = 1.0
            else:
                ang_diff = abs(ang - last_a)
                if ang_diff > 180.0:
                    ang_diff = 360.0 - ang_diff

            las_diff = abs(las - last_l)
            ok_a = ang_diff >= min_angle_delta
            ok_l = las_diff >= min_laser_delta
            # V49: ValidRange usa adapt range quando calibrato
            if adapt_baseline_enable and adapt_bl_set:
                ok_r = (adapt_bl_min <= las <= adapt_bl_max)
            else:
                ok_r = (min_laser_valid <= las <= max_laser_valid)

            if axis_stand_still:
                n_rej_axis += 1; rej_idx.append(raw_i); rej_ang.append(ang); rej_las.append(las); rej_why.append("axis"); continue
            if not ok_r:
                n_rej_range += 1; rej_idx.append(raw_i); rej_ang.append(ang); rej_las.append(las); rej_why.append("range"); continue
            if not ok_a and not ok_l:
                n_rej_both += 1; rej_idx.append(raw_i); rej_ang.append(ang); rej_las.append(las); rej_why.append("both"); continue
            elif not ok_a:
                n_rej_ang += 1; rej_idx.append(raw_i); rej_ang.append(ang); rej_las.append(las); rej_why.append("angle"); continue
            elif not ok_l:
                n_rej_las += 1; rej_idx.append(raw_i); rej_ang.append(ang); rej_las.append(las); rej_why.append("laser"); continue

        # Campione accettato → salva
        cur = n_acq
        buf_s[cur] = las
        buf_a[cur] = ang
        last_a = ang
        last_l = las
        n_acq += 1

        # *** v4.3.1 *** Aggiorna viaggio angolare totale (CW e CCW)
        if cur > 0:
            a_step = abs(buf_a[cur] - buf_a[cur - 1])
            if a_step > 180.0:
                a_step = 360.0 - a_step
            total_travel += a_step
        # *** v4.9 *** aggiorna angle_exceeded con dead zone separata
        if not angle_exceeded and total_travel > _det_start:
            angle_exceeded = True

        # *** v4.7.1 *** C-bis: vigile - ricalcola ogni ciclo, si resetta se instabile
        if flat_wait_enable and cur > 0:
            if abs(buf_s[cur] - buf_s[cur - 1]) < np.float32(flat_wait_toll):
                flat_consec += 1
            else:
                flat_consec = 0
            prev_flat = flat_found
            flat_found = flat_consec >= flat_wait_samples
            if flat_found and not prev_flat:
                flat_found_at_sample = cur
                flat_found_at_angle  = float(buf_a[cur])

        # ── Soglie: usa quelle dal DB se disponibili, altrimenti calcola ──
        if use_db_thresholds and db_thresh_hi is not None and cur < len(db_thresh_hi):
            # USA SOGLIE DAL DB (replica esatta comportamento PLC)
            th_hi = db_thresh_hi[cur] if db_thresh_hi[cur] != 0 else (db_mean[cur] + sigma_factor * db_sigma[cur] + min_abs_dev if db_mean else las + 10)
            th_lo = db_thresh_lo[cur] if db_thresh_lo is not None and cur < len(db_thresh_lo) and db_thresh_lo[cur] != 0 else th_hi - 0.25
            m = db_mean[cur] if db_mean is not None and cur < len(db_mean) else las
            s = db_sigma[cur] if db_sigma is not None and cur < len(db_sigma) else 0.0
            win_n = 10  # Placeholder
            
            # Soglie negative dal DB
            if db_thresh_hi_neg is not None and cur < len(db_thresh_hi_neg):
                th_hi_neg = db_thresh_hi_neg[cur]
                th_lo_neg = db_thresh_lo_neg[cur] if db_thresh_lo_neg is not None and cur < len(db_thresh_lo_neg) else th_hi_neg + 0.25
            else:
                th_hi_neg = m - sigma_factor * s - min_abs_dev
                th_lo_neg = m - (sigma_factor - hyst_sigmas) * s - min_abs_dev * 0.5
        else:
            # CALCOLA SOGLIE — look-back vettorizzato numpy (24x più veloce del loop Python)
            # use_fast_numpy=True: calcolo batch con slicing; use_float32 preserva replica PLC
            if cur > 0 and use_fast_numpy:
                # Distanza angolare vettorizzata su tutti i campioni precedenti
                ad = np.abs(buf_a[cur] - buf_a[:cur])
                np.subtract(360.0, ad, where=ad > 180.0, out=ad)
                mask = (ad >= 1.0) & (ad <= window_deg)
                # *** v4.6 *** filtro range adattivo: esclude buchi dalla baseline
                # V49: filtro look-back rimosso
                win_n = int(mask.sum())
                if win_n >= 3:
                    vs = buf_s[:cur][mask]
                    if use_float32:
                        # Accumulo sequenziale float32 — replica esatta PLC REAL
                        # (numpy batch sum usa ordine diverso → risultati diversi)
                        wsum = _f32_zero; wsq = _f32_zero
                        vs32 = vs  # gia float32 (buf_s dtype=float32)
                        for _v in vs32:
                            wsum += _v; wsq += _v * _v
                        wn32 = np.float32(win_n)
                        m32  = wsum / wn32
                        var  = wsq / wn32 - m32 * m32
                        if var < _f32_zero: var = _f32_zero
                        s = float(np.sqrt(var));  m = float(m32)
                        # *** v4.7 *** baseline adattiva tracking continuo
                        # V49: tracking rimosso
                    else:
                        wsum = float(vs.sum()); wsq = float((vs*vs).sum())
                        m = wsum / win_n
                        var = wsq / win_n - m * m
                        s = float(np.sqrt(max(0.0, var)))
                        # V49: tracking rimosso
                else:
                    m = float(buf_m[cur-1]) if cur > 0 else las
                    s = float(buf_sg[cur-1]) if cur > 0 else 0.0
            else:
                # Fallback loop Python (usato quando cur==0 o use_fast_numpy=False)
                if use_float32:
                    win_sum = _f32_zero; win_sq = _f32_zero
                else:
                    win_sum = 0.0; win_sq = 0.0
                win_n = 0
                for j in range(cur):
                    ad = abs(buf_a[cur] - buf_a[j])
                    if ad > 180.0: ad = 360.0 - ad
                    if 1.0 <= ad <= window_deg:
                        v = buf_s[j]
                        # *** v4.6 *** filtro range adattivo
                        if adapt_baseline_enable and adapt_bl_set:
                            if v < adapt_bl_min or v > adapt_bl_max:
                                continue
                        win_sum += v; win_sq += v * v; win_n += 1
                if win_n >= 3:
                    if use_float32:
                        m = win_sum / np.float32(win_n)
                        var = win_sq / np.float32(win_n) - m * m
                        s = float(np.sqrt(max(_f32_zero, var))); m = float(m)
                    else:
                        m = win_sum / win_n; var = win_sq / win_n - m * m
                        s = float(np.sqrt(max(0.0, var)))
                    # *** v4.7 *** baseline adattiva tracking continuo (fallback)
                        # V49: tracking rimosso
                else:
                    m = float(buf_m[cur-1]) if cur > 0 else las
                    s = float(buf_sg[cur-1]) if cur > 0 else 0.0

            # Soglie POSITIVE (valore > baseline)
            th_hi = m + sigma_factor * s + min_abs_dev
            th_lo = m + (sigma_factor - hyst_sigmas) * s + min_abs_dev * 0.5
            
            # Soglie NEGATIVE (valore < baseline)
            th_hi_neg = m - sigma_factor * s - min_abs_dev
            th_lo_neg = m - (sigma_factor - hyst_sigmas) * s - min_abs_dev * 0.5

        buf_m[cur]  = m
        buf_sg[cur] = s
        buf_wn[cur] = win_n
        buf_th[cur] = th_hi
        buf_tl[cur] = th_lo
        buf_th_neg[cur] = th_hi_neg
        buf_tl_neg[cur] = th_lo_neg
        buf_tl_neg[cur] = th_lo_neg

        # ── Cluster online ─────────────────────────────────────
        dev = las - m

        if not in_cluster:
            # *** v4.3: Check apertura cluster per polarita configurata ***
            # *** v4.7.1 *** guard FlatWait: cluster aperto solo se superficie piatta
            _fok = not flat_wait_enable or flat_found
            # Check apertura cluster POSITIVO
            if _fok and check_positive and las >= th_hi:
                in_cluster = True
                cl_polarity = 0  # Positivo
                cl_start   = cur
                cl_count   = 1
                cl_peak    = las
                cl_peak_dev = dev
                cl_peak_idx = cur
            # Check apertura cluster NEGATIVO
            elif _fok and check_negative and las <= th_hi_neg:
                in_cluster = True
                cl_polarity = 1  # Negativo
                cl_start   = cur
                cl_count   = 1
                cl_peak    = las
                cl_peak_dev = dev
                cl_peak_idx = cur

        elif in_cluster:
            # *** v4.3: Gestione cluster basata sulla polarita ***
            if cl_polarity == 0:
                # Cluster POSITIVO
                if las >= th_lo:
                    cl_count += 1
                    if dev > cl_peak_dev:
                        cl_peak     = las
                        cl_peak_dev = dev
                        cl_peak_idx = cur
                else:
                    # Chiude cluster positivo
                    c = {
                        "start": cl_start, "end": cur - 1, "count": cl_count,
                        "peak": cl_peak, "peak_dev": cl_peak_dev, "peak_idx": cl_peak_idx,
                        "valid": min_consecutive <= cl_count <= max_consecutive,
                        "polarity": 0,
                    }
                    clusters.append(c)
                    if c["valid"] and abs(cl_peak_dev) > best_peak_dev_abs:
                        best_peak_dev_abs = abs(cl_peak_dev)
                        best_peak_dev = cl_peak_dev
                        best = c
                        best_polarity = 0
                    in_cluster = False;  cl_count = 0
            else:
                # Cluster NEGATIVO
                if las <= th_lo_neg:
                    cl_count += 1
                    if dev < cl_peak_dev:  # Piu negativo e meglio
                        cl_peak     = las
                        cl_peak_dev = dev
                        cl_peak_idx = cur
                else:
                    # Chiude cluster negativo
                    c = {
                        "start": cl_start, "end": cur - 1, "count": cl_count,
                        "peak": cl_peak, "peak_dev": cl_peak_dev, "peak_idx": cl_peak_idx,
                        "valid": min_consecutive <= cl_count <= max_consecutive,
                        "polarity": 1,
                    }
                    clusters.append(c)
                    if c["valid"] and abs(cl_peak_dev) > best_peak_dev_abs:
                        best_peak_dev_abs = abs(cl_peak_dev)
                        best_peak_dev = cl_peak_dev
                        best = c
                        best_polarity = 1
                    in_cluster = False;  cl_count = 0

        # ── Check detection immediata ──────────────────────────
        # *** v4.7 *** Guard: angle_exceeded (bAngleExceeded SCL) + FlatWait
        _flat_ok = (not flat_wait_enable) or flat_found
        if in_cluster and cl_count >= min_consecutive and not weld_found \
                and angle_exceeded and _flat_ok:
            weld_found        = True
            detection_sample  = cur
            detection_angle   = buf_a[cur]
            detection_cluster_start = cl_start
            detected_polarity = cl_polarity  # *** v4.3 ***

        # Traccia frame
        frames.append({
            "idx": cur, "angle": ang, "laser": las,
            "th_hi": th_hi, "th_lo": th_lo, "mean": m, "sigma": s,
            "th_hi_neg": th_hi_neg, "th_lo_neg": th_lo_neg,  # *** v4.3 ***
            "in_cluster": in_cluster, "cl_count": cl_count,
            "cl_polarity": cl_polarity if in_cluster else -1,  # *** v4.3 ***
            "weld_found_here": weld_found and detection_sample == cur,
            "win_n": win_n,
            "flat_consec": flat_consec,
            "flat_found_f": flat_found,
        })

        # Stop se richiesto
        if stop_on_weld and weld_found:
            break

    # ── Cluster ancora aperto a fine scan ──
    if in_cluster:
        c = {
            "start": cl_start, "end": n_acq - 1, "count": cl_count,
            "peak": cl_peak, "peak_dev": cl_peak_dev, "peak_idx": cl_peak_idx,
            "valid": min_consecutive <= cl_count <= max_consecutive,
            "polarity": cl_polarity,  # *** v4.3 ***
            "wrap": True,
        }
        clusters.append(c)
        if c["valid"] and abs(cl_peak_dev) > best_peak_dev_abs:
            best_peak_dev_abs = abs(cl_peak_dev)
            best_peak_dev = cl_peak_dev
            best = c
            best_polarity = cl_polarity

    clusters_found = len(clusters)
    clusters_valid = sum(1 for c in clusters if c["valid"])

    # Consolida risultato finale
    if not stop_on_weld and best is not None:
        weld_found = True

    if weld_found and best is None and clusters_valid > 0:
        best = next(c for c in clusters if c["valid"])

    if weld_found and best is not None:
        weld_center    = float(buf_a[best["peak_idx"]])
        weld_start_ang = float(buf_a[best["start"]])
        weld_end_ang   = float(buf_a[best["end"]])
        peak_value     = float(best["peak"])
        peak_dev       = float(best["peak_dev"])
        consec         = best["count"]
    else:
        weld_center = weld_start_ang = weld_end_ang = 0.0
        peak_value = peak_dev = consec = 0.0

    # Diagnostica baseline (primi 30%)
    # SCL: FOR i_diag := 0 TO iSamplesAcquired*3/10 (inclusivo) → +1 elemento
    diag_end = max(1, n_acq * 3 // 10 + 1)
    bl_mean  = float(np.mean(buf_m[:diag_end]))
    bl_sigma = float(np.sqrt(np.mean((buf_s[:diag_end] - np.float32(bl_mean)) ** 2)))
    # *** v4.3: Usa valore assoluto per peak_sigmas ***
    peak_sigmas = abs(peak_dev) / (bl_sigma + 1e-6)

    detection_delay = (detection_sample - detection_cluster_start) if detection_sample >= 0 else -1

    # Rolling SNR: per ogni campione, rapporto |dev| / sigma locale
    rolling_snr = np.zeros(n_acq)
    for ri in range(n_acq):
        sg_loc = buf_sg[ri]
        dev_loc = abs(buf_s[ri] - buf_m[ri])
        rolling_snr[ri] = dev_loc / (sg_loc + 1e-9)

    return {
        "filt_samples":   buf_s[:n_acq].copy(),
        "filt_angles":    buf_a[:n_acq].copy(),
        "n_acquired":     n_acq,
        "mean_arr":       buf_m[:n_acq].copy(),
        "sigma_arr":      buf_sg[:n_acq].copy(),
        "thresh_hi":      buf_th[:n_acq].copy(),
        "thresh_lo":      buf_tl[:n_acq].copy(),
        "thresh_hi_neg":  buf_th_neg[:n_acq].copy(),  # *** v4.3 ***
        "thresh_lo_neg":  buf_tl_neg[:n_acq].copy(),  # *** v4.3 ***
        "win_n_arr":      buf_wn[:n_acq].copy(),
        "rolling_snr":    rolling_snr,
        "clusters":       clusters,
        "frames":         frames,
        "clusters_found": clusters_found,
        "clusters_valid": clusters_valid,
        "weld_found":     weld_found,
        "weld_center":    weld_center,
        "weld_start":     weld_start_ang if weld_found else 0.0,
        "weld_end":       weld_end_ang   if weld_found else 0.0,
        "peak_value":     peak_value,
        "peak_deviation": peak_dev,
        "peak_sigmas":    peak_sigmas,
        "consecutive_count": int(consec),
        "baseline_mean":  bl_mean,
        "baseline_sigma": bl_sigma,
        "adaptive_threshold": bl_mean + sigma_factor * bl_sigma + min_abs_dev,
        "detection_sample":  detection_sample,
        "detection_angle":   detection_angle,
        "detection_delay_samples": detection_delay,
        "detected_polarity": detected_polarity,  # *** v4.3 ***
        "best_polarity": best_polarity,  # *** v4.3 ***
        "total_travel": total_travel,  # *** v4.3.1 *** viaggio angolare totale [deg]
        "adapt_bl_set":   adapt_bl_set,   # *** v4.6 ***
        "adapt_bl_min":   adapt_bl_min,   # *** v4.6 ***
        "adapt_bl_max":   adapt_bl_max,   # *** v4.6 ***
        "flat_found":           flat_found,
        "flat_found_at_sample": flat_found_at_sample,
        "flat_found_at_angle":  flat_found_at_angle,
        "angle_exceeded":    angle_exceeded,
        "det_start_deg":     _det_start,    # *** v4.9 ***
        "flat_wait_samples": flat_wait_samples,
        "flat_consec":    flat_consec,    # *** v4.6 ***
        "frames": frames,
        "rejections": list(zip(rej_idx, rej_ang, rej_las, rej_why)),  # ricostruisce lista tuple per compatibilità
        "filter_stats": {
            "n_raw": n_raw, "n_acquired": n_acq,
            "n_rejected_angle": n_rej_ang,
            "n_rejected_laser": n_rej_las,
            "n_rejected_both":  n_rej_both,
            "n_rejected_range": n_rej_range,
            "n_rejected_axis":  n_rej_axis,
            "filter_ratio": 100.0 * (n_raw - n_acq) / max(n_raw, 1),
        },
    }


# ──────────────────────────────────────────────────────────────
#  BASELINE per tab analisi DB (non streaming, ricalcolo post)
# ──────────────────────────────────────────────────────────────

def compute_adaptive_baseline(samples, window_deg=10.0, sigma_factor=3.0,
                               min_abs_dev=1.5, angles_in=None):
    n = len(samples)
    if n == 0:
        return {}
    samples  = np.array(samples, dtype=float)
    # *** v4.3.2 *** Angoli grezzi (ABS + wrap 180 nel loop gestisce qualsiasi range)
    angles   = np.array(angles_in, dtype=float) if angles_in is not None and len(angles_in) == n \
               else np.linspace(0, 360, n, endpoint=False)
    baseline = np.zeros(n);  sigma_arr = np.zeros(n)
    thresh_hi = np.zeros(n); thresh_lo = np.zeros(n)
    hyst = 0.5

    for i in range(n):
        ai   = angles[i]
        # *** v4.3.1 *** Distanza angolare minima (bidirezionale, supporta CW e CCW)
        vals = []
        for j in range(n):
            if j == i:
                continue
            ad = abs(ai - angles[j])
            if ad > 180.0:
                ad = 360.0 - ad
            if 1.0 <= ad <= window_deg:
                vals.append(float(samples[j]))
        if len(vals) >= 3:
            m = float(np.mean(vals));  s = float(np.sqrt(max(np.var(vals), 0.0)))
        else:
            m = float(baseline[i-1]) if i > 0 else float(samples[i])
            s = float(sigma_arr[i-1]) if i > 0 else 0.0
        baseline[i] = m;  sigma_arr[i] = s
        thresh_hi[i] = m + sigma_factor * s + min_abs_dev
        thresh_lo[i] = m + (sigma_factor - hyst) * s + min_abs_dev * 0.5

    # *** v4.3.83 *** Soglie negative (calcolate vettorialmente post-loop)
    thresh_neg_hi = baseline - sigma_factor * sigma_arr - min_abs_dev
    thresh_neg_lo = baseline - (sigma_factor - hyst) * sigma_arr - min_abs_dev * 0.5

    diag_end  = max(1, n * 3 // 10)
    diag_mean = float(np.mean(baseline[:diag_end]))
    diag_devs = samples[:diag_end] - diag_mean
    diag_sig  = float(np.sqrt(np.mean(diag_devs**2)))

    return {
        "samples": samples, "baseline": baseline, "sigma": sigma_arr,
        "thresh_hi": thresh_hi, "thresh_lo": thresh_lo,
        "thresh_hi_neg": thresh_neg_hi, "thresh_lo_neg": thresh_neg_lo,  # *** v4.3.83 ***
        "delta": samples - baseline,
        "dyn_threshold": diag_sig * sigma_factor + min_abs_dev,
        "angles": angles,
        "global_mean": float(np.mean(samples)), "global_std": float(np.std(samples)),
        "diag_mean": diag_mean, "diag_sigma": diag_sig,
    }



# ──────────────────────────────────────────────────────────────
#  HELPER: ottieni valore scalare con alias (nuovi + vecchi nomi)
# ──────────────────────────────────────────────────────────────

def _sc(sc: dict, *keys, default=None):
    """Cerca il primo tasto trovato tra i candidati."""
    for k in keys:
        if k in sc:
            return sc[k]
    return default


# ──────────────────────────────────────────────────────────────
#  PALETTE
# ──────────────────────────────────────────────────────────────

DARK_BG = "#000000"; PANEL_BG = "#0d1117"; BORDER_CLR = "#484f58"
ACCENT  = "#79c0ff"; WELD_CLR = "#ff9070"; OK_CLR = "#56d364"
WARN_CLR = "#e3b341"; TEXT_CLR = "#f0f6fc"; MUTED_CLR = "#b1bac4"
ENTRY_BG = "#161b22"; SIM_CLR = "#d2a8ff"; DET_CLR = "#ff6e85"
STAT_CLR = "#f9c74f"; ERR_CLR  = "#FF6B6B"; CIAN_CLR = "#4fc3f7"
UDT_CLR  = CIAN_CLR    # colore per campi UDT IO_RicercaSaldatura
PLC_CLR  = "#f0883e"   # colore per tab PLC Reader
OPCUA_CLR = "#9b59b6"  # colore per tab OPC UA


# ══════════════════════════════════════════════════════════════════
#  PLC DB READER — lettura diretta DB da PLC via Snap7
# ══════════════════════════════════════════════════════════════════

PLC_MAX_SAMPLES_DEFAULT = 2001   # Array [0..2000] (v4.3 attuale)
PLC_MAX_SAMPLES_LEGACY  = 5001   # Array [0..5000] (versione precedente)
PLC_REAL_SIZE = 4;  PLC_LREAL_SIZE = 8;  PLC_INT_SIZE = 2
PLC_FB_TYPE_NAME = "Fb954_WeldFindV47"
PLC_INOUT_PTR_DEFAULT = 6  # byte per puntatore VAR_IN_OUT


def plc_build_offset_map(inout_ptr_size=PLC_INOUT_PTR_DEFAULT,
                         max_samples=PLC_MAX_SAMPLES_DEFAULT):
    """Costruisce mappa offset per DB non-ottimizzato dal layout SCL."""
    array_bytes = max_samples * PLC_REAL_SIZE
    entries = [];  off = 0

    def align():
        nonlocal off
        if off % 2: off += 1

    def real(n):
        nonlocal off;  align()
        entries.append((n, off, 'real', PLC_REAL_SIZE));  off += PLC_REAL_SIZE

    def lreal(n):
        nonlocal off;  align()
        entries.append((n, off, 'lreal', PLC_LREAL_SIZE));  off += PLC_LREAL_SIZE

    def sint(n):
        nonlocal off;  align()
        entries.append((n, off, 'int', PLC_INT_SIZE));  off += PLC_INT_SIZE

    def bools(names):
        nonlocal off;  align();  base = off
        for i, n in enumerate(names):
            entries.append((n, base + i // 8, 'bool', i % 8))
        off = base + (len(names) - 1) // 8 + 1;  align()

    def arr(n):
        nonlocal off;  align()
        entries.append((n, off, 'array_real', array_bytes));  off += array_bytes

    # VAR_INPUT
    real('I_LaserValue');  lreal('I_CurrentAngle');  bools(['I_AxisTandSteel'])
    # VAR_INPUT RETAIN
    real('I_BaselineWindowDeg');  real('I_SigmaFactor');  real('I_MinAbsDeviation')
    real('I_HysteresisSigmas');  sint('I_MinConsecutive');  sint('I_MaxConsecutive')
    real('I_MinAngleDelta');  real('I_MinLaserDelta');  bools(['I_StopOnWeld'])
    real('I_MaxLaserValidValue');  real('I_MinLaserValidValue');  sint('I_PeakPolarity')
    # *** v4.6 *** Nuovi VAR_INPUT
    bools(['I_AdaptBaselineEnable'])   # *** v4.6 ***
    real('I_AdaptBaselineOffset')      # *** v4.6 ***
    bools(['I_FlatWaitEnable'])         # *** v4.6 ***
    sint('I_FlatWaitSamples')           # *** v4.6 ***
    real('I_FlatWaitToll')              # *** v4.6 ***
    real('I_DetectionStartDeg')         # *** v4.9 ***
    # VAR_OUTPUT
    bools(['O_Ready', 'O_Busy', 'O_Done', 'O_Error'])
    sint('O_ErrorCode');  sint('O_DetectedPolarity')
    # VAR_IN_OUT (puntatore)
    align();  off += inout_ptr_size;  align()
    # VAR STAT — Array
    for a in ('arSamples', 'arAngles', 'arThreshHigh', 'arThreshLow',
              'arThreshHighNeg', 'arThreshLowNeg', 'arMean', 'arSigmaArr'):
        arr(a)
    # VAR STAT — Scalari
    sint('iState');  real('rLastAngleSaved');  real('rLastLaserSaved')
    bools(['bAngleExceeded', 'bInCluster'])
    sint('iClusterStart');  sint('iClusterCount');  real('rClusterPeak')
    real('rClusterPeakDev');  sint('iPeakIndex')
    real('rBestPeakDev');  real('rBestPeakDevAbs')
    sint('iBestStart');  sint('iBestEnd');  sint('iBestPeakIdx');  sint('iBestCount')
    real('rBestPeakAbs');  sint('iBestPolarity')
    bools(['bStart_Prev', 'bReset_Prev'])
    sint('j');  real('rAngDiff');  real('rLaserDiff')
    real('rWinSum');  real('rWinSumSq');  sint('iWinN')
    real('rM');  real('rV');  real('rS');  real('rDev');  real('rAngleCurrent')
    real('rThHi');  real('rThLo');  real('rThHiNeg');  real('rThLoNeg')
    sint('iCurIdx');  real('rTmpSum');  sint('i_diag');  sint('iDiagN');  real('rVal')
    real('rWeldAngleStart');  real('rWeldAngleEnd')
    real('rPeakValue');  real('rPeakDeviation');  real('rPeakSigmas')
    sint('iConsecutiveCount');  sint('iDetectedAtSample');  real('rDetectedAtAngle')
    real('rBaselineMean');  real('rBaselineSigma');  real('rAdaptiveThreshold')
    sint('iClustersFound');  sint('iClustersValid')
    bools(['bWrapAround', 'bMultipleClusters'])
    sint('iSamplesAcquired');  sint('iCurrentClusterPolarity')
    bools(['bCheckPositive', 'bCheckNegative'])
    real('rTotalTravel')
    real('rTravelAfterDet')   # *** v4.4 ***
    bools(['bWeldDetectedInternal'])  # *** v4.5 ***
    # *** v4.6 *** Nuove VAR statiche in coda
    real('rAdaptBaselineMin')           # *** v4.6 ***
    real('rAdaptBaselineMax')           # *** v4.6 ***
    bools(['bAdaptBaselineSet', 'bFlatSurfaceFound'])  # *** v4.6 ***
    sint('iFlatConsecCount')             # *** v4.6 ***
    real('rDetStartDeg')                 # *** v4.9 ***
    return entries, off, max_samples


def plc_decode_real(d, o):  return struct.unpack('>f', d[o:o+4])[0]
def plc_decode_lreal(d, o): return struct.unpack('>d', d[o:o+8])[0]
def plc_decode_int(d, o):  return struct.unpack('>h', d[o:o+2])[0]
def plc_decode_bool(d, o, b): return bool(d[o] & (1 << b))

# Dimensione in byte per tipo PLC — costante modulo-level (usata in _rt_poll
# senza ricreare il dict ad ogni iterazione)
_RT_DTYPE_SIZE = {"real": 4, "lreal": 8, "int": 2, "bool": 1, "array_real": 0}

def plc_decode_array_real(d, o, n):
    return list(struct.unpack(f'>{n}f', d[o:o + n * 4]))


def plc_decode_db(raw, offset_map, max_samples=PLC_MAX_SAMPLES_DEFAULT):
    """Decodifica byte grezzi usando la mappa offset → dict scalars/arrays."""
    result = {'scalars': {}, 'arrays': {}}
    for name, off, dtype, sz in offset_map:
        try:
            if dtype == 'real':    result['scalars'][name] = plc_decode_real(raw, off)
            elif dtype == 'lreal': result['scalars'][name] = plc_decode_lreal(raw, off)
            elif dtype == 'int':   result['scalars'][name] = plc_decode_int(raw, off)
            elif dtype == 'bool':  result['scalars'][name] = plc_decode_bool(raw, off, sz)
            elif dtype == 'array_real':
                result['arrays'][name] = plc_decode_array_real(raw, off, max_samples)
        except (struct.error, IndexError):
            if dtype == 'array_real': result['arrays'][name] = [0.0] * max_samples
            elif dtype == 'bool':     result['scalars'][name] = False
            elif dtype == 'int':      result['scalars'][name] = 0
            else:                     result['scalars'][name] = 0.0
    return result


# Ordine variabili nel file .db
_PLC_SCALAR_PRE = [
    'I_LaserValue', 'I_CurrentAngle', 'I_AxisTandSteel',
    'I_BaselineWindowDeg', 'I_SigmaFactor', 'I_MinAbsDeviation',
    'I_HysteresisSigmas', 'I_MinConsecutive', 'I_MaxConsecutive',
    'I_MinAngleDelta', 'I_MinLaserDelta', 'I_StopOnWeld',
    'I_MaxLaserValidValue', 'I_MinLaserValidValue', 'I_PeakPolarity',
    'I_AdaptBaselineEnable', 'I_AdaptBaselineOffset',  # *** v4.6 ***
    'I_FlatWaitEnable', 'I_FlatWaitSamples', 'I_FlatWaitToll',  # *** v4.6 ***
    'I_DetectionStartDeg',  # *** v4.9 ***
    'O_Ready', 'O_Busy', 'O_Done', 'O_Error', 'O_ErrorCode', 'O_DetectedPolarity',
]
_PLC_ARRAY_ORDER = [
    'arSamples', 'arAngles', 'arThreshHigh', 'arThreshLow',
    'arThreshHighNeg', 'arThreshLowNeg', 'arMean', 'arSigmaArr',
]
_PLC_SCALAR_POST = [
    'iState', 'rLastAngleSaved', 'rLastLaserSaved',
    'bAngleExceeded', 'bInCluster', 'iClusterStart', 'iClusterCount',
    'rClusterPeak', 'rClusterPeakDev', 'iPeakIndex',
    'rBestPeakDev', 'rBestPeakDevAbs',
    'iBestStart', 'iBestEnd', 'iBestPeakIdx', 'iBestCount',
    'rBestPeakAbs', 'iBestPolarity', 'bStart_Prev', 'bReset_Prev',
    'j', 'rAngDiff', 'rLaserDiff', 'rWinSum', 'rWinSumSq', 'iWinN',
    'rM', 'rV', 'rS', 'rDev', 'rAngleCurrent',
    'rThHi', 'rThLo', 'rThHiNeg', 'rThLoNeg',
    'iCurIdx', 'rTmpSum', 'i_diag', 'iDiagN', 'rVal',
    'rWeldAngleStart', 'rWeldAngleEnd',
    'rPeakValue', 'rPeakDeviation', 'rPeakSigmas',
    'iConsecutiveCount', 'iDetectedAtSample', 'rDetectedAtAngle',
    'rBaselineMean', 'rBaselineSigma', 'rAdaptiveThreshold',
    'iClustersFound', 'iClustersValid', 'bWrapAround', 'bMultipleClusters',
    'iSamplesAcquired', 'iCurrentClusterPolarity',
    'bCheckPositive', 'bCheckNegative', 'rTotalTravel', 'rTravelAfterDet',
    'bWeldDetectedInternal',    # *** v4.5 ***
    'rAdaptBaselineMin', 'rAdaptBaselineMax',  # *** v4.6 ***
    'bAdaptBaselineSet', 'bFlatSurfaceFound', 'iFlatConsecCount',  # *** v4.6 ***
    'rDetStartDeg',  # *** v4.9 ***
]
_PLC_INT_VARS = {
    'I_MinConsecutive', 'I_MaxConsecutive', 'I_PeakPolarity',
    'I_FlatWaitSamples',  # *** v4.6 ***
    'O_ErrorCode', 'O_DetectedPolarity', 'iState',
    'iClusterStart', 'iClusterCount', 'iPeakIndex',
    'iBestStart', 'iBestEnd', 'iBestPeakIdx', 'iBestCount', 'iBestPolarity',
    'j', 'iWinN', 'iCurIdx', 'i_diag', 'iDiagN',
    'iConsecutiveCount', 'iDetectedAtSample',
    'iClustersFound', 'iClustersValid',
    'iSamplesAcquired', 'iCurrentClusterPolarity',
    'iFlatConsecCount',  # *** v4.6 ***
}
_PLC_BOOL_VARS = {
    'I_AxisTandSteel', 'I_StopOnWeld', 'O_Ready', 'O_Busy', 'O_Done', 'O_Error',
    'bAngleExceeded', 'bInCluster', 'bStart_Prev', 'bReset_Prev',
    'bWrapAround', 'bMultipleClusters', 'bCheckPositive', 'bCheckNegative',
    'bWeldDetectedInternal',    # *** v4.5 ***
    'I_AdaptBaselineEnable', 'I_FlatWaitEnable',  # *** v4.6 ***
    'bAdaptBaselineSet', 'bFlatSurfaceFound',  # *** v4.6 ***
}


def plc_generate_db_text(decoded, db_name="WeldFindExport",
                         max_samples=PLC_MAX_SAMPLES_DEFAULT):
    """Genera file .db in formato TIA Portal dall'output del decoder."""
    lines = [
        f'DATA_BLOCK "{db_name}"',
        "{ DB_Accessible_From_OPC_UA := 'FALSE' ;",
        " S7_Optimized_Access := 'FALSE' }",
        "VERSION : 0.1", "NON_RETAIN",
        f'"{PLC_FB_TYPE_NAME}"', "", "BEGIN",
    ]
    sc = decoded['scalars'];  ar = decoded['arrays']

    def fmt(name, val):
        if name in _PLC_BOOL_VARS: return 'TRUE' if val else 'FALSE'
        if name in _PLC_INT_VARS:  return str(int(val))
        s = f"{val:.7g}"
        if '.' not in s and 'e' not in s.lower(): s += '.0'
        return s

    for n in _PLC_SCALAR_PRE:
        if n in sc: lines.append(f"   {n} := {fmt(n, sc[n])};")
    for an in _PLC_ARRAY_ORDER:
        if an in ar:
            a = ar[an]
            for i in range(max_samples):
                v = a[i] if i < len(a) else 0.0
                s = f"{v:.7g}"
                if '.' not in s and 'e' not in s.lower(): s += '.0'
                lines.append(f"   {an}[{i}] := {s};")
    for n in _PLC_SCALAR_POST:
        if n in sc: lines.append(f"   {n} := {fmt(n, sc[n])};")
    lines += ["", "END_DATA_BLOCK", ""]
    return '\n'.join(lines)


def plc_resolve_db_size(client, db_number, map_size):
    """Usa la dimensione reale dal PLC se disponibile, altrimenti usa map_size.
    Gestisce compatibilità tra versioni SCL con DB di dimensione diversa."""
    try:
        import snap7.type as _s7t
        info = client.get_block_info(_s7t.Block.DB, db_number)
        plc_size = int(info.MC7Size)
        # Usa il minore tra i due: evita di leggere oltre il DB reale
        # ma accetta DB più grandi (versioni SCL future con variabili extra)
        return min(plc_size, map_size) if plc_size > 0 else map_size
    except Exception:
        return map_size


class PLCReader:
    """Gestisce connessione snap7 e lettura DB da PLC S7-1500/1200."""
    def __init__(self, ip, rack=0, slot=1):
        if not SNAP7_AVAILABLE:
            raise ImportError("python-snap7 non installato!\npip install python-snap7")
        self.ip = ip;  self.rack = rack;  self.slot = slot
        self.client = snap7.client.Client()

    def connect(self):
        self.client.connect(self.ip, self.rack, self.slot)
        if not self.client.get_connected():
            raise ConnectionError(f"Impossibile connettersi a {self.ip}")
        info = self.client.get_cpu_info()
        cpu = info.ModuleTypeName.decode().strip()
        pdu = self.client.get_pdu_length()
        return cpu, pdu

    def disconnect(self):
        if self.client.get_connected():
            self.client.disconnect()

    def get_actual_db_size(self, db_number):
        """Legge la dimensione reale del DB dal PLC via get_block_info.
        Usato per rilevare automaticamente la versione SCL (v4.3 vs v4.4+).
        Ritorna int o None se non disponibile."""
        try:
            import snap7.type as _s7t
            info = self.client.get_block_info(_s7t.Block.DB, db_number)
            return int(info.MC7Size)
        except Exception:
            return None

    def read_db_raw(self, db_number, total_size, chunk=400, callback=None):
        data = bytearray(total_size);  off = 0;  reads = 0
        while off < total_size:
            sz = min(chunk, total_size - off)
            try:
                data[off:off+sz] = self.client.db_read(db_number, off, sz)
            except Exception as e:
                raise RuntimeError(
                    f"Errore lettura DB{db_number} @{off}: {e}\n"
                    f"Verifica: 1) DB esiste  2) S7_Optimized=FALSE  3) PUT/GET abilitato")
            off += sz;  reads += 1
            if callback and reads % 20 == 0:
                callback(off * 100 // total_size)
        return data


# ──────────────────────────────────────────────────────────────
#  OPC UA READER – Lettura DB WeldFind via OPC UA integrato S7-1500
# ──────────────────────────────────────────────────────────────

# Variabili "firma" per identificare un DB come WeldFind
_OPCUA_WELDFIND_SIGNATURE = {
    "arSamples", "arAngles", "arThreshHigh", "arThreshLow",
    "I_SigmaFactor", "I_BaselineWindowDeg", "iSamplesAcquired",
}
_OPCUA_MIN_MATCH = 4   # minimo variabili firma per considerarlo WeldFind

# Tutti gli scalari da leggere
_OPCUA_ALL_SCALARS = _PLC_SCALAR_PRE + _PLC_SCALAR_POST

# Tipo FB da cercare nell'XML OPC UA export
_OPCUA_FB_TYPE_PATTERN = "Fb954_WeldFindV47"


def opcua_parse_xml_weldfind(filepath, callback=None):
    """
    Parser VELOCE per file XML esportato dal server OPC UA S7-1500.

    Usa iterparse per gestire file 100+ MB senza caricare tutto in RAM.
    Cerca <UAObject> con HasTypeDefinition contenente 'Fb954_WeldFindV43',
    poi raccoglie le <UAVariable> figlie con i NodeId esatti.

    BUG-FIX: elem.clear() solo su UAObject/UAVariable processati
    (non su ogni elemento, altrimenti distrugge i figli Reference
    prima che il parent venga letto).
    """
    import xml.etree.ElementTree as ET

    file_size = os.path.getsize(filepath)

    def strip_ns(tag):
        """Rimuove namespace URI: {http://...}UAObject → UAObject"""
        return tag.split('}', 1)[1] if '}' in tag else tag

    def find_child(elem, child_tag_name):
        """Trova un figlio per nome, ignorando namespace XML."""
        for ch in elem:
            if strip_ns(ch.tag) == child_tag_name:
                return ch
        return None

    def get_ref_texts(elem, ref_type_name):
        """Estrae i testi dei Reference con un dato ReferenceType."""
        refs = find_child(elem, "References")
        if refs is None:
            return []
        results = []
        for ref in refs:
            if strip_ns(ref.tag) == "Reference":
                if ref.get("ReferenceType", "") == ref_type_name:
                    results.append(ref.text or "")
        return results

    # ── Passo 1: scansione iterativa ──
    weldfind_dbs = {}   # node_id → db_info
    all_variables = []  # lista di tutte le UAVariable trovate

    elem_count = 0
    context = ET.iterparse(filepath, events=("end",))

    for event, elem in context:
        elem_count += 1

        if callback and elem_count % 50000 == 0:
            pct = min(90, elem_count // 5000)
            callback(pct, f"Analisi XML... {elem_count} elementi")

        tag = strip_ns(elem.tag)

        if tag == "UAObject":
            # ── Controlla se è un DB WeldFind ──
            # Tutti i figli (References, DisplayName) sono intatti a questo punto
            node_id = elem.get("NodeId", "")   # ElementTree unescape &quot; → "
            browse_name = elem.get("BrowseName", "")
            parent_nid = elem.get("ParentNodeId", "")

            # Cerca HasTypeDefinition contenente il pattern FB
            type_refs = get_ref_texts(elem, "HasTypeDefinition")
            type_ref_match = ""
            for tr in type_refs:
                if _OPCUA_FB_TYPE_PATTERN in tr:
                    type_ref_match = tr
                    break

            if type_ref_match:
                name = browse_name.split(":", 1)[-1] if ":" in browse_name else browse_name
                weldfind_dbs[node_id] = {
                    "name": name,
                    "node_id": node_id,
                    "type_ref": type_ref_match,
                    "source": "xml",
                    "variables": {},
                    "var_count": 0,
                }

            # ★ Clear SOLO dopo aver processato (libera References e figli)
            elem.clear()

        elif tag == "UAVariable":
            # ── Raccogli info variabile ──
            var_node_id = elem.get("NodeId", "")
            var_datatype = elem.get("DataType", "")
            var_browse = elem.get("BrowseName", "")
            var_arr_dim = elem.get("ArrayDimensions", "")

            # Estrai DisplayName
            dn_elem = find_child(elem, "DisplayName")
            var_name = (dn_elem.text or "") if dn_elem is not None else ""
            if not var_name:
                var_name = var_browse.split(":", 1)[-1] if ":" in var_browse else var_browse

            if var_name and var_node_id:
                all_variables.append({
                    "name": var_name,
                    "node_id": var_node_id,
                    "datatype": var_datatype,
                    "array_dim": var_arr_dim,
                })

            # ★ Clear SOLO dopo aver processato
            elem.clear()

        # ★ NON fare clear su altri tag (Reference, DisplayName, ecc.)
        #    Vengono puliti quando il parent UAObject/UAVariable fa clear.

    # ── Passo 2: Associa variabili ai DB WeldFind ──
    if callback:
        callback(92, f"Associazione variabili ({len(all_variables)} vars, {len(weldfind_dbs)} DB)...")

    # Per ogni variabile, controlla se il suo NodeId inizia con un NodeId DB
    # es: ns=3;s="DbTestSaldCirc1"."I_LaserValue" inizia con ns=3;s="DbTestSaldCirc1"
    # Ordina i DB per lunghezza NodeId decrescente (match più specifico prima)
    db_nids_sorted = sorted(weldfind_dbs.keys(), key=len, reverse=True)

    for var in all_variables:
        vnid = var["node_id"]
        for db_nid in db_nids_sorted:
            if vnid.startswith(db_nid) and vnid != db_nid:
                weldfind_dbs[db_nid]["variables"][var["name"]] = {
                    "node_id": var["node_id"],
                    "datatype": var["datatype"],
                    "array_dim": var["array_dim"],
                }
                break

    # Aggiorna conteggi
    for db in weldfind_dbs.values():
        db["var_count"] = len(db["variables"])

    result = list(weldfind_dbs.values())

    if callback:
        callback(100, f"Trovati {len(result)} DB WeldFind, {sum(d['var_count'] for d in result)} variabili")

    return result


class OpcUaWeldFindReader:
    """Lettura DB WeldFind via OPC UA integrato nella CPU S7-1500."""

    def __init__(self, endpoint="opc.tcp://192.168.0.1:4840",
                 timeout=10.0, username=None, password=None):
        if not OPCUA_AVAILABLE:
            raise ImportError(
                "Libreria OPC UA non trovata!\n"
                "Installa con:  pip install opcua\n"
                "Oppure:        pip install asyncua")
        self.endpoint = endpoint
        self.timeout = timeout
        self.client = None
        self._connected = False
        self._username = username
        self._password = password
        self._ns_idx = None  # namespace index S7

    def connect(self):
        """Connette al server OPC UA. Ritorna dict info."""
        try:
            self.client = OpcClient(self.endpoint, timeout=self.timeout)
            if self._username and self._password:
                self.client.set_user(self._username)
                self.client.set_password(self._password)
            self.client.connect()
            self._connected = True
            ns_array = self.client.get_namespace_array()
            self._ns_idx = None
            for i, ns in enumerate(ns_array):
                if "siemens" in ns.lower() or "simatic" in ns.lower() or "s7" in ns.lower():
                    self._ns_idx = i;  break
            if self._ns_idx is None:
                self._ns_idx = 3  # default S7-1500
            server_name = "S7 OPC UA Server"
            try:
                server_name = self.client.get_server_node().get_display_name().Text
            except Exception:
                pass
            return {"endpoint": self.endpoint, "namespaces": ns_array,
                    "s7_namespace_idx": self._ns_idx, "server_name": server_name}
        except Exception as e:
            self._connected = False
            raise ConnectionError(
                f"Connessione OPC UA fallita: {e}\n\n"
                f"Verificare:\n"
                f"  1) Server OPC UA abilitato nella CPU\n"
                f"  2) Endpoint: {self.endpoint}\n"
                f"  3) Porta 4840 raggiungibile") from e

    def disconnect(self):
        if self.client and self._connected:
            try: self.client.disconnect()
            except Exception: pass
            self._connected = False

    @property
    def is_connected(self):
        return self._connected

    def browse_all_dbs(self):
        """Elenca tutti i DataBlock accessibili. Ritorna lista di dict."""
        if not self._connected:
            raise ConnectionError("Non connesso")
        dbs = []
        root = self.client.get_objects_node()
        # Cerca cartelle DataBlocks
        db_folders = self._find_nodes(root, [
            "DataBlocksGlobal", "DataBlocksInstance",
            "Data blocks", "Global-DBs"
        ], max_depth=3)
        if not db_folders:
            db_folders = [root]
        for folder in db_folders:
            try:
                for child in folder.get_children():
                    try:
                        name = child.get_display_name().Text
                        nc = child.get_node_class()
                        if nc == opcua_ua.NodeClass.Object:
                            dbs.append({"name": name,
                                        "node_id": str(child.nodeid),
                                        "node": child})
                    except Exception:
                        continue
            except Exception:
                continue
        return dbs

    def browse_weldfind_dbs(self, callback=None):
        """Trova DB di tipo WeldFind. Ritorna lista con info."""
        all_dbs = self.browse_all_dbs()
        wf_dbs = []
        for i, db in enumerate(all_dbs):
            if callback:
                callback(int((i+1)/max(len(all_dbs),1)*100),
                         f"Scansione {db['name']}...")
            try:
                child_names = set()
                children = db["node"].get_children()
                for ch in children:
                    try: child_names.add(ch.get_display_name().Text)
                    except Exception: continue
                matched = _OPCUA_WELDFIND_SIGNATURE & child_names
                if len(matched) >= _OPCUA_MIN_MATCH:
                    info = dict(db)
                    info["match_score"] = len(matched)
                    info["samples"] = 0
                    info["polarity"] = 0
                    info["state"] = 0
                    # Lettura rapida info
                    for ch in children:
                        try:
                            cn = ch.get_display_name().Text
                            if cn == "iSamplesAcquired":
                                info["samples"] = int(ch.get_value())
                            elif cn == "I_PeakPolarity":
                                info["polarity"] = int(ch.get_value())
                            elif cn == "iState":
                                info["state"] = int(ch.get_value())
                        except Exception: continue
                    wf_dbs.append(info)
            except Exception:
                continue
        return wf_dbs

    def read_weldfind_db(self, db_name, max_samples=2001, callback=None):
        """Legge un DB WeldFind completo. Ritorna dict compatibile col viewer."""
        if not self._connected:
            raise ConnectionError("Non connesso")
        result = {"scalars": {}, "arrays": {},
                  "raw_text": "",
                  "filename": f"OpcUA_{db_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"}

        db_node = self._get_db_node(db_name)
        if db_node is None:
            raise ValueError(
                f"DB '{db_name}' non trovato.\n"
                f"Verificare DB_Accessible_From_OPC_UA = TRUE")

        # Mappa figli
        child_map = {}
        for ch in db_node.get_children():
            try: child_map[ch.get_display_name().Text] = ch
            except Exception: continue

        total = len(_OPCUA_ALL_SCALARS) + len(_PLC_ARRAY_ORDER)
        step = 0

        # ── Scalari ──
        if callback: callback(0, "Lettura scalari...")
        for vn in _OPCUA_ALL_SCALARS:
            step += 1
            if vn in child_map:
                try:
                    raw = child_map[vn].get_value()
                    if vn in _PLC_BOOL_VARS:
                        result["scalars"][vn] = bool(raw)
                    elif vn in _PLC_INT_VARS:
                        result["scalars"][vn] = int(raw)
                    else:
                        result["scalars"][vn] = float(raw)
                except Exception:
                    if vn in _PLC_BOOL_VARS: result["scalars"][vn] = False
                    elif vn in _PLC_INT_VARS: result["scalars"][vn] = 0
                    else: result["scalars"][vn] = 0.0
            if callback and step % 10 == 0:
                callback(int(step/total*50), f"Scalari: {vn}")

        # ── Array ──
        n_samp = int(result["scalars"].get("iSamplesAcquired", 0))
        read_n = min(max(n_samp + 1, 1), max_samples)

        for ai, an in enumerate(_PLC_ARRAY_ORDER):
            if callback:
                callback(50 + int((ai+1)/len(_PLC_ARRAY_ORDER)*50),
                         f"Array {an}...")
            if an in child_map:
                arr = self._read_array(child_map[an], read_n, db_name)
                result["arrays"][an] = arr if arr else [0.0] * read_n
            else:
                result["arrays"][an] = [0.0] * read_n

        # Genera raw_text .db per compatibilità viewer
        result["raw_text"] = self._gen_db_text(result, db_name, max_samples)
        if callback: callback(100, "Completato!")
        return result

    def read_weldfind_db_xml(self, xml_db_info, max_samples=2001, callback=None):
        """
        Lettura VELOCE con NodeId pre-estratti dal file XML OPC UA.
        
        Salta completamente il browse: accede alle variabili per NodeId diretto.
        
        Args:
            xml_db_info: dict da opcua_parse_xml_weldfind() con chiave 'variables'
            max_samples: max campioni array
            callback: callback(pct, msg) per progresso
            
        Returns:
            dict compatibile col viewer (scalars, arrays, raw_text, filename)
        """
        if not self._connected:
            raise ConnectionError("Non connesso")
        db_name = xml_db_info["name"]
        xml_vars = xml_db_info.get("variables", {})
        
        result = {"scalars": {}, "arrays": {},
                  "raw_text": "",
                  "filename": f"OpcUA_{db_name}_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"}

        total = len(_OPCUA_ALL_SCALARS) + len(_PLC_ARRAY_ORDER)
        step = 0

        # ── Scalari: lettura diretta per NodeId ──
        if callback: callback(0, "Lettura scalari (XML-guided)...")
        for vn in _OPCUA_ALL_SCALARS:
            step += 1
            if vn in xml_vars:
                nid_str = xml_vars[vn]["node_id"]
                try:
                    node = self.client.get_node(nid_str)
                    raw = node.get_value()
                    if vn in _PLC_BOOL_VARS:    result["scalars"][vn] = bool(raw)
                    elif vn in _PLC_INT_VARS:   result["scalars"][vn] = int(raw)
                    else:                       result["scalars"][vn] = float(raw)
                except Exception:
                    if vn in _PLC_BOOL_VARS:    result["scalars"][vn] = False
                    elif vn in _PLC_INT_VARS:   result["scalars"][vn] = 0
                    else:                       result["scalars"][vn] = 0.0
            if callback and step % 15 == 0:
                callback(int(step / total * 40), f"Scalari... {vn}")

        # ── Array: lettura per NodeId con indice ──
        n_samp = int(result["scalars"].get("iSamplesAcquired", 0))
        read_n = min(max(n_samp + 1, 1), max_samples)

        for ai, an in enumerate(_PLC_ARRAY_ORDER):
            if callback:
                callback(40 + int((ai + 1) / len(_PLC_ARRAY_ORDER) * 55),
                         f"Array {an} ({read_n} el.)...")
            
            if an in xml_vars:
                # Il NodeId dell'array è noto, prova lettura nodo array
                arr_nid = xml_vars[an]["node_id"]
                try:
                    arr_node = self.client.get_node(arr_nid)
                    arr = self._read_array(arr_node, read_n, db_name)
                    if arr:
                        result["arrays"][an] = arr
                        continue
                except Exception:
                    pass
                
                # Fallback: lettura per indice usando formato NodeId dall'XML
                # es: ns=3;s="DbTestSaldCirc1"."arSamples"[0]
                # Costruiamo il pattern dal NodeId base della variabile
                arr = [0.0] * read_n
                base_nid = arr_nid  # es: ns=3;s="DbTestSaldCirc1"."arSamples"
                batch = 50
                for start in range(0, read_n, batch):
                    end = min(start + batch, read_n)
                    bnodes = []
                    for i in range(start, end):
                        try:
                            nid = f'{base_nid}[{i}]'
                            bnodes.append((i, self.client.get_node(nid)))
                        except Exception:
                            continue
                    if bnodes:
                        try:
                            vals = self.client.get_values([n for _, n in bnodes])
                            for (i, _), v in zip(bnodes, vals):
                                arr[i] = float(v)
                        except Exception:
                            for i, n in bnodes:
                                try: arr[i] = float(n.get_value())
                                except Exception: pass
                result["arrays"][an] = arr
            else:
                result["arrays"][an] = [0.0] * read_n

        result["raw_text"] = self._gen_db_text(result, db_name, max_samples)
        if callback: callback(100, "Completato!")
        return result

    # ── Metodi interni ────────────────────────────────────────

    def _get_db_node(self, db_name):
        ns = self._ns_idx
        # Tentativo 1: con virgolette
        for fmt in [f'ns={ns};s="{db_name}"', f'ns={ns};s={db_name}']:
            try:
                node = self.client.get_node(fmt)
                _ = node.get_display_name()
                return node
            except Exception: pass
        # Tentativo 2: browse
        try:
            for db in self.browse_all_dbs():
                if db["name"] == db_name:
                    return db["node"]
        except Exception: pass
        return None

    def _read_array(self, arr_node, count, db_name=""):
        """Legge array OPC UA con fallback multipli."""
        # 1: Array intero
        try:
            val = arr_node.get_value()
            if isinstance(val, (list, tuple)):
                arr = [float(v) for v in val[:count]]
                return arr + [0.0] * max(0, count - len(arr))
        except Exception: pass

        # 2: Figli indicizzati
        try:
            children = arr_node.get_children()
            if children:
                indexed = []
                for ch in children:
                    try:
                        nm = ch.get_display_name().Text.strip("[]")
                        indexed.append((int(nm), ch))
                    except Exception: continue
                indexed.sort(key=lambda x: x[0])
                nodes = [n for i, n in indexed if i < count]
                idxs = [i for i, n in indexed if i < count]
                if nodes:
                    arr = [0.0] * count
                    try:
                        vals = self.client.get_values(nodes)
                        for idx, v in zip(idxs, vals):
                            arr[idx] = float(v)
                    except Exception:
                        for idx, n in zip(idxs, nodes):
                            try: arr[idx] = float(n.get_value())
                            except Exception: pass
                    return arr
        except Exception: pass

        # 3: Accesso per NodeId diretto
        try:
            an = arr_node.get_display_name().Text
            ns = self._ns_idx
            arr = [0.0] * count
            batch = 50
            for start in range(0, count, batch):
                end = min(start + batch, count)
                bnodes = []
                for i in range(start, end):
                    for fmt in [f'ns={ns};s="{db_name}"."{an}"[{i}]',
                                f'ns={ns};s="{db_name}".{an}[{i}]']:
                        try:
                            bnodes.append((i, self.client.get_node(fmt)))
                            break
                        except Exception: continue
                if bnodes:
                    try:
                        vals = self.client.get_values([n for _, n in bnodes])
                        for (i, _), v in zip(bnodes, vals):
                            arr[i] = float(v)
                    except Exception:
                        for i, n in bnodes:
                            try: arr[i] = float(n.get_value())
                            except Exception: pass
            return arr
        except Exception: pass
        return None

    def _find_nodes(self, root, names, max_depth=3, _d=0):
        found = []
        if _d > max_depth: return found
        try:
            for ch in root.get_children():
                try:
                    n = ch.get_display_name().Text
                    if n in names: found.append(ch)
                    elif _d < max_depth:
                        found.extend(self._find_nodes(ch, names, max_depth, _d+1))
                except Exception: continue
        except Exception: pass
        return found

    def _gen_db_text(self, data, db_name, max_samples=2001):
        """Genera testo .db compatibile TIA Portal."""
        lines = [
            f'DATA_BLOCK "{db_name}"',
            "{ DB_Accessible_From_OPC_UA := 'TRUE' ;",
            " S7_Optimized_Access := 'FALSE' }",
            "VERSION : 0.1", "NON_RETAIN",
            f'"{PLC_FB_TYPE_NAME}"', "", "BEGIN",
        ]
        sc = data["scalars"];  ar = data["arrays"]
        def fmt(n, v):
            if n in _PLC_BOOL_VARS: return "TRUE" if v else "FALSE"
            if n in _PLC_INT_VARS: return str(int(v))
            s = f"{v:.7g}"
            if '.' not in s and 'e' not in s.lower(): s += '.0'
            return s
        for n in _PLC_SCALAR_PRE:
            if n in sc: lines.append(f"   {n} := {fmt(n, sc[n])};")
        for an in _PLC_ARRAY_ORDER:
            if an in ar:
                a = ar[an]
                for i in range(min(len(a), max_samples)):
                    v = a[i] if i < len(a) else 0.0
                    s = f"{v:.7g}"
                    if '.' not in s and 'e' not in s.lower(): s += '.0'
                    lines.append(f"   {an}[{i}] := {s};")
        for n in _PLC_SCALAR_POST:
            if n in sc: lines.append(f"   {n} := {fmt(n, sc[n])};")
        lines += ["", "END_DATA_BLOCK", ""]
        return '\n'.join(lines)


# ──────────────────────────────────────────────────────────────
#  APP
# ──────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════
#  STAT WORKER — funzione top-level (picklable per ProcessPoolExecutor)
#  Riceve path + params, simula, restituisce (found, snr, peak_dev, det_angle)
# ══════════════════════════════════════════════════════════════════

def _set_worker_low_priority():
    """Abbassa la priorità del processo worker."""
    import sys, os
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except Exception:
        pass


# Variabili globali del worker — popolate dall'initializer, zero IPC per task
_WORKER_FILES = None   # list of (raw_s_f32, raw_a_f32, fixed_kw)


def _worker_init(file_payloads, priority=True):
    """Initializer ProcessPoolExecutor: carica tutti i file UNA SOLA VOLTA.
    Ogni processo worker mantiene i dati in memoria locale.
    Zero I/O disco durante le simulazioni."""
    global _WORKER_FILES
    if priority:
        _set_worker_low_priority()
    _WORKER_FILES = file_payloads   # lista già pronta, passata via pickle una volta


def _stat_sim_worker_v2(fi: int, combo: tuple, keys: tuple) -> tuple:
    """Worker ottimizzato: riceve solo indici e valori numerici (55 bytes IPC).
    I dati dei file sono già in memoria locale dal initializer.
    Usa float32 (replica PLC). Se il cluster è borderline, ritenta con float64
    per recuperare casi in cui arrotondamento float32 perde la detection."""
    global _WORKER_FILES
    try:
        payload = _WORKER_FILES[fi]
        raw_s, raw_a, fixed_kw = payload[0], payload[1], payload[2]
        has_th = payload[7] if len(payload) > 7 else False
        params = dict(zip(keys, combo))
        params.update(fixed_kw)

        kw = dict(
            skip_filter      = True,
            use_fast_numpy   = True,
            window_deg       = float(params.get("window_deg",      10.0)),
            sigma_factor     = float(params.get("sigma_factor",     3.0)),
            min_abs_dev      = float(params.get("min_abs_dev",      1.5)),
            hyst_sigmas      = float(params.get("hyst_sigmas",      0.5)),
            min_consecutive  = int(params.get("min_consecutive",    3)),
            max_consecutive  = int(params.get("max_consecutive",    60)),
            stop_on_weld     = bool(int(params.get("stop_on_weld",  1))),
            peak_polarity    = int(params.get("peak_polarity",      0)),
            # *** v4.6 ***
            adapt_baseline_enable = bool(params.get("adapt_baseline_enable", False)),
            adapt_baseline_offset = float(params.get("adapt_baseline_offset", 3.0)),
            flat_wait_enable  = bool(params.get("flat_wait_enable",  False)),
            flat_wait_samples = int(params.get("flat_wait_samples", 5)),
            flat_wait_toll    = float(params.get("flat_wait_toll",   0.5)),
            detection_start_deg = float(params.get("detection_start_deg", 0.0)),  # *** v4.9 ***
        )

        # *** v4.3.76 BUGFIX *** NON usare mai use_db_thresholds nel grid search.
        # Le soglie PLC sono calcolate con i parametri ORIGINALI del DB:
        # passarle al worker fisserebbe le soglie indipendentemente dai parametri
        # di sweep (sigma_factor, window_deg, min_abs_dev, hyst_sigmas non avrebbero
        # alcun effetto) -> tutte le combinazioni darebbero la stessa % trovati.
        # Le soglie PLC si usano solo nel simulatore singolo (modalita VERIFICA).
        # (has_th mantenuto nel payload per usi futuri ma non usato qui)

        # Float32 — replica esatta aritmetica REAL PLC
        res = simulate_plc_realtime(raw_s, raw_a, use_float32=True, **kw)
        found = bool(res.get("weld_found", False))

        snr      = float(res.get("peak_sigmas",  0.0)) if found else 0.0
        peak_dev = abs(float(res.get("peak_deviation", 0.0))) if found else 0.0
        det_ang  = float(res.get("detection_angle", 0.0)) if found else 0.0
        return (found, snr, peak_dev, det_ang)
    except Exception:
        return (False, 0.0, 0.0, 0.0)


def _stat_sim_worker(filepath: str, params: dict):
    """Worker legacy — tenuto per compatibilità, non più usato dal grid search."""
    try:
        data  = parse_db_file(filepath)
        sc    = data["scalars"]
        ar    = data["arrays"]
        n     = int(sc.get("iSamplesAcquired", 0)) or len(ar.get("arSamples", []))
        if n < 3:
            return (False, 0.0, 0.0)
        raw_s = ar.get("arSamples", [])[:n]
        raw_a = ar.get("arAngles",  [])[:n]
        kw = dict(
            skip_filter=True, use_fast_numpy=True, use_float32=True,
            window_deg      = float(params.get("window_deg",      10.0)),
            sigma_factor    = float(params.get("sigma_factor",     3.0)),
            min_abs_dev     = float(params.get("min_abs_dev",      1.5)),
            hyst_sigmas     = float(params.get("hyst_sigmas",      0.5)),
            min_consecutive = int(params.get("min_consecutive",    3)),
            max_consecutive = int(params.get("max_consecutive",    60)),
            stop_on_weld    = bool(int(params.get("stop_on_weld",  1))),
            peak_polarity   = int(params.get("peak_polarity",      0)),
        )
        res = simulate_plc_realtime(raw_s, raw_a, **kw)
        found    = bool(res.get("weld_found", False))
        snr      = float(res.get("peak_sigmas",  0.0)) if found else 0.0
        peak_dev = abs(float(res.get("peak_deviation", 0.0))) if found else 0.0
        return (found, snr, peak_dev)
    except Exception:
        return (False, 0.0, 0.0)




# ══════════════════════════════════════════════════════════════════
#  SQLITE — funzioni modulo-level (Auto Export, Import, Query)
# ══════════════════════════════════════════════════════════════════

def weld_sqlite_init(db_path: str):
    """Apre/crea SQLite con schema acquisitions. WAL mode per scrittura+lettura simultanea."""
    import sqlite3
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS acquisitions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp    TEXT    NOT NULL,
        db_number    INTEGER NOT NULL DEFAULT 0,
        filename     TEXT,
        weld_found   INTEGER NOT NULL DEFAULT 0,
        det_angle    REAL,    det_sample INTEGER,
        peak_value   REAL,    peak_dev   REAL,    peak_sigmas REAL,
        n_samples    INTEGER,
        sigma_factor REAL,    min_abs_dev REAL,   hyst_sigmas REAL,
        window_deg   REAL,    min_consec  INTEGER, max_consec  INTEGER,
        polarity     INTEGER,
        raw_samples  BLOB,    raw_angles  BLOB,
        scalars_json TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts    ON acquisitions(timestamp)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_db    ON acquisitions(db_number)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_found ON acquisitions(weld_found)")
    con.commit()
    return con


def weld_sqlite_insert(con, db_number: int, decoded: dict,
                       filename: str = "", max_samples: int = 2001) -> int:
    """Inserisce una acquisizione nel database SQLite. Ritorna row id."""
    import sqlite3, json as _json, struct as _struct, datetime as _dt
    sc  = decoded.get("scalars", {})
    ar  = decoded.get("arrays",  {})
    n   = int(sc.get("iSamplesAcquired", 0)) or len(ar.get("arSamples", []))
    ts  = decoded.get("timestamp", _dt.datetime.now().isoformat(sep=" ", timespec="seconds"))
    raw_s = ar.get("arSamples", [])[:n]
    raw_a = ar.get("arAngles",  [])[:n]
    blob_s = _struct.pack(f"<{len(raw_s)}f", *raw_s) if raw_s else b""
    blob_a = _struct.pack(f"<{len(raw_a)}f", *raw_a) if raw_a else b""

    # Criterio saldatura trovata: prima chiavi esplicite, poi logica cluster
    _bwf = sc.get("bWeldFound", sc.get("IO_RicercaSaldatura.Trovata", None))
    if _bwf is not None:
        weld_found = int(bool(_bwf))
    else:
        _cv = int(sc.get("iClustersValid", 0))
        _cc = int(sc.get("iConsecutiveCount", 0))
        _mc = max(1, int(sc.get("I_MinConsecutive", 1)))
        weld_found = 1 if (_cv >= 1 and _cc >= _mc) else 0
    scalars_json = _json.dumps(
        {k: float(v) if isinstance(v, (int, float)) else str(v) for k,v in sc.items()},
        ensure_ascii=False)

    cur = con.execute("""INSERT INTO acquisitions
        (timestamp,db_number,filename,weld_found,det_angle,det_sample,peak_value,
         peak_dev,peak_sigmas,n_samples,sigma_factor,min_abs_dev,hyst_sigmas,
         window_deg,min_consec,max_consec,polarity,raw_samples,raw_angles,scalars_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, db_number, filename, weld_found,
         float(sc.get("rDetectedAtAngle",0)), int(sc.get("iDetectedAtSample",-1)),
         float(sc.get("rPeakValue",0)),       float(sc.get("rPeakDeviation",0)),
         float(sc.get("rPeakSigmas",0)),      n,
         float(sc.get("I_SigmaFactor",3.0)),  float(sc.get("I_MinAbsDeviation",1.5)),
         float(sc.get("I_HysteresisSigmas",0.5)), float(sc.get("I_BaselineWindowDeg",10.0)),
         int(sc.get("I_MinConsecutive",3)),   int(sc.get("I_MaxConsecutive",60)),
         int(sc.get("I_PeakPolarity",0)),     blob_s, blob_a, scalars_json))
    con.commit()
    return cur.lastrowid


def weld_sqlite_load_row(row: tuple) -> dict:
    """Converte una riga SELECT * dal DB in formato compatibile col viewer."""
    import struct as _struct, json as _json
    (row_id, ts, db_number, filename, weld_found, det_angle, det_sample,
     peak_value, peak_dev, peak_sigmas, n_samples,
     sigma_factor, min_abs_dev, hyst_sigmas, window_deg, min_consec, max_consec,
     polarity, blob_s, blob_a, scalars_json) = row
    n_s = len(blob_s)//4 if blob_s else 0
    n_a = len(blob_a)//4 if blob_a else 0
    raw_s = list(_struct.unpack(f"<{n_s}f", blob_s)) if blob_s else []
    raw_a = list(_struct.unpack(f"<{n_a}f", blob_a)) if blob_a else []
    scalars = _json.loads(scalars_json) if scalars_json else {}
    scalars.setdefault("iSamplesAcquired", n_samples or n_s)
    return {"scalars": scalars,
            "arrays":  {"arSamples": raw_s, "arAngles": raw_a},
            "raw_text": "", "filename": filename or f"SQLite_DB{db_number}_{ts[:10]}.db",
            "sqlite_row_id": row_id, "sqlite_db_number": db_number}


class WeldViewerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"WeldDetector DB Analyzer  {APP_RELEASE}  —  SCL v4.7")
        self.geometry("1380x880")
        self.minsize(1100, 680)
        self.configure(bg=DARK_BG)
        self.db_data = None;  self.comp_data = None;  self.sim_result = None
        self._opcua_last_data = None;  self._opcua_last_dbname = None
        self._style();  self._build_ui()
        # Controlla librerie all'avvio (non bloccante)
        self.after(500, self._startup_check_libs)
        # Cleanup alla chiusura
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Pulizia alla chiusura: ferma tutti i servizi e uccide i processi figli."""
        # 1. Shutdown graceful dei servizi
        if hasattr(self, '_autoexp_running') and self._autoexp_running:
            self._autoexp_stop()
        if hasattr(self, '_rt_running') and self._rt_running:
            self._rt_stop()
        # 2. Shutdown executor grid-search (cancel futures + termina worker)
        if hasattr(self, '_stat_executor') and self._stat_executor:
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                try: self._stat_executor.shutdown(wait=False, cancel_futures=True)
                except (TypeError, OSError, BrokenPipeError): pass
                try: self._stat_executor.shutdown(wait=False)
                except (OSError, BrokenPipeError): pass
            self._stat_executor = None
        # 3. Distruggi la finestra Tk
        try:
            self.destroy()
        except Exception:
            pass
        # 4. Kill dell'intero albero di processi — nessun zombie nel Task Manager
        _kill_process_tree()

    # ── CONTROLLO LIBRERIE ALL'AVVIO ────────────────────────
    def _startup_check_libs(self):
        """Verifica all'avvio solo le librerie OBBLIGATORIE (numpy, matplotlib).
        Il check OPC UA/snap7 avviene quando l'utente abilita il flag."""
        missing = []

        try:
            import numpy
        except ImportError:
            missing.append("numpy")

        try:
            import matplotlib
        except ImportError:
            missing.append("matplotlib")

        if not missing:
            return

        msg = (f"Librerie obbligatorie mancanti: {', '.join(missing)}\n\n"
               f"L'applicazione non funzionerà correttamente.\n\n"
               f"Vuoi installarle ora?")
        if messagebox.askyesno("Librerie Mancanti", msg, parent=self):
            self._install_libs(missing)

    def _install_libs(self, lib_names):
        """Installa librerie via pip (chiamata dallo startup check)."""
        import subprocess, sys
        for lib in lib_names:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", lib],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    messagebox.showinfo("Installazione",
                        f"✓ {lib} installata!\n\nRiavvia l'applicazione per usarla.",
                        parent=self)
                else:
                    err = result.stderr.strip().split('\n')[-1] if result.stderr else "Errore sconosciuto"
                    messagebox.showerror("Errore Installazione",
                        f"Errore installando {lib}:\n{err}\n\n"
                        f"Prova manualmente:\n{sys.executable} -m pip install {lib}",
                        parent=self)
            except Exception as e:
                messagebox.showerror("Errore", f"Errore: {e}", parent=self)

    # ── STYLE ─────────────────────────────────────────────────
    def _style(self):
        st = ttk.Style(self);  st.theme_use("clam")
        base = dict(background=DARK_BG, foreground=TEXT_CLR, fieldbackground=ENTRY_BG,
                    troughcolor=PANEL_BG, bordercolor=BORDER_CLR,
                    lightcolor=BORDER_CLR, darkcolor=BORDER_CLR, font=("Consolas", 10))
        st.configure(".", **base)
        st.configure("TFrame", background=DARK_BG)
        st.configure("TLabel", background=DARK_BG, foreground=TEXT_CLR)
        st.configure("Muted.TLabel", background=DARK_BG, foreground=MUTED_CLR, font=("Consolas", 9))
        st.configure("Title.TLabel", background=DARK_BG, foreground=ACCENT, font=("Consolas", 11, "bold"))
        st.configure("Result.TLabel", background=PANEL_BG, foreground=ACCENT, font=("Consolas", 10))
        st.configure("SimResult.TLabel", background=PANEL_BG, foreground=SIM_CLR, font=("Consolas", 10, "bold"))
        st.configure("DetResult.TLabel", background=PANEL_BG, foreground=DET_CLR, font=("Consolas", 10, "bold"))
        st.configure("UdtResult.TLabel", background=PANEL_BG, foreground=UDT_CLR, font=("Consolas", 10, "bold"))
        st.configure("PlcResult.TLabel", background=PANEL_BG, foreground=PLC_CLR, font=("Consolas", 10, "bold"))
        for name, bg, fg in [("Accent", ACCENT, DARK_BG), ("Sim", SIM_CLR, DARK_BG),
                               ("Plc", PLC_CLR, DARK_BG), ("Opcua", OPCUA_CLR, DARK_BG)]:
            st.configure(f"{name}.TButton", background=bg, foreground=fg,
                         font=("Consolas", 10, "bold"), padding=(10, 5))
            hover = {"Accent": "#88c8ff", "Sim": "#d4a7ff", "Plc": "#f5a862", "Opcua": "#b07cc8"}
            st.map(f"{name}.TButton", background=[("active", hover.get(name, "#88c8ff"))],
                   foreground=[("active", DARK_BG)])
        st.configure("TButton", background=ENTRY_BG, foreground=TEXT_CLR,
                     bordercolor=BORDER_CLR, padding=(8, 4))
        st.map("TButton", background=[("active", ACCENT)], foreground=[("active", DARK_BG)])
        st.configure("TNotebook", background=DARK_BG, bordercolor=BORDER_CLR)
        st.configure("TNotebook.Tab", background=PANEL_BG, foreground=MUTED_CLR,
                     padding=(12, 4), bordercolor=BORDER_CLR)
        st.map("TNotebook.Tab", background=[("selected", DARK_BG)], foreground=[("selected", ACCENT)])
        st.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_CLR, insertcolor=TEXT_CLR)
        st.configure("TLabelframe", background=DARK_BG, foreground=MUTED_CLR, bordercolor=BORDER_CLR)
        st.configure("TLabelframe.Label", background=DARK_BG, foreground=MUTED_CLR)
        # Treeview: sfondo scuro per TUTTE le tabelle (inclusa OPC UA)
        st.configure("Treeview",
                     background=ENTRY_BG,
                     foreground=TEXT_CLR,
                     fieldbackground=ENTRY_BG,
                     rowheight=22,
                     bordercolor=BORDER_CLR,
                     font=("Consolas", 9))
        st.configure("Treeview.Heading",
                     background=PANEL_BG,
                     foreground=ACCENT,
                     relief="flat",
                     font=("Consolas", 9, "bold"))
        st.map("Treeview",
               background=[("selected", ACCENT)],
               foreground=[("selected", DARK_BG)])
        st.map("Treeview.Heading",
               background=[("active", BORDER_CLR)],
               foreground=[("active", ACCENT)])
        # Combobox: sfondo e testo espliciti (fix Windows con tema clam)
        st.configure("TCombobox",
                     fieldbackground=ENTRY_BG, background=ENTRY_BG,
                     foreground=TEXT_CLR, selectbackground=ACCENT,
                     selectforeground=DARK_BG, insertcolor=TEXT_CLR,
                     arrowcolor=TEXT_CLR, bordercolor=BORDER_CLR)
        st.map("TCombobox",
               fieldbackground=[("readonly", ENTRY_BG), ("disabled", PANEL_BG)],
               foreground=[("readonly", TEXT_CLR), ("disabled", MUTED_CLR)],
               background=[("readonly", ENTRY_BG), ("active", PANEL_BG)],
               selectbackground=[("readonly", ACCENT)],
               selectforeground=[("readonly", DARK_BG)])
        # Scrollbar stile dark
        st.configure("TScrollbar", background=PANEL_BG, troughcolor=DARK_BG,
                     arrowcolor=MUTED_CLR, bordercolor=BORDER_CLR)
        st.map("TScrollbar", background=[("active", BORDER_CLR)])
        # Progressbar
        st.configure("TProgressbar", troughcolor=PANEL_BG, background=ACCENT,
                     bordercolor=BORDER_CLR)
        # Radiobutton e Checkbutton ttk
        st.configure("TRadiobutton", background=DARK_BG, foreground=TEXT_CLR)
        st.map("TRadiobutton",
               background=[("active", PANEL_BG)],
               foreground=[("active", ACCENT), ("disabled", MUTED_CLR)])
        st.configure("TCheckbutton", background=DARK_BG, foreground=TEXT_CLR,
                     indicatorcolor="#1f6feb", indicatorrelief="flat")
        st.map("TCheckbutton",
               background=[("active", PANEL_BG)],
               foreground=[("active", ACCENT), ("disabled", MUTED_CLR)],
               indicatorcolor=[("selected", "#1f6feb"), ("!selected", "#21262d")])
        # Combobox leggibile su Windows
        st.configure("TCombobox",
                     fieldbackground=ENTRY_BG, background=ENTRY_BG,
                     foreground=TEXT_CLR, selectbackground=ACCENT,
                     selectforeground=DARK_BG, arrowcolor=TEXT_CLR)
        st.map("TCombobox",
               fieldbackground=[("readonly", ENTRY_BG), ("disabled", DARK_BG)],
               foreground=[("readonly", TEXT_CLR), ("disabled", MUTED_CLR)])

    # ── LAYOUT ────────────────────────────────────────────────
    def _build_ui(self):
        # ── Barra superiore ──
        top = ttk.Frame(self);  top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text=f"◈ WELD DETECTOR  DB ANALYZER",
                  style="Title.TLabel").pack(side="left")
        self._lbl_release = tk.Label(top, text=APP_RELEASE,
            font=("Consolas", 9), fg="#58a6ff", bg=DARK_BG,
            relief="flat", padx=6, pady=1)
        self._lbl_release.pack(side="left", padx=(8, 0))

        # ── Indicatore connessione (visibile quando attiva) ──
        self._lbl_conn = tk.Label(top, text="", font=("Consolas", 9, "bold"),
            fg=DARK_BG, bg=DARK_BG, padx=6, pady=1)
        self._lbl_conn.pack(side="left", padx=(12, 0))

        # ── Bottoni sinistra: Manuale + Pausa Viewer ──
        ttk.Button(top, text="📖  Manuale",
                   command=self._open_manual).pack(side="left", padx=(12, 2))
        self._viewer_paused = False
        self._btn_pause = tk.Button(top, text="⏸ Pausa Viewer", font=("Consolas", 9),
            bg=ENTRY_BG, fg=MUTED_CLR, relief="flat", padx=8, pady=2,
            command=self._toggle_viewer_pause)
        self._btn_pause.pack(side="left", padx=2)

        # ── Bottoni destra: file ──
        ttk.Button(top, text="📂  Apri file .db", style="Accent.TButton",
                   command=self._open_file).pack(side="right", padx=4)
        ttk.Button(top, text="🗄  Da SQLite", style="Sim.TButton",
                   command=self._open_from_sqlite).pack(side="right", padx=2)
        self.lbl_file = ttk.Label(top, text="Nessun file caricato", style="Muted.TLabel")
        self.lbl_file.pack(side="right", padx=10)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=6)

        # ── Splitter verticale: contenuto sopra, log sotto ──
        vpane = ttk.PanedWindow(self, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # ── Parte superiore: PanedWindow orizzontale (left + right) ──
        upper = ttk.Frame(vpane)
        vpane.add(upper, weight=4)

        main = ttk.PanedWindow(upper, orient="horizontal")
        main.pack(fill="both", expand=True)
        self._main_pane = main
        left = ttk.Frame(main, width=400);  main.add(left, weight=0)
        self._left_frame = left
        self._build_left(left)
        right = ttk.Frame(main);  main.add(right, weight=1)
        self._build_right(right)

        # ── Parte inferiore: Log Panel ──
        log_frame = ttk.Frame(vpane)
        vpane.add(log_frame, weight=1)

        log_header = ttk.Frame(log_frame)
        log_header.pack(fill="x")
        tk.Label(log_header, text="📋 Log Applicazione", font=("Consolas", 9, "bold"),
                 bg=DARK_BG, fg=MUTED_CLR).pack(side="left", padx=4)
        tk.Button(log_header, text="🗑 Pulisci", font=("Consolas", 8),
                  bg=ENTRY_BG, fg=MUTED_CLR, relief="flat",
                  command=self._clear_app_log).pack(side="right", padx=4)

        log_sb = ttk.Scrollbar(log_frame)
        log_sb.pack(side="right", fill="y")
        self._app_log = tk.Text(log_frame, bg="#0a0e14", fg=TEXT_CLR,
                                 font=("Consolas", 9), wrap="word", height=3,
                                 yscrollcommand=log_sb.set,
                                 insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self._app_log.pack(fill="both", expand=True)
        log_sb.config(command=self._app_log.yview)

        self._app_log.tag_config("ok",   foreground=OK_CLR)
        self._app_log.tag_config("err",  foreground=WELD_CLR)
        self._app_log.tag_config("info", foreground=ACCENT)
        self._app_log.tag_config("warn", foreground=WARN_CLR)
        self._app_log.tag_config("dim",  foreground=MUTED_CLR)

        self.app_log(f"═══ WeldDetector DB Analyzer {APP_RELEASE} avviato ═══", "info")

    # ── LOG APPLICAZIONE ─────────────────────────────────────
    def app_log(self, msg, tag=None):
        """Log centralizzato visibile nel pannello inferiore."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._app_log.insert("end", f"[{ts}] ", "dim")
        self._app_log.insert("end", f"{msg}\n", tag if tag else ())
        self._app_log.see("end")
        self.update_idletasks()

    def _run_in_thread(self, fn, on_done=None, on_error=None,
                       status_var=None, status_msg="In corso..."):
        """Esegue fn() in un thread daemon — UI sempre reattiva.
        on_done(result) e on_error(exc) vengono chiamati nel thread UI via after(0)."""
        import threading

        if status_var:
            try: status_var.set(status_msg)
            except Exception: pass

        def _worker():
            try:
                result = fn()
                if on_done:
                    self.after(0, lambda r=result: on_done(r))
            except Exception as exc:
                if on_error:
                    self.after(0, lambda e=exc: on_error(e))
                else:
                    self.after(0, lambda e=exc:
                        messagebox.showerror("Errore", str(e)))
            finally:
                if status_var:
                    self.after(0, lambda: status_var.set(""))

        t = threading.Thread(target=_worker, daemon=True,
                             name=getattr(fn, '__name__', 'task'))
        t.start()
        return t

    def _clear_app_log(self):
        self._app_log.delete("1.0", "end")
        self.app_log("Log pulito.", "dim")

    def _toggle_viewer_pause(self):
        """Alterna pausa viewer. Auto Export continua a salvare file."""
        self._viewer_paused = not self._viewer_paused
        if self._viewer_paused:
            self._btn_pause.config(text="▶ Riprendi Viewer", bg=WARN_CLR, fg=DARK_BG)
        else:
            self._btn_pause.config(text="⏸ Pausa Viewer", bg=ENTRY_BG, fg=MUTED_CLR)

    def _update_conn_indicator(self, mode=None):
        """Aggiorna indicatore connessione nella barra superiore.
        mode: None=disconnesso, 'snap7'=PLC, 'opcua'=OPC UA, 'autoexp'=Auto Export attivo"""
        if mode == "autoexp":
            self._lbl_conn.config(text="● AUTO EXPORT ATTIVO", bg="#1a3a1a", fg=OK_CLR)
            self.title(f"WeldDetector DB Analyzer  {APP_RELEASE}  ●  AUTO EXPORT")
        elif mode == "realtime":
            self._lbl_conn.config(text="● RT MONITOR", bg="#0d2a2a", fg="#4fc3f7")
            self.title(f"WeldDetector DB Analyzer  {APP_RELEASE}  ●  RT MONITOR")
        elif mode == "snap7":
            self._lbl_conn.config(text="● PLC Connesso", bg="#1a2a3a", fg=PLC_CLR)
        elif mode == "opcua":
            self._lbl_conn.config(text="● OPC UA", bg="#2a1a3a", fg=OPCUA_CLR)
        else:
            self._lbl_conn.config(text="", bg=DARK_BG, fg=DARK_BG)
            self.title(f"WeldDetector DB Analyzer  {APP_RELEASE}  —  SCL v4.7")

    # ── PANNELLO SINISTRO CON SCROLLBAR ─────────────────────────────────────
    def _build_left(self, p):
        # Canvas scrollabile per il pannello sinistro
        canvas = tk.Canvas(p, bg=DARK_BG, highlightthickness=0, width=280)
        scrollbar = ttk.Scrollbar(p, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Scroll con rotella del mouse
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        
        # === CONTENUTO PANNELLO ===
        sf = scrollable_frame  # alias
        
        # Status file
        self._status_var = tk.StringVar(value="Nessun file caricato")
        self._status_lbl = tk.Label(sf, textvariable=self._status_var,
                                    bg=PANEL_BG, fg=MUTED_CLR, font=("Consolas", 9),
                                    anchor="w", wraplength=260, justify="left", pady=4, padx=6)
        self._status_lbl.pack(fill="x", pady=(0, 4), padx=2)

        # ═══════════════════════════════════════════════════════════
        # RISULTATO PRINCIPALE (sempre visibile, compatto)
        # ═══════════════════════════════════════════════════════════
        rf = ttk.LabelFrame(sf, text=" RISULTATO ", padding=4)
        rf.pack(fill="x", pady=(0, 4), padx=2)
        
        self._result_vars = {}
        for key, lbl in [
            ("iDetectedAtSample",  "Rilevato #"),
            ("rDetectedAtAngle",   "Angolo (deg)"),
            ("rPeakValue",         "Picco"),
            ("rPeakSigmas",        "SNR (sigma)"),
            ("iSamplesAcquired",   "Campioni"),
        ]:
            row = ttk.Frame(rf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._result_vars[key] = v
            ttk.Label(row, textvariable=v, style="DetResult.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # PARAMETRI BASELINE (read-only, auto dal DB)
        # ═══════════════════════════════════════════════════════════
        pf = ttk.LabelFrame(sf, text=" Parametri Baseline (da DB) ", padding=4)
        pf.pack(fill="x", pady=(0, 4), padx=2)
        self._param_vars = {}
        for lbl, key in [("Finestra (deg)", "window_deg"),
                          ("Sigma Factor",  "sigma_factor"),
                          ("Min Abs Dev",   "min_abs_dev")]:
            row = ttk.Frame(pf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._param_vars[key] = v
            ttk.Label(row, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # DETTAGLI CLUSTER
        # ═══════════════════════════════════════════════════════════
        cf = ttk.LabelFrame(sf, text=" Dettagli Cluster ", padding=4)
        cf.pack(fill="x", pady=(0, 4), padx=2)
        for key, lbl in [
            ("rWeldAngleStart",    "Inizio (deg)"),
            ("rWeldAngleEnd",      "Fine (deg)"),
            ("rPeakDeviation",     "Deviazione"),
            ("iConsecutiveCount",  "Camp. cluster"),
            ("iClustersFound",     "Cluster tot."),
            ("iClustersValid",     "Cluster validi"),
        ]:
            row = ttk.Frame(cf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._result_vars[key] = v
            ttk.Label(row, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # BASELINE DIAGNOSTICA
        # ═══════════════════════════════════════════════════════════
        bf = ttk.LabelFrame(sf, text=" Baseline ", padding=4)
        bf.pack(fill="x", pady=(0, 4), padx=2)
        for key, lbl in [
            ("rBaselineMean",      "Media"),
            ("rBaselineSigma",     "Sigma"),
            ("rAdaptiveThreshold", "Soglia"),
        ]:
            row = ttk.Frame(bf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._result_vars[key] = v
            ttk.Label(row, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # USCITE FB (compatto)
        # ═══════════════════════════════════════════════════════════
        of = ttk.LabelFrame(sf, text=" Stato FB ", padding=4)
        of.pack(fill="x", pady=(0, 4), padx=2)
        self._out_vars = {}
        # Riga compatta con Ready/Busy/Done/Error
        row1 = ttk.Frame(of); row1.pack(fill="x", pady=1)
        for key in ["O_Ready", "O_Busy", "O_Done", "O_Error"]:
            v = tk.StringVar(value="-"); self._out_vars[key] = v
            ttk.Label(row1, text=key.replace("O_","")[:4], style="Muted.TLabel", width=5).pack(side="left")
            ttk.Label(row1, textvariable=v, style="Result.TLabel", width=3).pack(side="left", padx=(0,4))
        # ErrorCode separato
        row2 = ttk.Frame(of); row2.pack(fill="x", pady=1)
        ttk.Label(row2, text="ErrorCode", style="Muted.TLabel", width=14).pack(side="left")
        v = tk.StringVar(value="--"); self._out_vars["O_ErrorCode"] = v
        ttk.Label(row2, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")
        # Variabili stato extra
        for key, lbl in [
            ("bWrapAround",        "Wrap-around"),
            ("bMultipleClusters",  "Multi-cluster"),
            ("I_AxisTandSteel",    "Asse fermo"),
            ("iState",             "iState"),
        ]:
            row = ttk.Frame(of); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._result_vars[key] = v
            ttk.Label(row, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # UDT (collassato, solo i principali)
        # ═══════════════════════════════════════════════════════════
        uf = ttk.LabelFrame(sf, text=" UDT IO_RicercaSaldatura ", padding=4)
        uf.pack(fill="x", pady=(0, 4), padx=2)
        self._udt_vars = {}
        for key, lbl in [
            ("IO_RicercaSaldatura.Trovata",          "Trovata"),
            ("IO_RicercaSaldatura.OutPosizioneAsse", "Posizione (deg)"),
            ("IO_RicercaSaldatura.ActualValue",      "Valore attuale"),
        ]:
            row = ttk.Frame(uf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._udt_vars[key] = v
            ttk.Label(row, textvariable=v, style="UdtResult.TLabel", background=PANEL_BG).pack(side="right")
        # Variabili meno importanti
        for key, lbl in [
            ("IO_RicercaSaldatura.EnableRicerca",    "Enable"),
            ("IO_RicercaSaldatura.ClearReq",         "ClearReq"),
            ("IO_RicercaSaldatura.ValidRangeValue",  "ValidRange"),
        ]:
            row = ttk.Frame(uf); row.pack(fill="x", pady=1)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=14).pack(side="left")
            v = tk.StringVar(value="--"); self._udt_vars[key] = v
            ttk.Label(row, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ═══════════════════════════════════════════════════════════
        # PULSANTI ESPORTAZIONE
        # ═══════════════════════════════════════════════════════════
        ttk.Separator(sf, orient="horizontal").pack(fill="x", padx=2, pady=6)
        ttk.Button(sf, text="Esporta PNG", command=self._export_png).pack(fill="x", pady=2, padx=2)
        ttk.Button(sf, text="Esporta CSV", command=self._export_csv).pack(fill="x", pady=2, padx=2)

    # ── NOTEBOOK DESTRO ───────────────────────────────────────
    def _build_right(self, p):
        # ── Notebook PRINCIPALE (4 tab) ──
        nb = ttk.Notebook(p);  nb.pack(fill="both", expand=True);  self.nb = nb

        def _fig_canvas(parent, nrows=1, polar=False):
            fig = Figure(facecolor=DARK_BG)
            if polar:
                ax = fig.add_subplot(111, projection="polar")
                self._style_polar(ax)
                axes = ax
            else:
                axes = [fig.add_subplot(nrows, 1, i+1) for i in range(nrows)]
            # ── Label coordinate in cima al tab (in alto a sinistra) ──
            _coord_lbl = tk.Label(parent, text="", font=("Consolas", 9),
                fg=ACCENT, bg=DARK_BG, padx=4, anchor="w")
            _coord_lbl.pack(side="top", fill="x")

            c = FigureCanvasTkAgg(fig, parent)
            c.get_tk_widget().pack(fill="both", expand=True)
            NavigationToolbar2Tk(c, parent).pack(fill="x")

            if not polar:
                def on_motion(event, _lbl=_coord_lbl):
                    if event.inaxes:
                        _lbl.config(text=f"  X: {event.xdata:.3f}   Y: {event.ydata:.4f}")
                    else:
                        _lbl.config(text="")
                c.mpl_connect('motion_notify_event', on_motion)

                # Crosshair su tutti gli assi
                for ax in (axes if isinstance(axes, list) else [axes]):
                    # Linee crosshair
                    hline = ax.axhline(color=MUTED_CLR, linewidth=0.5, alpha=0, linestyle=":")
                    vline = ax.axvline(color=MUTED_CLR, linewidth=0.5, alpha=0, linestyle=":")

                    def _make_crosshair_handler(a, h, v):
                        def handler(event):
                            if event.inaxes == a:
                                h.set_ydata([event.ydata])
                                v.set_xdata([event.xdata])
                                h.set_alpha(0.5);  v.set_alpha(0.5)
                            else:
                                h.set_alpha(0);  v.set_alpha(0)
                        return handler

                    c.mpl_connect('motion_notify_event',
                                   _make_crosshair_handler(ax, hline, vline))

            return fig, axes, c

        # ══════════════════════════════════════════════════════
        #  TAB 1: 📊 ANALISI (con sotto-notebook)
        # ══════════════════════════════════════════════════════
        analisi_frame = ttk.Frame(nb)
        nb.add(analisi_frame, text="  📊  Analisi  ")
        sub_nb = ttk.Notebook(analisi_frame)
        sub_nb.pack(fill="both", expand=True)
        self._sub_nb = sub_nb
        self._analisi_dirty = set()   # indici tab da ridisegnare al primo accesso

        def _on_analisi_tab_changed(event=None):
            """Ridisegna il tab analisi solo se marcato sporco (lazy draw)."""
            if not self.comp_data:
                return
            try:
                cur = self._sub_nb.index("current")
            except Exception:
                return
            if cur not in self._analisi_dirty:
                return
            draw_map = {
                0: self._draw_signal_tab,
                1: self._draw_polar_tab,
                2: self._draw_hist_tab,
                3: self._draw_raw_graphs_tab,
                4: self._draw_samples_angles_tab,
            }
            fn = draw_map.get(cur)
            if fn:
                fn()
                self._analisi_dirty.discard(cur)

        sub_nb.bind("<<NotebookTabChanged>>", _on_analisi_tab_changed)

        def _sub_tab(title):
            f = ttk.Frame(sub_nb);  sub_nb.add(f, text=title);  return f

        # Sub 1 – Segnale & Baseline
        t = _sub_tab("  Segnale & Baseline  ")
        self.fig1, (self.ax1a, self.ax1b), self.canvas1 = _fig_canvas(t, 2)
        self._style_axes(self.ax1a, "Segnale laser + baseline adattiva", "Angolo (°)", "Valore")
        self._style_axes(self.ax1b, "Delta (segnale − baseline)", "Angolo (°)", "Δ")
        self.fig1.tight_layout(pad=2.5)

        # Sub 2 – Polare
        t = _sub_tab("  Vista polare  ")
        self.fig2, self.ax2, self.canvas2 = _fig_canvas(t, polar=True)
        self.fig2.tight_layout(pad=2.5)

        # Sub 3 – Distribuzione
        t = _sub_tab("  Distribuzione  ")
        self.fig3 = Figure(facecolor=DARK_BG)
        self.ax3a = self.fig3.add_subplot(121);  self.ax3b = self.fig3.add_subplot(122)
        self._style_axes(self.ax3a, "Istogramma segnale", "Valore", "Conteggio")
        self._style_axes(self.ax3b, "Istogramma delta",   "Δ",      "Conteggio")
        self.fig3.tight_layout(pad=2.5)
        c3 = FigureCanvasTkAgg(self.fig3, t);  c3.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c3, t).pack(fill="x");  self.canvas3 = c3

        # Sub 4 – Grezzi
        t = _sub_tab("  Grafici Grezzi  ")
        self.fig4, (self.ax4a, self.ax4b, self.ax4c), self.canvas4 = _fig_canvas(t, 3)
        self._style_axes(self.ax4a, "Segnale laser grezzo",           "Indice", "Valore")
        self._style_axes(self.ax4b, "Angolo encoder grezzo",          "Indice", "Angolo (°)")
        self._style_axes(self.ax4c, "Laser vs Angolo (scatter)",      "Angolo (°)", "Valore")
        self.fig4.tight_layout(pad=2.8)

        # Sub 5 – arSamples ↔ arAngles
        t = _sub_tab("  arSamples ↔ arAngles  ")
        self.fig5 = Figure(facecolor=DARK_BG)
        gs = self.fig5.add_gridspec(2, 2, width_ratios=[4,1], height_ratios=[1,4], hspace=0.04, wspace=0.04)
        self.ax5_main  = self.fig5.add_subplot(gs[1, 0])
        self.ax5_top   = self.fig5.add_subplot(gs[0, 0], sharex=self.ax5_main)
        self.ax5_right = self.fig5.add_subplot(gs[1, 1], sharey=self.ax5_main)
        self._style_axes(self.ax5_main, "", "arAngles (°)", "arSamples")
        self._style_axes(self.ax5_top,  "arSamples ↔ arAngles", "", "N")
        self._style_axes(self.ax5_right,"", "N", "")
        self.fig5.subplots_adjust(left=0.08, right=0.96, top=0.92, bottom=0.08)
        c5 = FigureCanvasTkAgg(self.fig5, t);  c5.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c5, t).pack(fill="x");  self.canvas5 = c5

        # Sub 6 – Dati grezzi DB
        t = _sub_tab("  Dati grezzi DB  ")
        self._build_raw_tab(t)

        # Sub 7 – Confronto Multi-DB
        t = _sub_tab("  Confronto Multi-DB  ")
        self._build_multidb_tab(t)

        # ══════════════════════════════════════════════════════
        #  TAB 2: ▶ SIMULATORE PLC
        # ══════════════════════════════════════════════════════
        sim_frame = ttk.Frame(nb)
        nb.add(sim_frame, text="  ▶  Simulatore PLC  ")
        self._build_sim_tab(sim_frame)

        # ══════════════════════════════════════════════════════
        #  TAB 3: 🔌 PLC READER
        # ══════════════════════════════════════════════════════
        plc_frame = ttk.Frame(nb)
        nb.add(plc_frame, text="  🔌  PLC Reader  ")
        self._build_plc_tab(plc_frame)

        # ══════════════════════════════════════════════════════
        #  TAB 4: 🌐 OPC UA
        # ══════════════════════════════════════════════════════

        # ══════════════════════════════════════════════════════
        #  TAB 5: 📈 STATISTICHE PARAMETRI
        # ══════════════════════════════════════════════════════
        stat_frame = ttk.Frame(nb)
        nb.add(stat_frame, text="  📈  Statistiche  ")
        self._build_stat_tab(stat_frame)

        sql_imp_frame = ttk.Frame(nb)
        nb.add(sql_imp_frame, text="  🗄  SQLite Import  ")
        self._build_sqlite_import_tab(sql_imp_frame)

        sql_qry_frame = ttk.Frame(nb)
        nb.add(sql_qry_frame, text="  🔍  SQLite Query  ")
        self._build_sqlite_query_tab(sql_qry_frame)

        self._analisi_tab_idx  = 0
        self._sim_tab_idx      = 1
        self._plc_tab_idx      = 2
        self._stat_tab_idx     = 3
        self._sqlimport_tab_idx= 4
        self._sqlquery_tab_idx = 5
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, event=None):
        """Mostra pannello sinistro solo su tab Analisi, nasconde sulle altre."""
        try:
            current = self.nb.index("current")
        except Exception:
            return
        if current == self._analisi_tab_idx:
            # Su Analisi: mostra pannello sinistro
            try:
                self._main_pane.pane(self._left_frame)
            except Exception:
                self._main_pane.insert(0, self._left_frame, weight=0)
        else:
            # Su Simulatore/PLC/OPC UA: nascondi pannello sinistro
            if self._left_frame.winfo_manager():
                self._main_pane.forget(self._left_frame)

    # ── TAB DATI GREZZI ───────────────────────────────────────
    def _build_raw_tab(self, p):
        sb = ttk.Scrollbar(p);  sb.pack(side="right", fill="y")
        self.txt_raw = tk.Text(p, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas", 9),
                               wrap="none", yscrollcommand=sb.set,
                               insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self.txt_raw.pack(fill="both", expand=True)
        sb.config(command=self.txt_raw.yview)

    # ── TAB SIMULATORE ────────────────────────────────────────
    def _build_sim_tab(self, parent):
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)

        # ── Colonna parametri con SCROLLBAR ──
        lf_outer = ttk.Frame(pane, width=400);  pane.add(lf_outer, weight=0)
        lf_outer.pack_propagate(False)          # *** impedisce resize da contenuto ***
        
        # Canvas scrollabile
        sim_canvas = tk.Canvas(lf_outer, bg=DARK_BG, highlightthickness=0, width=380)
        sim_scrollbar = ttk.Scrollbar(lf_outer, orient="vertical", command=sim_canvas.yview)
        sim_scrollable = ttk.Frame(sim_canvas)
        
        sim_scrollable.bind("<Configure>", lambda e: sim_canvas.configure(scrollregion=sim_canvas.bbox("all")))
        _sim_win_id = sim_canvas.create_window((0, 0), window=sim_scrollable, anchor="nw")
        sim_canvas.configure(yscrollcommand=sim_scrollbar.set)
        # Forza il frame scrollabile a riempire la larghezza del canvas
        def _sync_width(event):
            sim_canvas.itemconfigure(_sim_win_id, width=event.width)
        sim_canvas.bind("<Configure>", _sync_width)
        
        def _sim_mousewheel(event):
            sim_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        sim_canvas.bind("<MouseWheel>", _sim_mousewheel)
        
        sim_scrollbar.pack(side="right", fill="y")
        sim_canvas.pack(side="left", fill="both", expand=True)
        
        lf = sim_scrollable  # alias

        # Info modalita (compatta)
        info = ttk.LabelFrame(lf, text=" Modalita ", padding=4)
        info.pack(fill="x", padx=4, pady=(4, 2))
        self._sim_stop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(info, text="StopOnWeld", variable=self._sim_stop_var).pack(anchor="w")
        self._sim_axis_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(info, text="AxisStandStill", variable=self._sim_axis_var).pack(anchor="w")
        
        # Dizionario per memorizzare i valori originali dal DB
        self._db_original_params = {}
        
        # Indicatore stato parametri (mostra se modificati rispetto a DB)
        self._param_status_var = tk.StringVar(value="")
        self._param_status_label = ttk.Label(info, textvariable=self._param_status_var, 
                                              style="Muted.TLabel", font=("Segoe UI", 8))
        self._param_status_label.pack(anchor="w", pady=(2, 0))

        # Filtro (compatto)
        f1 = ttk.LabelFrame(lf, text=" Filtro ", padding=4)
        f1.pack(fill="x", padx=4, pady=2)
        self._sv = {}
        for lbl, key, val in [
            ("Min ang (deg)",  "min_ang",   "0.50"),
            ("Min laser",      "min_las",   "0.10"),
            ("Min valid",      "min_valid", "0.0"),
            ("Max valid",      "max_valid", "9999.0"),
            ("Max camp.",      "max_samp",  "5000"),
        ]:
            r = ttk.Frame(f1); r.pack(fill="x", pady=1)
            ttk.Label(r, text=lbl, style="Muted.TLabel", width=12).pack(side="left")
            v = tk.StringVar(value=val); self._sv[key] = v
            ttk.Entry(r, textvariable=v, width=8).pack(side="right")

        # Soglie + Cluster: layout multi-colonna
        # Colonne: Parametro | DB orig | Stats | Corrente
        # Colori sfondo: DB=scuro, Stats=blu scuro, Corrente=entry
        _COL_DB    = "#0d1117"   # sfondo valori DB origine
        _COL_STAT  = "#0d1f38"   # sfondo valori statistiche
        _COL_CUR   = ENTRY_BG    # sfondo valore corrente

        # Dizionari StringVar per colonne DB-orig e Stats
        self._sv_orig = {}   # key -> StringVar (valori DB)
        self._sv_stat = {}   # key -> StringVar (valori stats)

        def _make_param_frame(parent, rows):
            # Intestazione colonne
            hdr = ttk.Frame(parent); hdr.pack(fill="x", pady=(0, 2))
            ttk.Label(hdr, text="", width=10).pack(side="left")
            tk.Label(hdr, text="DB", bg=_COL_DB, fg=MUTED_CLR,
                     font=("Consolas", 7), width=7, anchor="c").pack(side="left", padx=1)
            self._stat_col_hdr = tk.Label(hdr, text="Stats", bg=_COL_STAT, fg=MUTED_CLR,
                     font=("Consolas", 7), width=7, anchor="c")
            self._stat_col_hdr.pack(side="left", padx=1)
            self._stat_col_hdr.pack_forget()  # nascosta finche no stats
            tk.Label(hdr, text="Corrente", bg=_COL_CUR, fg=TEXT_CLR,
                     font=("Consolas", 7), width=7, anchor="c").pack(side="left", padx=1)
            # Righe
            for lbl, key, val in rows:
                r = ttk.Frame(parent); r.pack(fill="x", pady=1)
                ttk.Label(r, text=lbl, style="Muted.TLabel", width=10).pack(side="left")
                # Colonna DB origine (read-only)
                ov = tk.StringVar(value="—"); self._sv_orig[key] = ov
                tk.Label(r, textvariable=ov, bg=_COL_DB, fg=MUTED_CLR,
                         font=("Consolas", 9), width=7, anchor="e",
                         relief="flat", bd=1).pack(side="left", padx=1)
                # Colonna Stats (read-only, nascosta inizialmente)
                sv2 = tk.StringVar(value=""); self._sv_stat[key] = sv2
                sl = tk.Label(r, textvariable=sv2, bg=_COL_STAT, fg="#79c0ff",
                              font=("Consolas", 9), width=7, anchor="e",
                              relief="flat", bd=1)
                sl.pack(side="left", padx=1)
                sl.pack_forget()  # nascosta finche no stats
                sl._key = key     # riferimento per show/hide
                # Colonna Corrente (entry editabile)
                v = tk.StringVar(value=val); self._sv[key] = v
                tk.Entry(r, textvariable=v, width=7, bg=_COL_CUR,
                         fg=TEXT_CLR, insertbackground=TEXT_CLR,
                         relief="flat", bd=1, font=("Consolas", 9)).pack(side="left", padx=1)

        f2 = ttk.LabelFrame(lf, text=" Soglie ", padding=4)
        f2.pack(fill="x", padx=4, pady=2)
        _make_param_frame(f2, [
            ("Finestra", "win",    "10.0"),
            ("Sigma",    "sig_f",  "3.0"),
            ("Min Abs",  "min_abs", "1.5"),
            ("Hyst",     "hyst",   "0.5"),
        ])

        f3 = ttk.LabelFrame(lf, text=" Cluster ", padding=4)
        f3.pack(fill="x", padx=4, pady=2)
        _make_param_frame(f3, [
            ("Min consec", "min_cons", "3"),
            ("Max consec", "max_cons", "60"),
        ])

        # *** v4.3: Polarita picco ***
        f4 = ttk.LabelFrame(lf, text=" Polarita (v4.3) ", padding=4)
        f4.pack(fill="x", padx=4, pady=2)
        self._sim_polarity_var = tk.IntVar(value=0)
        for txt, val in [("POSITIVI", 0), ("NEGATIVI", 1), ("ENTRAMBI", 2)]:
            ttk.Radiobutton(f4, text=txt, variable=self._sim_polarity_var, value=val
                           ).pack(anchor="w", pady=0)

        # *** v4.9 *** Dead zone detection separata
        f49 = ttk.LabelFrame(lf, text=" v4.9: Eccentrico ", padding=4)
        f49.pack(fill="x", padx=4, pady=2)
        r49a = ttk.Frame(f49); r49a.pack(fill="x", pady=1)
        ttk.Label(r49a, text="DetStart [\u00b0]", style="Muted.TLabel", width=12).pack(side="left")
        self._sv["det_start"] = tk.StringVar(value="0.0")
        ttk.Entry(r49a, textvariable=self._sv["det_start"], width=8).pack(side="right")
        ttk.Label(f49, text="0=usa window (retrocompat.)",
                  style="Muted.TLabel", font=("Consolas",7)).pack(anchor="w")

        # *** v4.6 *** Baseline adattiva + Superficie piatta
        f5 = ttk.LabelFrame(lf, text=" v4.6: Buchi anello ", padding=4)
        f5.pack(fill="x", padx=4, pady=2)
        self._sv46_adapt_en  = tk.BooleanVar(value=False)
        self._sv46_flat_en   = tk.BooleanVar(value=False)
        # Baseline adattiva
        r46a = ttk.Frame(f5); r46a.pack(fill="x", pady=1)
        tk.Checkbutton(r46a, text="Baseline adattiva", variable=self._sv46_adapt_en,
            bg=DARK_BG, selectcolor="#1f6feb", activebackground=DARK_BG,
            fg=TEXT_CLR, font=("Consolas",9)).pack(side="left")
        r46b = ttk.Frame(f5); r46b.pack(fill="x", pady=1)
        ttk.Label(r46b, text="  Offset [mm]", style="Muted.TLabel", width=12).pack(side="left")
        self._sv["adapt_offset"] = tk.StringVar(value="3.0")
        ttk.Entry(r46b, textvariable=self._sv["adapt_offset"], width=8).pack(side="right")
        # Superficie piatta
        r46c = ttk.Frame(f5); r46c.pack(fill="x", pady=1)
        tk.Checkbutton(r46c, text="Attesa sup. piatta", variable=self._sv46_flat_en,
            bg=DARK_BG, selectcolor="#1f6feb", activebackground=DARK_BG,
            fg=TEXT_CLR, font=("Consolas",9)).pack(side="left")
        r46d = ttk.Frame(f5); r46d.pack(fill="x", pady=1)
        ttk.Label(r46d, text="  Campioni", style="Muted.TLabel", width=12).pack(side="left")
        self._sv["flat_samples"] = tk.StringVar(value="5")
        ttk.Entry(r46d, textvariable=self._sv["flat_samples"], width=8).pack(side="right")
        r46e = ttk.Frame(f5); r46e.pack(fill="x", pady=1)
        ttk.Label(r46e, text="  Toll. [mm]", style="Muted.TLabel", width=12).pack(side="left")
        self._sv["flat_toll"] = tk.StringVar(value="0.5")
        ttk.Entry(r46e, textvariable=self._sv["flat_toll"], width=8).pack(side="right")

        # Pulsanti
        btn_frame = ttk.Frame(lf)
        btn_frame.pack(fill="x", padx=4, pady=(6, 4))
        ttk.Button(btn_frame, text="ESEGUI SIMULAZIONE", style="Sim.TButton",
                   command=self._run_simulation).pack(fill="x", pady=(0, 2))
        ttk.Button(btn_frame, text="Ripristina Default DB", 
                   command=self._restore_db_defaults).pack(fill="x")

        # ═══════════════════════════════════════════════════════════
        # RISULTATI SIMULAZIONE (compatti)
        # ═══════════════════════════════════════════════════════════
        res = ttk.LabelFrame(lf, text=" RISULTATO ", padding=4)
        res.pack(fill="x", padx=4, pady=2)
        self._sr = {}
        self._sr_flat_found = tk.StringVar(value="—")
        self._sr_angle_exc  = tk.StringVar(value="—")

        # Banner stato (wraplength evita di allargare la colonna)
        self._sim_banner_var = tk.StringVar(value="--")
        self._sim_banner = tk.Label(res, textvariable=self._sim_banner_var,
                                    bg=PANEL_BG, fg=MUTED_CLR,
                                    font=("Consolas", 9, "bold"), pady=2, padx=2,
                                    wraplength=255, justify="left", anchor="w")
        self._sim_banner.pack(fill="x", pady=(0, 2))

        # Dati principali
        for key, lbl, style in [
            ("det_ang",   "Angolo (deg)", "DetResult.TLabel"),
            ("det_samp",  "Campione #",   "DetResult.TLabel"),
            ("peak",      "Picco",        "SimResult.TLabel"),
            ("sigmas",    "SNR (sigma)",  "SimResult.TLabel"),
            ("trovata",   "Trovata",      "UdtResult.TLabel"),
        ]:
            r = ttk.Frame(res);  r.pack(fill="x", pady=1)
            ttk.Label(r, text=lbl, style="Muted.TLabel", width=12).pack(side="left")
            v = tk.StringVar(value="--");  self._sr[key] = v
            ttk.Label(r, textvariable=v, style=style, background=PANEL_BG).pack(side="right")

        # Dettagli cluster
        det = ttk.LabelFrame(lf, text=" Dettagli ", padding=4)
        det.pack(fill="x", padx=4, pady=2)
        for key, lbl, style in [
            ("start",     "Inizio (deg)",  "SimResult.TLabel"),
            ("end",       "Fine (deg)",    "SimResult.TLabel"),
            ("dev",       "Deviazione",    "SimResult.TLabel"),
            ("consec",    "Camp. cluster", "SimResult.TLabel"),
            ("clusters",  "Cluster val.",  "SimResult.TLabel"),
            ("out_pos",   "OutPosAsse",    "UdtResult.TLabel"),
        ]:
            r = ttk.Frame(det);  r.pack(fill="x", pady=1)
            ttk.Label(r, text=lbl, style="Muted.TLabel", width=12).pack(side="left")
            v = tk.StringVar(value="--");  self._sr[key] = v
            ttk.Label(r, textvariable=v, style=style, background=PANEL_BG).pack(side="right")

        # *** v4.7 *** Indicatori superficie piatta e dead zone
        v47_f = ttk.LabelFrame(lf, text=" v4.6/v4.7 ", padding=4)
        v47_f.pack(fill="x", padx=4, pady=2)
        for _lbl, _sv in [("Baseline OK", self._sr_angle_exc),
                          ("Sup. piatta", self._sr_flat_found)]:
            _r = ttk.Frame(v47_f); _r.pack(fill="x", pady=1)
            ttk.Label(_r, text=_lbl, style="Muted.TLabel", width=12).pack(side="left")
            ttk.Label(_r, textvariable=_sv, style="SimResult.TLabel",
                      background=PANEL_BG).pack(side="right")
        self._sr_flat_found_lbl = v47_f

        # Statistiche filtro
        flt = ttk.LabelFrame(lf, text=" Filtro ", padding=4)
        flt.pack(fill="x", padx=4, pady=2)
        for key, lbl in [
            ("n_raw",     "Input"),
            ("n_acq",     "Filtrati"),
            ("rej_ang",   "Rej ang"),
            ("rej_las",   "Rej las"),
            ("rej_range", "Rej range"),
            ("rej_axis",  "Rej axis"),
        ]:
            r = ttk.Frame(flt);  r.pack(fill="x", pady=1)
            ttk.Label(r, text=lbl, style="Muted.TLabel", width=12).pack(side="left")
            v = tk.StringVar(value="--");  self._sr[key] = v
            ttk.Label(r, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # Baseline
        bl = ttk.LabelFrame(lf, text=" Baseline ", padding=4)
        bl.pack(fill="x", padx=4, pady=2)
        for key, lbl in [
            ("bl_mean",   "Media"),
            ("bl_sig",    "Sigma"),
            ("det_delay", "Delay"),
        ]:
            r = ttk.Frame(bl);  r.pack(fill="x", pady=1)
            ttk.Label(r, text=lbl, style="Muted.TLabel", width=12).pack(side="left")
            v = tk.StringVar(value="--");  self._sr[key] = v
            ttk.Label(r, textvariable=v, style="Result.TLabel", background=PANEL_BG).pack(side="right")

        # ── Grafici simulazione ──
        rf = ttk.Frame(pane);  pane.add(rf, weight=1)
        # ── Wrapper notebook: Sorgente | Grafici ─────────────
        sim_outer_nb = ttk.Notebook(rf)
        sim_outer_nb.pack(fill="both", expand=True)
        self._sim_outer_nb = sim_outer_nb

        # Sub-tab 0: Sorgente dati
        src_frame = ttk.Frame(sim_outer_nb)
        sim_outer_nb.add(src_frame, text="  📂  Sorgente  ")
        self._build_sim_source_tab(src_frame)

        # Sub-tab 1: Grafici simulazione
        graf_frame = ttk.Frame(sim_outer_nb)
        sim_outer_nb.add(graf_frame, text="  📊  Grafici  ")
        rf2 = graf_frame   # alias per il codice dei grafici che usa rf

        sim_nb = ttk.Notebook(rf2);  sim_nb.pack(fill="both", expand=True);  self.sim_nb = sim_nb
        self._sim_dirty    = set()
        self._sim_draw_map = []

        def _on_sim_tab_changed(event=None):
            if not self.sim_result:
                return
            try:
                cur = self.sim_nb.index("current")
            except Exception:
                return
            if cur not in self._sim_dirty:
                return
            if cur < len(self._sim_draw_map):
                self._sim_draw_map[cur]()
                self._sim_dirty.discard(cur)

        sim_nb.bind("<<NotebookTabChanged>>", _on_sim_tab_changed)

        def _stab(title):
            f = ttk.Frame(sim_nb);  sim_nb.add(f, text=title);  return f

        # Grafico A - Segnale + detection point
        sa = _stab("  Segnale & Detection  ")
        self.fig_sa = Figure(facecolor=DARK_BG)
        self.ax_sa1 = self.fig_sa.add_subplot(211)
        self.ax_sa2 = self.fig_sa.add_subplot(212)
        self._style_axes(self.ax_sa1, "Segnale filtrato + soglie + punto detection", "Angolo (deg)", "Valore")
        self._style_axes(self.ax_sa2, "Delta segnale - baseline", "Angolo (deg)", "Delta")
        self.fig_sa.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sa, sa);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sa).pack(fill="x");  self.canvas_sa = c

        # Grafico B - Effetto filtro
        sb2 = _stab("  Effetto Filtro  ")
        self.fig_sb = Figure(facecolor=DARK_BG)
        self.ax_sb1 = self.fig_sb.add_subplot(211)
        self.ax_sb2 = self.fig_sb.add_subplot(212)
        self._style_axes(self.ax_sb1, "Grezzo vs Filtrato", "Angolo (deg)", "Valore")
        self._style_axes(self.ax_sb2, "Densita campioni per angolo", "Angolo (deg)", "Campioni / grado")
        self.fig_sb.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sb, sb2);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sb2).pack(fill="x");  self.canvas_sb = c

        # Grafico C - Polare simulata
        sc3 = _stab("  Vista polare  ")
        self.fig_sc = Figure(facecolor=DARK_BG)
        self.ax_sc = self.fig_sc.add_subplot(111, projection="polar")
        self._style_polar(self.ax_sc)
        self.fig_sc.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sc, sc3);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sc3).pack(fill="x");  self.canvas_sc = c

        # Grafico D - Soglie per campione
        sd4 = _stab("  Soglie per campione  ")
        self.fig_sd = Figure(facecolor=DARK_BG)
        self.ax_sd1 = self.fig_sd.add_subplot(211)
        self.ax_sd2 = self.fig_sd.add_subplot(212)
        self._style_axes(self.ax_sd1, "Mean e Sigma per campione (live)", "Angolo (deg)", "Valore")
        self._style_axes(self.ax_sd2, "ThreshHigh / ThreshLow per campione (live)", "Angolo (deg)", "Soglia")
        self.fig_sd.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sd, sd4);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sd4).pack(fill="x");  self.canvas_sd = c

        # Grafico E – Timeline detection (NUOVO: mostra campione x campione fino al rilevamento)
        se5 = _stab("  Timeline Detection  ")
        self.fig_se = Figure(facecolor=DARK_BG)
        self.ax_se1 = self.fig_se.add_subplot(311)
        self.ax_se2 = self.fig_se.add_subplot(312)
        self.ax_se3 = self.fig_se.add_subplot(313)
        self._style_axes(self.ax_se1, "Valore laser per campione", "Indice campione", "Valore")
        self._style_axes(self.ax_se2, "Δ segnale vs soglia", "Indice campione", "Δ")
        self._style_axes(self.ax_se3, "Contatore cluster (sale fino a detection)", "Indice campione", "Consecutivi")
        self.fig_se.tight_layout(pad=2.8)
        c = FigureCanvasTkAgg(self.fig_se, se5);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, se5).pack(fill="x");  self.canvas_se = c

        # Grafico F – Confronto DB vs Simulazione (residuo soglie)
        sf6 = _stab("  DB vs Sim  ")
        self.fig_sf = Figure(facecolor=DARK_BG)
        self.ax_sf1 = self.fig_sf.add_subplot(311)
        self.ax_sf2 = self.fig_sf.add_subplot(312)
        self.ax_sf3 = self.fig_sf.add_subplot(313)
        self._style_axes(self.ax_sf1, "Mean PLC vs Sim", "Angolo (°)", "Valore")
        self._style_axes(self.ax_sf2, "ThreshHigh PLC vs Sim", "Angolo (°)", "Valore")
        self._style_axes(self.ax_sf3, "Residuo (PLC − Sim)", "Angolo (°)", "Δ")
        self.fig_sf.tight_layout(pad=2.8)
        c = FigureCanvasTkAgg(self.fig_sf, sf6);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sf6).pack(fill="x");  self.canvas_sf = c

        # Grafico G – Copertura Finestra Look-back
        sg7 = _stab("  Copertura Finestra  ")
        self.fig_sg = Figure(facecolor=DARK_BG)
        self.ax_sg1 = self.fig_sg.add_subplot(211)
        self.ax_sg2 = self.fig_sg.add_subplot(212)
        self._style_axes(self.ax_sg1, "win_n: campioni nella finestra look-back", "Angolo (°)", "win_n")
        self._style_axes(self.ax_sg2, "Soglia affidabile (win_n ≥ 3) vs debole", "Angolo (°)", "Valore")
        self.fig_sg.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sg, sg7);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sg7).pack(fill="x");  self.canvas_sg = c

        # Grafico H – Mappa Campioni Rifiutati
        sh8 = _stab("  Campioni Rifiutati  ")
        self.fig_sh = Figure(facecolor=DARK_BG)
        self.ax_sh1 = self.fig_sh.add_subplot(211)
        self.ax_sh2 = self.fig_sh.add_subplot(212)
        self._style_axes(self.ax_sh1, "Mappa rifiuti per angolo", "Angolo (°)", "Valore laser")
        self._style_axes(self.ax_sh2, "Distribuzione rifiuti per motivo e angolo", "Angolo (°)", "Conteggio")
        self.fig_sh.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_sh, sh8);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, sh8).pack(fill="x");  self.canvas_sh = c

        # Grafico I – SNR Rolling
        si9 = _stab("  SNR Rolling  ")
        self.fig_si = Figure(facecolor=DARK_BG)
        self.ax_si1 = self.fig_si.add_subplot(211)
        self.ax_si2 = self.fig_si.add_subplot(212)
        self._style_axes(self.ax_si1, "Sigma rolling (noise floor)", "Angolo (°)", "σ locale")
        self._style_axes(self.ax_si2, "SNR locale  |dev| / σ", "Angolo (°)", "SNR")
        self.fig_si.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_si, si9);  c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, si9).pack(fill="x");  self.canvas_si = c

        # Tab J – Dettaglio Cluster (tabella testuale)
        sj10 = _stab("  Dettaglio Cluster  ")
        sj_sb = ttk.Scrollbar(sj10);  sj_sb.pack(side="right", fill="y")
        self.txt_clusters = tk.Text(sj10, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas", 10),
                                     wrap="none", yscrollcommand=sj_sb.set,
                                     insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self.txt_clusters.pack(fill="both", expand=True)
        sj_sb.config(command=self.txt_clusters.yview)
        self.txt_clusters.tag_configure("header", foreground=ACCENT, font=("Consolas", 11, "bold"))
        self.txt_clusters.tag_configure("valid", foreground=OK_CLR)
        self.txt_clusters.tag_configure("invalid", foreground=WARN_CLR)
        self.txt_clusters.tag_configure("best", foreground=DET_CLR, font=("Consolas", 10, "bold"))

        # Tab L – Report Diagnostico
        sl12 = _stab("  Report Diagnostico  ")
        sl_sb = ttk.Scrollbar(sl12);  sl_sb.pack(side="right", fill="y")
        self.txt_report = tk.Text(sl12, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas", 10),
                                   wrap="word", yscrollcommand=sl_sb.set,
                                   insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self.txt_report.pack(fill="both", expand=True)
        sl_sb.config(command=self.txt_report.yview)
        self.txt_report.tag_configure("title", foreground=ACCENT, font=("Consolas", 13, "bold"))
        self.txt_report.tag_configure("ok", foreground=OK_CLR, font=("Consolas", 10, "bold"))
        self.txt_report.tag_configure("warn", foreground=WARN_CLR, font=("Consolas", 10, "bold"))
        self.txt_report.tag_configure("err", foreground="#FF6B6B", font=("Consolas", 10, "bold"))
        self.txt_report.tag_configure("section", foreground=UDT_CLR, font=("Consolas", 11, "bold"))
        self.txt_report.tag_configure("info", foreground=TEXT_CLR)

        # Tab M – Discordanze PLC vs Sim
        sm13 = _stab("  ⚠  Discordanze  ")
        self._disc_tab_frame = sm13

        disc_top = ttk.Frame(sm13); disc_top.pack(fill="both", expand=True)
        _dsb  = ttk.Scrollbar(disc_top); _dsb.pack(side="right", fill="y")
        _dsbx = ttk.Scrollbar(disc_top, orient="horizontal"); _dsbx.pack(side="bottom", fill="x")
        _dcols = ("file","plc","sim","plc_ang","sim_ang","delta_ang","plc_cons","sim_cons")
        self._disc_tree = ttk.Treeview(disc_top, columns=_dcols, show="headings", height=8,
                                        yscrollcommand=_dsb.set, xscrollcommand=_dsbx.set)
        _dsb.config(command=self._disc_tree.yview)
        _dsbx.config(command=self._disc_tree.xview)
        for _col, _lbl, _w in [
            ("file","File",200),("plc","PLC",60),("sim","Sim",60),
            ("plc_ang","Ang PLC",70),("sim_ang","Ang Sim",70),("delta_ang","Δ ang",65),
            ("plc_cons","Cons PLC",70),("sim_cons","Cons Sim",70),
        ]:
            self._disc_tree.heading(_col, text=_lbl)
            self._disc_tree.column(_col, width=_w, anchor="center" if _col != "file" else "w")
        self._disc_tree.tag_configure("disc",  background="#2a1a1a", foreground=WARN_CLR)
        self._disc_tree.tag_configure("match", foreground=MUTED_CLR)
        self._disc_tree.pack(fill="both", expand=True)
        self._disc_tree.bind("<Button-3>",
            lambda e: self._disc_context_menu(e, self._disc_tree,
                getattr(self,"_disc_rows",[])))

        disc_bot = ttk.Frame(sm13); disc_bot.pack(fill="both", expand=True)
        self._disc_fig = Figure(facecolor=DARK_BG)
        self._disc_ax1 = self._disc_fig.add_subplot(121)
        self._disc_ax2 = self._disc_fig.add_subplot(122)
        self._style_axes(self._disc_ax1, "Angolo rilevato: PLC vs Sim", "File #", "Angolo (°)")
        self._style_axes(self._disc_ax2, "Campioni consecutivi: PLC vs Sim", "File #", "Consecutivi")
        self._disc_fig.tight_layout(pad=2.0)
        _dcanv = FigureCanvasTkAgg(self._disc_fig, disc_bot)
        _dcanv.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(_dcanv, disc_bot).pack(fill="x")
        self._disc_canvas = _dcanv
        self._disc_rows = []

    # ── HELPERS STILE ─────────────────────────────────────────
    def _style_axes(self, ax, title="", xlabel="", ylabel=""):
        ax.set_facecolor(PANEL_BG)
        ax.set_title(title, color=ACCENT, fontsize=10, pad=6)
        ax.set_xlabel(xlabel, color=MUTED_CLR, fontsize=9)
        ax.set_ylabel(ylabel, color=MUTED_CLR, fontsize=9)
        ax.tick_params(colors=MUTED_CLR, labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(BORDER_CLR)
        ax.grid(True, color=BORDER_CLR, linewidth=0.5, alpha=0.7)

    def _style_polar(self, ax):
        ax.set_facecolor(PANEL_BG)
        ax.tick_params(colors=MUTED_CLR, labelsize=8)
        ax.grid(True, color=BORDER_CLR, linewidth=0.5, alpha=0.7)
        ax.set_theta_zero_location("N");  ax.set_theta_direction(-1)

    def _resolve_n_acq(self):
        sc = self.db_data["scalars"]
        n  = int(sc.get("iSamplesAcquired", 0))
        return n if n > 0 else int(sc.get("iSampleIndex", 0))

    # ── OPEN FILE ─────────────────────────────────────────────
    _MANUAL_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9Iml0Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLjAiPgo8dGl0bGU+V2VsZEZpbmQgdjQuNyDigJQgTWFudWFsZSBUZWNuaWNvPC90aXRsZT4KPHN0eWxlPgogIDpyb290IHsKICAgIC0tYmc6ICMwZDExMTc7IC0tcGFuZWw6ICMxNjFiMjI7IC0tYm9yZGVyOiAjMzAzNjNkOwogICAgLS1hY2NlbnQ6ICM1OGE2ZmY7IC0tb2s6ICMzZmI5NTA7IC0td2FybjogI2QyOTkyMjsKICAgIC0tZXJyOiAjZjg1MTQ5OyAtLXRleHQ6ICNjOWQxZDk7IC0tbXV0ZWQ6ICM4Yjk0OWU7CiAgICAtLWNvZGUtYmc6ICMxYTIwMjk7IC0taGVhZC1iZzogIzIxMjYyZDsKICB9CiAgKiB7IGJveC1zaXppbmc6IGJvcmRlci1ib3g7IG1hcmdpbjogMDsgcGFkZGluZzogMDsgfQogIGJvZHkgeyBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsgZm9udC1mYW1pbHk6ICdTZWdvZSBVSScsIHN5c3RlbS11aSwgc2Fucy1zZXJpZjsgZm9udC1zaXplOiAxNXB4OyBsaW5lLWhlaWdodDogMS43NTsgfQogIC53cmFwcGVyIHsgZGlzcGxheTogZmxleDsgbWluLWhlaWdodDogMTAwdmg7IH0KICBuYXYgeyB3aWR0aDogMjgwcHg7IG1pbi13aWR0aDogMjgwcHg7IGJhY2tncm91bmQ6IHZhcigtLXBhbmVsKTsgYm9yZGVyLXJpZ2h0OiAxcHggc29saWQgdmFyKC0tYm9yZGVyKTsgcGFkZGluZzogMjBweCAwOyBwb3NpdGlvbjogc3RpY2t5OyB0b3A6IDA7IGhlaWdodDogMTAwdmg7IG92ZXJmbG93LXk6IGF1dG87IH0KICBtYWluIHsgZmxleDogMTsgcGFkZGluZzogNDBweCA2MHB4OyBtYXgtd2lkdGg6IDk4MHB4OyB9CiAgLm5hdi1sb2dvIHsgcGFkZGluZzogMCAyMHB4IDE4cHg7IGJvcmRlci1ib3R0b206IDFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOyBtYXJnaW4tYm90dG9tOiAxMHB4OyB9CiAgLm5hdi1sb2dvIGgyIHsgY29sb3I6IHZhcigtLWFjY2VudCk7IGZvbnQtc2l6ZTogMTZweDsgZm9udC1mYW1pbHk6IENvbnNvbGFzLCBtb25vc3BhY2U7IH0KICAubmF2LWxvZ28gc3BhbiB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTFweDsgfQogIG5hdiBhIHsgZGlzcGxheTogYmxvY2s7IHBhZGRpbmc6IDVweCAyMHB4OyBjb2xvcjogdmFyKC0tbXV0ZWQpOyB0ZXh0LWRlY29yYXRpb246IG5vbmU7IGZvbnQtc2l6ZTogMTNweDsgYm9yZGVyLWxlZnQ6IDJweCBzb2xpZCB0cmFuc3BhcmVudDsgdHJhbnNpdGlvbjogYWxsIC4xNXM7IH0KICBuYXYgYTpob3ZlciwgbmF2IGEuYWN0aXZlIHsgY29sb3I6IHZhcigtLWFjY2VudCk7IGJvcmRlci1sZWZ0LWNvbG9yOiB2YXIoLS1hY2NlbnQpOyBiYWNrZ3JvdW5kOiByZ2JhKDg4LDE2NiwyNTUsLjA3KTsgfQogIG5hdiAuc2VjIHsgcGFkZGluZzogMTJweCAyMHB4IDNweDsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxMXB4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBsZXR0ZXItc3BhY2luZzogLjA4ZW07IH0KICBoMSB7IGZvbnQtc2l6ZTogMjhweDsgY29sb3I6IHZhcigtLWFjY2VudCk7IG1hcmdpbi1ib3R0b206IDhweDsgYm9yZGVyLWJvdHRvbTogMXB4IHNvbGlkIHZhcigtLWJvcmRlcik7IHBhZGRpbmctYm90dG9tOiAxMnB4OyB9CiAgaDIgeyBmb250LXNpemU6IDIwcHg7IGNvbG9yOiB2YXIoLS1hY2NlbnQpOyBtYXJnaW46IDM4cHggMCAxMnB4OyBwYWRkaW5nLWJvdHRvbTogNnB4OyBib3JkZXItYm90dG9tOiAxcHggc29saWQgdmFyKC0tYm9yZGVyKTsgfQogIGgzIHsgZm9udC1zaXplOiAxNnB4OyBjb2xvcjogI2U2ZWRmMzsgbWFyZ2luOiAyNnB4IDAgOXB4OyB9CiAgaDQgeyBmb250LXNpemU6IDEzcHg7IGNvbG9yOiB2YXIoLS13YXJuKTsgbWFyZ2luOiAxOHB4IDAgNnB4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBsZXR0ZXItc3BhY2luZzogLjA1ZW07IH0KICBwICB7IG1hcmdpbi1ib3R0b206IDExcHg7IH0KICBjb2RlIHsgYmFja2dyb3VuZDogdmFyKC0tY29kZS1iZyk7IHBhZGRpbmc6IDJweCA2cHg7IGJvcmRlci1yYWRpdXM6IDRweDsgZm9udC1mYW1pbHk6IENvbnNvbGFzLCBtb25vc3BhY2U7IGZvbnQtc2l6ZTogMTNweDsgY29sb3I6ICNlNmNjODc7IH0KICBwcmUgeyBiYWNrZ3JvdW5kOiB2YXIoLS1jb2RlLWJnKTsgYm9yZGVyOiAxcHggc29saWQgdmFyKC0tYm9yZGVyKTsgYm9yZGVyLXJhZGl1czogOHB4OyBwYWRkaW5nOiAxNnB4IDIwcHg7IG1hcmdpbjogMTJweCAwOyBvdmVyZmxvdy14OiBhdXRvOyBmb250LWZhbWlseTogQ29uc29sYXMsIG1vbm9zcGFjZTsgZm9udC1zaXplOiAxM3B4OyBsaW5lLWhlaWdodDogMS42OyB9CiAgcHJlIC5rdyAgeyBjb2xvcjogI2ZmN2I3MjsgfQogIHByZSAudmFyIHsgY29sb3I6ICM3OWMwZmY7IH0KICBwcmUgLm51bSB7IGNvbG9yOiAjZjJjYzYwOyB9CiAgcHJlIC5jbXQgeyBjb2xvcjogIzhiOTQ5ZTsgZm9udC1zdHlsZTogaXRhbGljOyB9CiAgcHJlIC5vcCAgeyBjb2xvcjogI2QyYThmZjsgfQogIHByZSAuc3RyIHsgY29sb3I6ICNhNWQ2ZmY7IH0KICB0YWJsZSB7IHdpZHRoOiAxMDAlOyBib3JkZXItY29sbGFwc2U6IGNvbGxhcHNlOyBtYXJnaW46IDE0cHggMDsgZm9udC1zaXplOiAxNHB4OyB9CiAgdGggeyBiYWNrZ3JvdW5kOiB2YXIoLS1oZWFkLWJnKTsgY29sb3I6IHZhcigtLWFjY2VudCk7IHBhZGRpbmc6IDEwcHggMTRweDsgdGV4dC1hbGlnbjogbGVmdDsgYm9yZGVyLWJvdHRvbTogMnB4IHNvbGlkIHZhcigtLWJvcmRlcik7IH0KICB0ZCB7IHBhZGRpbmc6IDlweCAxNHB4OyBib3JkZXItYm90dG9tOiAxcHggc29saWQgdmFyKC0tYm9yZGVyKTsgdmVydGljYWwtYWxpZ246IHRvcDsgfQogIHRyOmhvdmVyIHRkIHsgYmFja2dyb3VuZDogdmFyKC0taGVhZC1iZyk7IH0KICB0ZDpmaXJzdC1jaGlsZCB7IGZvbnQtZmFtaWx5OiBDb25zb2xhcywgbW9ub3NwYWNlOyBjb2xvcjogI2U2Y2M4Nzsgd2hpdGUtc3BhY2U6IG5vd3JhcDsgfQogIC5mb3JtdWxhIHsgYmFja2dyb3VuZDogdmFyKC0tY29kZS1iZyk7IGJvcmRlci1sZWZ0OiAzcHggc29saWQgdmFyKC0tYWNjZW50KTsgYm9yZGVyLXJhZGl1czogMCA4cHggOHB4IDA7IHBhZGRpbmc6IDE0cHggMThweDsgbWFyZ2luOiAxMnB4IDA7IGZvbnQtZmFtaWx5OiBDb25zb2xhcywgbW9ub3NwYWNlOyBmb250LXNpemU6IDE0cHg7IH0KICAuZm9ybXVsYSAubGJsIHsgY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxMXB4OyB0ZXh0LXRyYW5zZm9ybTogdXBwZXJjYXNlOyBtYXJnaW4tYm90dG9tOiA1cHg7IGZvbnQtZmFtaWx5OiAnU2Vnb2UgVUknLCBzYW5zLXNlcmlmOyB9CiAgLm5vdGUgICB7IGJhY2tncm91bmQ6IHJnYmEoODgsMTY2LDI1NSwuMDgpOyAgYm9yZGVyLWxlZnQ6IDNweCBzb2xpZCB2YXIoLS1hY2NlbnQpOyBib3JkZXItcmFkaXVzOiAwIDZweCA2cHggMDsgcGFkZGluZzogMTFweCAxNnB4OyBtYXJnaW46IDEycHggMDsgfQogIC53YXJuICAgeyBiYWNrZ3JvdW5kOiByZ2JhKDIxMCwxNTMsMzQsLjA4KTsgIGJvcmRlci1sZWZ0OiAzcHggc29saWQgdmFyKC0td2Fybik7ICAgYm9yZGVyLXJhZGl1czogMCA2cHggNnB4IDA7IHBhZGRpbmc6IDExcHggMTZweDsgbWFyZ2luOiAxMnB4IDA7IH0KICAub2sgICAgIHsgYmFja2dyb3VuZDogcmdiYSg2MywxODUsODAsLjA4KTsgICBib3JkZXItbGVmdDogM3B4IHNvbGlkIHZhcigtLW9rKTsgICAgIGJvcmRlci1yYWRpdXM6IDAgNnB4IDZweCAwOyBwYWRkaW5nOiAxMXB4IDE2cHg7IG1hcmdpbjogMTJweCAwOyB9CiAgLmRhbmdlciB7IGJhY2tncm91bmQ6IHJnYmEoMjQ4LDgxLDczLC4wOCk7ICAgYm9yZGVyLWxlZnQ6IDNweCBzb2xpZCB2YXIoLS1lcnIpOyAgICBib3JkZXItcmFkaXVzOiAwIDZweCA2cHggMDsgcGFkZGluZzogMTFweCAxNnB4OyBtYXJnaW46IDEycHggMDsgfQogIC5ub3RlIHN0cm9uZyAgIHsgY29sb3I6IHZhcigtLWFjY2VudCk7IGRpc3BsYXk6IGJsb2NrOyBtYXJnaW4tYm90dG9tOiAzcHg7IH0KICAud2FybiBzdHJvbmcgICB7IGNvbG9yOiB2YXIoLS13YXJuKTsgICBkaXNwbGF5OiBibG9jazsgbWFyZ2luLWJvdHRvbTogM3B4OyB9CiAgLm9rIHN0cm9uZyAgICAgeyBjb2xvcjogdmFyKC0tb2spOyAgICAgZGlzcGxheTogYmxvY2s7IG1hcmdpbi1ib3R0b206IDNweDsgfQogIC5kYW5nZXIgc3Ryb25nIHsgY29sb3I6IHZhcigtLWVycik7ICAgIGRpc3BsYXk6IGJsb2NrOyBtYXJnaW4tYm90dG9tOiAzcHg7IH0KICAuYmFkZ2UgeyBkaXNwbGF5OiBpbmxpbmUtYmxvY2s7IHBhZGRpbmc6IDJweCA4cHg7IGJvcmRlci1yYWRpdXM6IDIwcHg7IGZvbnQtc2l6ZTogMTFweDsgZm9udC13ZWlnaHQ6IDYwMDsgbWFyZ2luLWxlZnQ6IDhweDsgdmVydGljYWwtYWxpZ246IG1pZGRsZTsgfQogIC5iYWRnZS5uZXcgIHsgYmFja2dyb3VuZDogcmdiYSg2MywxODUsODAsLjIpOyAgY29sb3I6IHZhcigtLW9rKTsgfQogIC5iYWRnZS5icmsgIHsgYmFja2dyb3VuZDogcmdiYSgyNDgsODEsNzMsLjIpOyAgY29sb3I6IHZhcigtLWVycik7IH0KICAuYmFkZ2Uub3B0ICB7IGJhY2tncm91bmQ6IHJnYmEoODgsMTY2LDI1NSwuMik7IGNvbG9yOiB2YXIoLS1hY2NlbnQpOyB9CiAgLmZsb3cgeyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDVweDsgZmxleC13cmFwOiB3cmFwOyBtYXJnaW46IDEycHggMDsgfQogIC5mYm94IHsgYmFja2dyb3VuZDogdmFyKC0tcGFuZWwpOyBib3JkZXI6IDFweCBzb2xpZCB2YXIoLS1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiA2cHg7IHBhZGRpbmc6IDdweCAxM3B4OyBmb250LXNpemU6IDEzcHg7IHRleHQtYWxpZ246IGNlbnRlcjsgfQogIC5mYm94LmFjdCB7IGJvcmRlci1jb2xvcjogdmFyKC0tYWNjZW50KTsgY29sb3I6IHZhcigtLWFjY2VudCk7IH0KICAuZmJveC5vayAgeyBib3JkZXItY29sb3I6IHZhcigtLW9rKTsgICAgIGNvbG9yOiB2YXIoLS1vayk7IH0KICAuZmJveC5ybSAgeyBib3JkZXItY29sb3I6IHZhcigtLWVycik7ICAgIGNvbG9yOiB2YXIoLS1lcnIpOyB0ZXh0LWRlY29yYXRpb246IGxpbmUtdGhyb3VnaDsgb3BhY2l0eTogMC41OyB9CiAgLmFyciB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMThweDsgfQogIGhyIHsgYm9yZGVyOiBub25lOyBib3JkZXItdG9wOiAxcHggc29saWQgdmFyKC0tYm9yZGVyKTsgbWFyZ2luOiAzNHB4IDA7IH0KICAudnRhZyB7IGNvbG9yOiB2YXIoLS1tdXRlZCk7IGZvbnQtc2l6ZTogMTNweDsgbWFyZ2luLWxlZnQ6IDEwcHg7IGZvbnQtZmFtaWx5OiBDb25zb2xhcywgbW9ub3NwYWNlOyB9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9IndyYXBwZXIiPgo8bmF2PgogIDxkaXYgY2xhc3M9Im5hdi1sb2dvIj4KICAgIDxoMj7imqEgV2VsZEZpbmQgdjQuNzwvaDI+CiAgICA8c3Bhbj5NYW51YWxlIFRlY25pY28gQ29tcGxldG88L3NwYW4+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ic2VjIj5JbnRyb2R1emlvbmU8L2Rpdj4KICA8YSBocmVmPSIjb3ZlcnZpZXciPlBhbm9yYW1pY2Egc2lzdGVtYTwvYT4KICA8YSBocmVmPSIjY2hhbmdlbG9nIj5DaGFuZ2Vsb2cgdmVyc2lvbmk8L2E+CiAgPGRpdiBjbGFzcz0ic2VjIj5BbGdvcml0bW8gUExDIChTQ0wpPC9kaXY+CiAgPGEgaHJlZj0iI2Zsb3ciPkZsdXNzbyBlc2VjdXppb25lPC9hPgogIDxhIGhyZWY9IiNyZXNldCI+UmVzZXQgZSBpbml6aWFsaXp6YXppb25lPC9hPgogIDxhIGhyZWY9IiNmaWx0ZXIiPkZpbHRybyBhY3F1aXNpemlvbmU8L2E+CiAgPGEgaHJlZj0iI2Jhc2VsaW5lIj5CYXNlbGluZSBsb29rLWJhY2s8L2E+CiAgPGEgaHJlZj0iI2FkYXB0Ij5CYXNlbGluZSBhZGF0dGl2YSB2NC42L3Y0Ljc8L2E+CiAgPGEgaHJlZj0iI2ZsYXR3YWl0Ij5BdHRlc2Egc3VwLiBwaWF0dGEgdjQuNjwvYT4KICA8YSBocmVmPSIjdGhyZXNob2xkcyI+Q2FsY29sbyBzb2dsaWU8L2E+CiAgPGEgaHJlZj0iI2NsdXN0ZXIiPkxvZ2ljYSBjbHVzdGVyPC9hPgogIDxhIGhyZWY9IiNkZXRlY3Rpb24iPkRldGVjdGlvbiB2NC43PC9hPgogIDxhIGhyZWY9IiNwb3N0ZGV0Ij5Qb3N0LWRldGVjdGlvbiB0cmF2ZWw8L2E+CiAgPGEgaHJlZj0iI3BvbGFyaXR5Ij5Qb2xhcml0w6AgcGljY28gdjQuMzwvYT4KICA8ZGl2IGNsYXNzPSJzZWMiPlBhcmFtZXRyaSBTQ0w8L2Rpdj4KICA8YSBocmVmPSIjcGFyYW0tZmlsdGVyIj5GaWx0cm88L2E+CiAgPGEgaHJlZj0iI3BhcmFtLXRocmVzaCI+U29nbGllPC9hPgogIDxhIGhyZWY9IiNwYXJhbS1jbHVzdGVyIj5DbHVzdGVyPC9hPgogIDxhIGhyZWY9IiNwYXJhbS12NDYiPnY0LjYgb3B6aW9uYWxpPC9hPgogIDxkaXYgY2xhc3M9InNlYyI+U29mdHdhcmUgUHl0aG9uPC9kaXY+CiAgPGEgaHJlZj0iI3RhYnMiPlN0cnV0dHVyYSB0YWI8L2E+CiAgPGEgaHJlZj0iI3NpbSI+U2ltdWxhdG9yZTwvYT4KICA8YSBocmVmPSIjc3RhdHMiPlN0YXRpc3RpY2hlIC8gR3JpZCBzZWFyY2g8L2E+CiAgPGEgaHJlZj0iI2F1dG9leHAiPkF1dG8gRXhwb3J0PC9hPgogIDxhIGhyZWY9IiNzcWxpdGUiPlNRTGl0ZTwvYT4KICA8ZGl2IGNsYXNzPSJzZWMiPkRpYWdub3N0aWNhPC9kaXY+CiAgPGEgaHJlZj0iI2Vycm9ycyI+Q29kaWNpIGVycm9yZTwvYT4KICA8YSBocmVmPSIjdHVuaW5nIj5HdWlkYSB0dW5pbmc8L2E+CiAgPGEgaHJlZj0iI2Zsb2F0MzIiPk5vdGUgZmxvYXQzMjwvYT4KICA8YSBocmVmPSIjdjQ3LWZhbHNlLXBvcyI+RmFsc2kgcG9zaXRpdmkgdjQuNzwvYT4KPC9uYXY+Cgo8bWFpbj4KCjxzZWN0aW9uIGlkPSJvdmVydmlldyI+CjxoMT5XZWxkRmluZCB2NC43IOKAlCBNYW51YWxlIFRlY25pY28gPHNwYW4gY2xhc3M9InZ0YWciPlNDTCB2NC43IMK3IFB5dGhvbiB2NC4zLjkzPC9zcGFuPjwvaDE+Cgo8cD5XZWxkRmluZCDDqCB1biBzaXN0ZW1hIGRpIHJpbGV2YW1lbnRvIGluIHRlbXBvIHJlYWxlIGRlbGxhIHBvc2l6aW9uZSBkaSBzYWxkYXR1cmEgKG8gYnVjbykgc3UgY2lsaW5kcmkgcm90YW50aSBvIHBhcnRpIGluIG1vdmltZW50byBsaW5lYXJlLiDDiCBjb21wb3N0byBkYSBkdWUgY29tcG9uZW50aSBwcmluY2lwYWxpOjwvcD4KCjx0YWJsZT4KPHRyPjx0aD5Db21wb25lbnRlPC90aD48dGg+VGVjbm9sb2dpYTwvdGg+PHRoPkZ1bnppb25lPC90aD48L3RyPgo8dHI+PHRkPkZiOTU0X1dlbGRGaW5kVjQ3PC90ZD48dGQ+U0NMIOKAlCBTNy0xNTAwPC90ZD48dGQ+QWxnb3JpdG1vIHJlYWwtdGltZTogYWNxdWlzaXNjZSBjYW1waW9uaSBsYXNlcitlbmNvZGVyLCByaWxldmEgbGEgc2FsZGF0dXJhIGR1cmFudGUgaWwgbW92aW1lbnRvPC90ZD48L3RyPgo8dHI+PHRkPndlbGRfdmlld2VyX3Y0MzwvdGQ+PHRkPlB5dGhvbiAvIFRraW50ZXI8L3RkPjx0ZD5BbmFsaXNpIG9mZmxpbmUsIHNpbXVsYXppb25lLCBncmlkIHNlYXJjaCBwYXJhbWV0cmksIGFyY2hpdmlvIFNRTGl0ZSwgYXV0byBleHBvcnQgbXVsdGktREI8L3RkPjwvdHI+CjwvdGFibGU+Cgo8ZGl2IGNsYXNzPSJub3RlIj4KPHN0cm9uZz7wn5OMIFByaW5jaXBpbyBkaSBmdW56aW9uYW1lbnRvPC9zdHJvbmc+CklsIHNlbnNvcmUgbGFzZXIgbWlzdXJhIGxhIGRpc3RhbnphIGRhbGxhIHN1cGVyZmljaWUgZGVsIHBlenpvIG1lbnRyZSBydW90YS4gTGEgc2FsZGF0dXJhIChvIGJ1Y28pIHByb2R1Y2UgdW4nYW5vbWFsaWEgbmVsbGEgZGlzdGFuemEuIEwnYWxnb3JpdG1vIHJpbGV2YSBxdWVzdGEgYW5vbWFsaWEgY29uZnJvbnRhbmRvIG9nbmkgY2FtcGlvbmUgY29uIHVuYSA8c3Ryb25nPmJhc2VsaW5lIGxvY2FsZSBhZGF0dGl2YTwvc3Ryb25nPiBjYWxjb2xhdGEgc3UgdW5hIGZpbmVzdHJhIGFuZ29sYXJlIHNjb3JyZXZvbGUuCjwvZGl2Pgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iY2hhbmdlbG9nIj4KPGgyPkNoYW5nZWxvZyB2ZXJzaW9uaTwvaDI+Cgo8aDM+djQuNyA8c3BhbiBjbGFzcz0iYmFkZ2UgbmV3Ij5jb3JyZW50ZTwvc3Bhbj48L2gzPgo8dGFibGU+Cjx0cj48dGg+TW9kaWZpY2E8L3RoPjx0aD5EZXNjcml6aW9uZTwvdGg+PHRoPkltcGF0dG88L3RoPjwvdHI+Cjx0cj48dGQ+QmFzZWxpbmUgYWRhdHRpdmEg4oCUIHRyYWNraW5nIGNvbnRpbnVvPC90ZD48dGQ+SWwgcmFuZ2UgW21pbiwgbWF4XSB2aWVuZSBhZ2dpb3JuYXRvIG9nbmkgY2ljbG8gY29uIGxhIG1lZGlhIGNvcnJlbnRlIChub24gcGnDuSBvbmUtc2hvdCBhbGxhIHByaW1hIHN0YWJpbGl6emF6aW9uZSkuIElsIGZpbHRybyBkaXZlbnRhIGF0dGl2byBzb2xvIGRvcG8gPGNvZGU+YkFuZ2xlRXhjZWVkZWQ9VFJVRTwvY29kZT4uPC90ZD48dGQ+UGnDuSByb2J1c3RvIHN1IHN1cGVyZmljaSBjb24gZHJpZnQgbGVudG88L3RkPjwvdHI+Cjx0cj48dGQ+RWxpbWluYXppb25lIHNjYW4gcmV0cm9hdHRpdm8gKEYtYmlzKTwvdGQ+PHRkPkxvIHNjYW4gcmV0cm9hdHRpdm8gc3VpIGNhbXBpb25pIGluIGRlYWQgem9uZSDDqCBzdGF0byByaW1vc3NvLiBFcmEgY2F1c2EgZGkgZmFsc2kgcG9zaXRpdmk6IGxhIGJhc2VsaW5lIHRhcmRhIGNvbmZyb250YXRhIGNvbiBjYW1waW9uaSBpbml6aWFsaSBsaSBjbGFzc2lmaWNhdmEgZXJyb25lYW1lbnRlIGNvbWUgYnVjaGUuPC90ZD48dGQ+UmlkdXppb25lIGZhbHNpIHBvc2l0aXZpIGNvbiBkcmlmdDwvdGQ+PC90cj4KPHRyPjx0ZD5EZWFkIHpvbmUg4oaSIGJBbmdsZUV4Y2VlZGVkPC90ZD48dGQ+TGEgZ3VhcmQgZGkgZGV0ZWN0aW9uIHVzYSBpbCBmbGFnIDxjb2RlPiNiQW5nbGVFeGNlZWRlZDwvY29kZT4gaW52ZWNlIGRlbCBjb25mcm9udG8gZmxvYXQgPGNvZGU+I3JUb3RhbFRyYXZlbCDiiaUgI0lfQmFzZWxpbmVXaW5kb3dEZWc8L2NvZGU+LiBTZW1hbnRpY2FtZW50ZSBpZGVudGljbywgcGnDuSByb2J1c3RvIChmbGFnIHBlcnNpc3RlbnRlKS48L3RkPjx0ZD5OZXNzdW4gaW1wYXR0byBmdW56aW9uYWxlPC90ZD48L3RyPgo8L3RhYmxlPgoKPGgzPnY0LjY8L2gzPgo8dGFibGU+Cjx0cj48dGg+TW9kaWZpY2E8L3RoPjx0aD5QYXJhbWV0cmk8L3RoPjwvdHI+Cjx0cj48dGQ+QmFzZWxpbmUgYWRhdHRpdmEgKG9wemlvbmFsZSk8L3RkPjx0ZD48Y29kZT5JX0FkYXB0QmFzZWxpbmVFbmFibGU8L2NvZGU+LCA8Y29kZT5JX0FkYXB0QmFzZWxpbmVPZmZzZXQ8L2NvZGU+PC90ZD48L3RyPgo8dHI+PHRkPkF0dGVzYSBzdXBlcmZpY2llIHBpYXR0YSAob3B6aW9uYWxlKTwvdGQ+PHRkPjxjb2RlPklfRmxhdFdhaXRFbmFibGU8L2NvZGU+LCA8Y29kZT5JX0ZsYXRXYWl0U2FtcGxlczwvY29kZT4sIDxjb2RlPklfRmxhdFdhaXRUb2xsPC9jb2RlPjwvdGQ+PC90cj4KPC90YWJsZT4KCjxoMz52NC41PC9oMz4KPHA+SW50cm9kb3R0byA8Y29kZT5iV2VsZERldGVjdGVkSW50ZXJuYWw8L2NvZGU+OiBsYSBmbGFnIHB1YmJsaWNhIDxjb2RlPlRyb3ZhdGE8L2NvZGU+IHZpZW5lIGFsemF0YSBzb2xvIGRvcG8gYXZlciBwZXJjb3JzbyA8Y29kZT5yUG9zdERldFRyYXZlbERlZ0MgPSAywrA8L2NvZGU+IGRhbGwnaXN0YW50ZSBkaSBjb25mZXJtYSBkZWwgY2x1c3Rlci48L3A+Cgo8aDM+djQuNDwvaDM+CjxwPkRlYWQgem9uZSBiYXNlbGluZSArIHNjYW4gcmV0cm9hdHRpdm8gKHBvaSByaW1vc3NvIGluIHY0LjcpLiBQb3N0LWRldGVjdGlvbiB0cmF2ZWwgYWNjdW11bGF0b3IuPC9wPgoKPGgzPnY0LjM8L2gzPgo8cD48Y29kZT5JX1BlYWtQb2xhcml0eTwvY29kZT4gKDA9cG9zaXRpdm8sIDE9bmVnYXRpdm8sIDI9ZW50cmFtYmkpLCA8Y29kZT5PX0RldGVjdGVkUG9sYXJpdHk8L2NvZGU+LiBTdXBwb3J0byBDVy9DQ1cgY29uIGRpc3RhbnphIGFuZ29sYXJlIGJpZGlyZXppb25hbGUuPC9wPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iZmxvdyI+CjxoMj5GbHVzc28gZGkgZXNlY3V6aW9uZSBwZXIgY2ljbG8gUExDPC9oMj4KCjxkaXYgY2xhc3M9ImZsb3ciPgogIDxkaXYgY2xhc3M9ImZib3giPklfTGFzZXJWYWx1ZTxicj5JX0N1cnJlbnRBbmdsZTwvZGl2PgogIDxkaXYgY2xhc3M9ImFyciI+4oaSPC9kaXY+CiAgPGRpdiBjbGFzcz0iZmJveCI+QWdnaW9ybmE8YnI+QWN0dWFsVmFsdWU8YnI+VmFsaWRSYW5nZTwvZGl2PgogIDxkaXYgY2xhc3M9ImFyciI+4oaSPC9kaXY+CiAgPGRpdiBjbGFzcz0iZmJveCBhY3QiPkVkZ2UgcmVzZXQ8YnI+KENsZWFyUmVxIOKGkTxicj5FbmFibGUg4oaRKTwvZGl2PgogIDxkaXYgY2xhc3M9ImFyciI+4oaSPC9kaXY+CiAgPGRpdiBjbGFzcz0iZmJveCI+QSkgR3VhcmQ8YnI+YkFuZ2xlRXhjZWVkZWQ8L2Rpdj4KICA8ZGl2IGNsYXNzPSJhcnIiPuKGkjwvZGl2PgogIDxkaXYgY2xhc3M9ImZib3giPkIpIEZpbHRybzxicj7OlEFuZyArIM6UTGFzZXI8L2Rpdj4KICA8ZGl2IGNsYXNzPSJhcnIiPuKGkjwvZGl2PgogIDxkaXYgY2xhc3M9ImZib3ggYWN0Ij5DKSBTYWx2YTxicj5idWZmZXI8L2Rpdj4KPC9kaXY+CjxkaXYgY2xhc3M9ImZsb3ciIHN0eWxlPSJtYXJnaW4tdG9wOi02cHgiPgogIDxkaXYgY2xhc3M9ImZib3giPkMtYmlzKSBGbGF0V2FpdDxicj5pRmxhdENvbnNlY0NvdW50PC9kaXY+CiAgPGRpdiBjbGFzcz0iYXJyIj7ihpI8L2Rpdj4KICA8ZGl2IGNsYXNzPSJmYm94Ij5BLWJpcykgUG9zdC1kZXQ8YnI+dHJhdmVsICsywrA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJhcnIiPuKGkjwvZGl2PgogIDxkaXYgY2xhc3M9ImZib3ggYWN0Ij5EKSBMb29rLWJhY2s8YnI+Rk9SIGo9MC4uY3VyLTE8L2Rpdj4KICA8ZGl2IGNsYXNzPSJhcnIiPuKGkjwvZGl2PgogIDxkaXYgY2xhc3M9ImZib3giPkUpIENsdXN0ZXI8YnI+b25saW5lPC9kaXY+CiAgPGRpdiBjbGFzcz0iYXJyIj7ihpI8L2Rpdj4KICA8ZGl2IGNsYXNzPSJmYm94IG9rIj5GKSBEZXRlY3Rpb248YnI+KGJBbmdsZUV4Y2VlZGVkKTwvZGl2PgogIDxkaXYgY2xhc3M9ImFyciI+4oaSPC9kaXY+CiAgPGRpdiBjbGFzcz0iZmJveCBybSI+Ri1iaXMgKHJpbW9zc2E8YnI+aW4gdjQuNyk8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGNsYXNzPSJ3YXJuIj4KPHN0cm9uZz7imqAgU2luZ2xlLXRocmVhZCBQTEM8L3N0cm9uZz4KSWwgRk9SIGludGVybm8gZGVsIGxvb2stYmFjayBzY2Fuc2lvbmEgYWwgbWFzc2ltbyA8Y29kZT5pU2FtcGxlc0FjcXVpcmVkIC0gMTwvY29kZT4gY2FtcGlvbmkgcHJlY2VkZW50aSAobWF4IDIwMDApLiBOZXNzdW4gV0hJTEUgbG9vcCDigJQgc29sbyBGT1IgY29uIGxpbWl0ZSBzdXBlcmlvcmUgZmlzc28gcGVyIHNpY3VyZXp6YSB3YXRjaGRvZy4KPC9kaXY+Cjwvc2VjdGlvbj4KCjxocj4KCjxzZWN0aW9uIGlkPSJyZXNldCI+CjxoMj5SZXNldCBlIGluaXppYWxpenphemlvbmU8L2gyPgoKPHA+SWwgYmxvY2NvIGRpIHJlc2V0IHNpIGF0dGl2YSBzdSBkdWUgZnJvbnRpIGRpc3RpbnRpICh1bmlmaWNhdGkgaW4gVjQ2Kyk6PC9wPgoKPGRpdiBjbGFzcz0iZm9ybXVsYSI+CjxkaXYgY2xhc3M9ImxibCI+Q29uZGl6aW9uZSByZXNldCAoU0NMIHJpZ2EgMTc4KTwvZGl2Pgo8c3BhbiBjbGFzcz0ia3ciPklGPC9zcGFuPiAoQ2xlYXJSZXEgPHNwYW4gY2xhc3M9Imt3Ij5BTkQgTk9UPC9zcGFuPiBiUmVzZXRfUHJldikgPHNwYW4gY2xhc3M9Im9wIj5PUjwvc3Bhbj4gKEVuYWJsZVJpY2VyY2EgPHNwYW4gY2xhc3M9Imt3Ij5BTkQgTk9UPC9zcGFuPiBiU3RhcnRfUHJldikgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJjbXQiPi8vIEZyb250ZSBDbGVhclJlcSDihpIgaVN0YXRlIDo9IDAgKElkbGUpPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJjbXQiPi8vIEZyb250ZSBFbmFibGVSaWNlcmNhIOKGkiBpU3RhdGUgOj0gMSAoT25saW5lKTwvc3Bhbj4KPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CjwvZGl2PgoKPHA+VmFyaWFiaWxpIHJlc2V0dGF0ZSBlc3BsaWNpdGFtZW50ZTogdHV0dGUgcXVlbGxlIGRpIHN0YXRvIHJpbGV2YW50aSBwZXIgbGEgcmljZXJjYSBpbmNsdXNlIGxlIHZhcmlhYmlsaSB2NC42ICg8Y29kZT5iQWRhcHRCYXNlbGluZVNldDwvY29kZT4sIDxjb2RlPnJBZGFwdEJhc2VsaW5lTWluL01heDwvY29kZT4sIDxjb2RlPmJGbGF0U3VyZmFjZUZvdW5kPC9jb2RlPiwgPGNvZGU+aUZsYXRDb25zZWNDb3VudDwvY29kZT4pLCBpIGJ1ZmZlciBhcnJheSwgaSBjb250YXRvcmkgY2x1c3Rlci48L3A+Cgo8cD5WYXJpYWJpbGkgPHN0cm9uZz5ub248L3N0cm9uZz4gcmVzZXR0YXRlIGVzcGxpY2l0YW1lbnRlIChtYSBzZW56YSBpbXBhdHRpKTo8L3A+Cjx0YWJsZT4KPHRyPjx0aD5WYXJpYWJpbGU8L3RoPjx0aD5QZXJjaMOpIMOoIHNpY3VyYTwvdGg+PC90cj4KPHRyPjx0ZD5pQ2x1c3RlclN0YXJ0LCByQ2x1c3RlclBlYWssIHJDbHVzdGVyUGVha0RldiwgaVBlYWtJbmRleDwvdGQ+PHRkPkxldHRlIHNvbG8gcXVhbmRvIDxjb2RlPmJJbkNsdXN0ZXI9VFJVRTwvY29kZT4gKHJlc2V0dGF0byk8L3RkPjwvdHI+Cjx0cj48dGQ+aUJlc3RTdGFydC9FbmQvUGVha0lkeC9Db3VudCwgckJlc3RQZWFrQWJzPC90ZD48dGQ+U292cmFzY3JpdHRlIGFsIHByaW1vIGNsdXN0ZXIgdmFsaWRvIChnYXJhbnRpdG8gZGEgPGNvZGU+ckJlc3RQZWFrRGV2QWJzIDo9IC0xLjA8L2NvZGU+KTwvdGQ+PC90cj4KPHRyPjx0ZD5yTSwgclMsIHJUaEhpL0xvL0hpTmVnL0xvTmVnPC90ZD48dGQ+UmljYWxjb2xhdGUgZnJlc2ggb2duaSBjaWNsbyBuZWwgYmxvY2NvIEQ8L3RkPjwvdHI+Cjx0cj48dGQ+cldpblN1bSwgcldpblN1bVNxLCBpV2luTiwgaiwgckFuZ0RpZmbigKY8L3RkPjx0ZD5WYXJpYWJpbGkgdGVtcG9yYW5lZSBkaSBjaWNsbywgc292cmFzY3JpdHRlIHByaW1hIGRpIG9nbmkgdXNvPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iZmlsdGVyIj4KPGgyPkZpbHRybyBhY3F1aXNpemlvbmU8L2gyPgo8cD5PZ25pIGNhbXBpb25lIGdyZXp6byB2aWVuZSBhY2NldHRhdG8gc29sbyBzZSA8c3Ryb25nPnR1dHRlIGUgNDwvc3Ryb25nPiBsZSBjb25kaXppb25pIHNvbm8gdmVyZTo8L3A+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5Db25kaXppb25lIGRpIGFjY2V0dGF6aW9uZTwvZGl2PgpyQW5nRGlmZiDiiaUgSV9NaW5BbmdsZURlbHRhICA8c3BhbiBjbGFzcz0ia3ciPkFORDwvc3Bhbj4Kckxhc2VyRGlmZiDiiaUgSV9NaW5MYXNlckRlbHRhICA8c3BhbiBjbGFzcz0ia3ciPkFORDwvc3Bhbj4KVmFsaWRSYW5nZVZhbHVlICA8c3BhbiBjbGFzcz0ia3ciPkFORDwvc3Bhbj4KPHNwYW4gY2xhc3M9Imt3Ij5OT1Q8L3NwYW4+IElfQXhpc1RhbmRTdGVlbAo8L2Rpdj4KCjxkaXYgY2xhc3M9Im5vdGUiPgo8c3Ryb25nPvCfk4wgUHJpbW8gY2FtcGlvbmU8L3N0cm9uZz4KPGNvZGU+ckFuZ0RpZmYgOj0gMS4wPC9jb2RlPiBwZXIgYnlwYXNzIGdhcmFudGl0byBzdWwgcHJpbW8gY2FtcGlvbmUgKG5lc3N1biAibGFzdCBzYXZlZCIgZGlzcG9uaWJpbGUpLgo8L2Rpdj4KCjxoMyBpZD0icGFyYW0tZmlsdGVyIj5QYXJhbWV0cmkgZmlsdHJvPC9oMz4KPHRhYmxlPgo8dHI+PHRoPlBhcmFtZXRybyBTQ0w8L3RoPjx0aD5EZWZhdWx0PC90aD48dGg+RGVzY3JpemlvbmU8L3RoPjwvdHI+Cjx0cj48dGQ+SV9NaW5BbmdsZURlbHRhPC90ZD48dGQ+MC41wrA8L3RkPjx0ZD5WYXJpYXppb25lIGFuZ29sYXJlIG1pbmltYSB0cmEgZHVlIGNhbXBpb25pIGFjY2V0dGF0aSBjb25zZWN1dGl2aS4gUGVyIGNvcnNhIGxpbmVhcmUgYnJldmUgKH4zMG1tLCAxNS0yMCBjYW1waW9uaSk6IHVzYXJlIDAuMDAxwrAuPC90ZD48L3RyPgo8dHI+PHRkPklfTWluTGFzZXJEZWx0YTwvdGQ+PHRkPjAuMSBtbTwvdGQ+PHRkPlZhcmlhemlvbmUgbGFzZXIgbWluaW1hLiBGaWx0cmEgY2FtcGlvbmkgImZlcm1pIi4gUGVyIHN1cGVyZmljaSBsaXNjZSByaWR1cnJlIGEgMC4wMDEuPC90ZD48L3RyPgo8dHI+PHRkPklfTWluTGFzZXJWYWxpZFZhbHVlPC90ZD48dGQ+MC4wIG1tPC90ZD48dGQ+TGltaXRlIGluZmVyaW9yZSByYW5nZSB2YWxpZG8uIENhbXBpb25pIHNvdHRvIHZlbmdvbm8gc2NhcnRhdGkuPC90ZD48L3RyPgo8dHI+PHRkPklfTWF4TGFzZXJWYWxpZFZhbHVlPC90ZD48dGQ+4oCUPC90ZD48dGQ+TGltaXRlIHN1cGVyaW9yZSByYW5nZSB2YWxpZG8uIENhbXBpb25pIHNvcHJhIHZlbmdvbm8gc2NhcnRhdGkuPC90ZD48L3RyPgo8dHI+PHRkPklfQXhpc1RhbmRTdGVlbDwvdGQ+PHRkPkZBTFNFPC90ZD48dGQ+VFJVRSA9IGFzc2UgZmVybW8g4oaSIHR1dHRpIGkgY2FtcGlvbmkgcmlmaXV0YXRpLjwvdGQ+PC90cj4KPC90YWJsZT4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9ImJhc2VsaW5lIj4KPGgyPkJhc2VsaW5lIGxvb2stYmFjayBhbmdvbGFyZTwvaDI+CjxwPlBlciBvZ25pIGNhbXBpb25lIGFjY2V0dGF0byBhbGwnaW5kaWNlIDxjb2RlPmlDdXJJZHg8L2NvZGU+LCBpbCBQTEMgY2FsY29sYSBtZWRpYSBlIHNpZ21hIGRlaSBjYW1waW9uaSBwcmVjZWRlbnRpIG5lbGxhIDxzdHJvbmc+ZmluZXN0cmEgYW5nb2xhcmU8L3N0cm9uZz4gPGNvZGU+WzHCsCwgSV9CYXNlbGluZVdpbmRvd0RlZ108L2NvZGU+OjwvcD4KCjxkaXYgY2xhc3M9ImZvcm11bGEiPgo8ZGl2IGNsYXNzPSJsYmwiPkZPUiBsb29wIFNDTCDigJQgYmxvY2NvIEQ8L2Rpdj4KPHNwYW4gY2xhc3M9Imt3Ij5GT1I8L3NwYW4+IGogOj0gMCA8c3BhbiBjbGFzcz0ia3ciPlRPPC9zcGFuPiBpU2FtcGxlc0FjcXVpcmVkIC0gMiA8c3BhbiBjbGFzcz0ia3ciPkRPPC9zcGFuPgogIHJBbmdEaWZmIDo9IEFCUyhhckFuZ2xlc1tpQ3VySWR4XSAtIGFyQW5nbGVzW2pdKQogIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IHJBbmdEaWZmID4gMTgwLjAgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPiByQW5nRGlmZiA6PSAzNjAuMCAtIHJBbmdEaWZmIDxzcGFuIGNsYXNzPSJrdyI+RU5EX0lGPC9zcGFuPiAgPHNwYW4gY2xhc3M9ImNtdCI+Ly8gc3VwcG9ydG8gQ1cvQ0NXPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IHJBbmdEaWZmID49IDxzcGFuIGNsYXNzPSJudW0iPjEuMDwvc3Bhbj4gPHNwYW4gY2xhc3M9Imt3Ij5BTkQ8L3NwYW4+IHJBbmdEaWZmIDw9IElfQmFzZWxpbmVXaW5kb3dEZWcgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogICAgPHNwYW4gY2xhc3M9ImNtdCI+Ly8gKioqIHY0LjcgKioqIHNlIGJhc2VsaW5lIGFkYXR0aXZhIGF0dGl2YTogZmlsdHJhIGNhbXBpb25pIGFub21hbGk8L3NwYW4+CiAgICA8c3BhbiBjbGFzcz0ia3ciPklGIE5PVDwvc3Bhbj4gSV9BZGFwdEJhc2VsaW5lRW5hYmxlIDxzcGFuIGNsYXNzPSJrdyI+T1IgTk9UPC9zcGFuPiBiQWRhcHRCYXNlbGluZVNldCA8c3BhbiBjbGFzcz0ia3ciPk9SPC9zcGFuPgogICAgICAgKGFyU2FtcGxlc1tqXSA+PSByQWRhcHRCYXNlbGluZU1pbiA8c3BhbiBjbGFzcz0ia3ciPkFORDwvc3Bhbj4gYXJTYW1wbGVzW2pdIDw9IHJBZGFwdEJhc2VsaW5lTWF4KSA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+CiAgICAgIHJXaW5TdW0gICArPSBhclNhbXBsZXNbal0KICAgICAgcldpblN1bVNxICs9IGFyU2FtcGxlc1tqXSAqIGFyU2FtcGxlc1tqXQogICAgICBpV2luTiAgICAgKz0gMQogICAgPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CiAgPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CjxzcGFuIGNsYXNzPSJrdyI+RU5EX0ZPUjwvc3Bhbj4KPC9kaXY+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5DYWxjb2xvIG1lYW4gZSBzaWdtYSAoZmxvYXQzMiBSRUFMIFBMQyk8L2Rpdj4Kck0gOj0gcldpblN1bSAvIElOVF9UT19SRUFMKGlXaW5OKQpyViA6PSByV2luU3VtU3EgLyBJTlRfVE9fUkVBTChpV2luTikgLSByTSAqIHJNCjxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IHJWICZsdDsgMCA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+IHJWIDo9IDAgPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CnJTIDo9IFNRUlQoclYpCjwvZGl2PgoKPGRpdiBjbGFzcz0id2FybiI+CjxzdHJvbmc+4pqgIEZpbmVzdHJhIG1pbmltYTwvc3Ryb25nPgpTZSA8Y29kZT5pV2luTiAmbHQ7IDM8L2NvZGU+LCBpbCBQTEMgcHJvcGFnYSBpIHZhbG9yaSBkZWwgY2FtcGlvbmUgcHJlY2VkZW50ZTogPGNvZGU+ck0gOj0gYXJNZWFuW2lDdXJJZHgtMV08L2NvZGU+LiBBY2NhZGUgdGlwaWNhbWVudGUgbmVpIHByaW1pc3NpbWkgY2FtcGlvbmkgZG92ZSBub24gY2kgc29ubyBhbmNvcmEgYWJiYXN0YW56YSBwdW50aSBuZWxsYSBmaW5lc3RyYS4KPC9kaXY+Cjwvc2VjdGlvbj4KCjxocj4KCjxzZWN0aW9uIGlkPSJhZGFwdCI+CjxoMj5CYXNlbGluZSBhZGF0dGl2YSA8c3BhbiBjbGFzcz0iYmFkZ2UgbmV3Ij52NC42L3Y0Ljc8L3NwYW4+PC9oMj4KCjxwPk9wemlvbmFsZSAoPGNvZGU+SV9BZGFwdEJhc2VsaW5lRW5hYmxlIDo9IEZBTFNFPC9jb2RlPiBkaSBkZWZhdWx0IOKAlCBjb21wb3J0YW1lbnRvIGlkZW50aWNvIGEgdjQuNSBzZSBkaXNhYmlsaXRhdGEpLjwvcD4KCjxoMz5Mb2dpY2EgVjQuNyDigJQgdHJhY2tpbmcgY29udGludW88L2gzPgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5BZ2dpb3JuYW1lbnRvIHJhbmdlIGFkYXR0aXZvIChvZ25pIGNpY2xvIGNvbiBpV2luTuKJpTMpPC9kaXY+CjxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IElfQWRhcHRCYXNlbGluZUVuYWJsZSA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+CiAgckFkYXB0QmFzZWxpbmVNaW4gOj0gck0gLSBJX0FkYXB0QmFzZWxpbmVPZmZzZXQKICByQWRhcHRCYXNlbGluZU1heCA6PSByTSArIElfQWRhcHRCYXNlbGluZU9mZnNldAogIGJBZGFwdEJhc2VsaW5lU2V0IDo9IGJBbmdsZUV4Y2VlZGVkICA8c3BhbiBjbGFzcz0iY210Ij4vLyBmaWx0cm8gYXR0aXZvIHNvbG8gZG9wbyBwcmltYSBmaW5lc3RyYTwvc3Bhbj4KPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CjwvZGl2PgoKPGgzPkRpZmZlcmVuemEgVjQuNiB2cyBWNC43PC9oMz4KPHRhYmxlPgo8dHI+PHRoPjwvdGg+PHRoPlY0LjYgKG9uZS1zaG90KTwvdGg+PHRoPlY0LjcgKHRyYWNraW5nKTwvdGg+PC90cj4KPHRyPjx0ZD5RdWFuZG8gc2kgY2FsaWJyYTwvdGQ+PHRkPlVuYSBzb2xhIHZvbHRhLCBhbGxhIHByaW1hIHN0YWJpbGl6emF6aW9uZSAoaVdpbk7iiaUzKTwvdGQ+PHRkPk9nbmkgY2ljbG8gaW4gY3VpIGlXaW5O4omlMzwvdGQ+PC90cj4KPHRyPjx0ZD5RdWFuZG8gc2kgYXR0aXZhIGlsIGZpbHRybzwvdGQ+PHRkPlN1Yml0byBkb3BvIGxhIHByaW1hIGNhbGlicmF6aW9uZTwvdGQ+PHRkPlNvbG8gZG9wbyA8Y29kZT5iQW5nbGVFeGNlZWRlZD1UUlVFPC9jb2RlPiAo4omlIElfQmFzZWxpbmVXaW5kb3dEZWcgcGVyY29yc2kpPC90ZD48L3RyPgo8dHI+PHRkPlN1cGVyZmljaSBjb24gZHJpZnQgbGVudG88L3RkPjx0ZD5SYW5nZSBibG9jY2F0byBzdWwgdmFsb3JlIGluaXppYWxlIOKGkiBwb3RyZWJiZSBlc2NsdWRlcmUgbGEgc3VwZXJmaWNpZSBzYW5hPC90ZD48dGQ+UmFuZ2Ugc2VndWUgaWwgZHJpZnQg4oaSIHBpw7kgcm9idXN0bzwvdGQ+PC90cj4KPHRyPjx0ZD5GZWVkYmFjazwvdGQ+PHRkPlN0YWJpbGUgKGNhbGlicmF6aW9uZSB1bmljYSk8L3RkPjx0ZD5TdGFiaWxlOiBsYSBtZWFuIGNhbGNvbGF0YSBlc2NsdWRlIGdpw6AgaSBidWNoaSBxdWFuZG8gaWwgZmlsdHJvIMOoIGF0dGl2bzwvdGQ+PC90cj4KPC90YWJsZT4KCjxoMz5Db21lIGZ1bnppb25hIGlsIGZpbHRybyBuZWxsYSBsb29rLWJhY2s8L2gzPgo8cD5RdWFuZG8gPGNvZGU+YkFkYXB0QmFzZWxpbmVTZXQ9VFJVRTwvY29kZT4sIG5lbCBibG9jY28gRCB2ZW5nb25vIGluY2x1c2kgbmVsbGEgc29tbWEgc29sbyBpIGNhbXBpb25pIDxjb2RlPmo8L2NvZGU+IHBlciBjdWkgPGNvZGU+YXJTYW1wbGVzW2pdIOKIiCBbckFkYXB0QmFzZWxpbmVNaW4sIHJBZGFwdEJhc2VsaW5lTWF4XTwvY29kZT4uIEkgY2FtcGlvbmkgYW5vbWFsaSAoYnVjaGkpIHZlbmdvbm8gZXNjbHVzaSBkYWwgY2FsY29sbyBkaSBtZWFuL3NpZ21hIG1hIHJpbWFuZ29ubyBuZWwgYnVmZmVyIGUgcG9zc29ubyBlc3NlcmUgcmlsZXZhdGkgbm9ybWFsbWVudGUgY29tZSBjbHVzdGVyLjwvcD4KCjxoMyBpZD0icGFyYW0tdjQ2Ij5QYXJhbWV0cmkgdjQuNiBvcHppb25hbGk8L2gzPgo8dGFibGU+Cjx0cj48dGg+UGFyYW1ldHJvIFNDTDwvdGg+PHRoPkRlZmF1bHQ8L3RoPjx0aD5EZXNjcml6aW9uZTwvdGg+PC90cj4KPHRyPjx0ZD5JX0FkYXB0QmFzZWxpbmVFbmFibGU8L3RkPjx0ZD5GQUxTRTwvdGQ+PHRkPkFiaWxpdGEgaWwgZmlsdHJvIHJhbmdlIGFkYXR0aXZvIG5lbCBsb29rLWJhY2suPC90ZD48L3RyPgo8dHI+PHRkPklfQWRhcHRCYXNlbGluZU9mZnNldDwvdGQ+PHRkPjMuMCBtbTwvdGQ+PHRkPlNlbWliYW5kYSBkZWwgcmFuZ2UgYWRhdHRpdm8gYXR0b3JubyBhbGxhIG1lZGlhLiBEZXZlIGVzc2VyZSAmZ3Q7IHZhcmlhemlvbmUgbm9ybWFsZSBzdXBlcmZpY2llIGUgJmx0OyBwcm9mb25kaXTDoCBidWNvLiBWYWxvcmUgdGlwaWNvOiB+Mi8zIGRlbGxhIHByb2ZvbmRpdMOgIGRlbCBidWNvLjwvdGQ+PC90cj4KPHRyPjx0ZD5JX0ZsYXRXYWl0RW5hYmxlPC90ZD48dGQ+RkFMU0U8L3RkPjx0ZD5BYmlsaXRhIGF0dGVzYSBzdXBlcmZpY2llIHBpYXR0YSBwcmltYSBkaSBkZXRlY3Rpb24uPC90ZD48L3RyPgo8dHI+PHRkPklfRmxhdFdhaXRTYW1wbGVzPC90ZD48dGQ+NTwvdGQ+PHRkPkNhbXBpb25pIHN0YWJpbGkgY29uc2VjdXRpdmkgcmljaGllc3RpIHByaW1hIGRpIGFiaWxpdGFyZSBkZXRlY3Rpb24uPC90ZD48L3RyPgo8dHI+PHRkPklfRmxhdFdhaXRUb2xsPC90ZD48dGQ+MC41IG1tPC90ZD48dGQ+VmFyaWF6aW9uZSBtYXNzaW1hIGludGVyLWNhbXBpb25lIHBlciBjb25zaWRlcmFyZSAicGlhbm8iLiBEZXZlIGVzc2VyZSAmbHQ7IHZhcmlhemlvbmUgcHJvZG90dGEgZGFpIGJ1Y2hpLjwvdGQ+PC90cj4KPC90YWJsZT4KCjxkaXYgY2xhc3M9Im5vdGUiPgo8c3Ryb25nPvCfk4wgU2V0dXAgcmFjY29tYW5kYXRvIHBlciBidWNoaSBzdSBhbmVsbG88L3N0cm9uZz4KPGNvZGU+SV9BZGFwdEJhc2VsaW5lRW5hYmxlPVRSVUU8L2NvZGU+LCA8Y29kZT5JX0FkYXB0QmFzZWxpbmVPZmZzZXQ8L2NvZGU+ID0gfjIvMyBwcm9mb25kaXTDoCBidWNvLCA8Y29kZT5JX0ZsYXRXYWl0RW5hYmxlPVRSVUU8L2NvZGU+LCA8Y29kZT5JX0ZsYXRXYWl0U2FtcGxlcz01PC9jb2RlPiwgPGNvZGU+SV9GbGF0V2FpdFRvbGw9MC4zLi4wLjU8L2NvZGU+LCA8Y29kZT5JX1BlYWtQb2xhcml0eT0xPC9jb2RlPiAobmVnYXRpdm8pLCA8Y29kZT5JX1N0b3BPbldlbGQ9RkFMU0U8L2NvZGU+Lgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9ImZsYXR3YWl0Ij4KPGgyPkF0dGVzYSBzdXBlcmZpY2llIHBpYXR0YSA8c3BhbiBjbGFzcz0iYmFkZ2UgbmV3Ij52NC42PC9zcGFuPjwvaDI+Cgo8cD5PcHppb25hbGUgKDxjb2RlPklfRmxhdFdhaXRFbmFibGUgOj0gRkFMU0U8L2NvZGU+IGRpIGRlZmF1bHQpLiBCbG9jY2EgbGEgZGV0ZWN0aW9uIChibG9jY28gRikgZmluY2jDqSBub24gc2kgdHJvdmFubyA8Y29kZT5JX0ZsYXRXYWl0U2FtcGxlczwvY29kZT4gY2FtcGlvbmkgY29uc2VjdXRpdmkgY29uIHZhcmlhemlvbmUgaW50ZXItY2FtcGlvbmUg4omkIDxjb2RlPklfRmxhdFdhaXRUb2xsPC9jb2RlPi48L3A+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5CbG9jY28gQy1iaXMgKHBlciBvZ25pIGNhbXBpb25lIGFjY2V0dGF0byk8L2Rpdj4KPHNwYW4gY2xhc3M9Imt3Ij5JRjwvc3Bhbj4gSV9GbGF0V2FpdEVuYWJsZSA8c3BhbiBjbGFzcz0ia3ciPkFORCBOT1Q8L3NwYW4+IGJGbGF0U3VyZmFjZUZvdW5kIDxzcGFuIGNsYXNzPSJrdyI+QU5EPC9zcGFuPiBpQ3VySWR4ID4gMCA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+CiAgPHNwYW4gY2xhc3M9Imt3Ij5JRjwvc3Bhbj4gQUJTKGFyU2FtcGxlc1tpQ3VySWR4XSAtIGFyU2FtcGxlc1tpQ3VySWR4LTFdKSAmbHQ7PSBJX0ZsYXRXYWl0VG9sbCA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+CiAgICBpRmxhdENvbnNlY0NvdW50ICs9IDEKICAgIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IGlGbGF0Q29uc2VjQ291bnQgPj0gSV9GbGF0V2FpdFNhbXBsZXMgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogICAgICBiRmxhdFN1cmZhY2VGb3VuZCA6PSA8c3BhbiBjbGFzcz0ia3ciPlRSVUU8L3NwYW4+CiAgICA8c3BhbiBjbGFzcz0ia3ciPkVORF9JRjwvc3Bhbj4KICA8c3BhbiBjbGFzcz0ia3ciPkVMU0U8L3NwYW4+CiAgICBpRmxhdENvbnNlY0NvdW50IDo9IDAgIDxzcGFuIGNsYXNzPSJjbXQiPi8vIGNhbXBpb25lIGluc3RhYmlsZSAoZXMuIGJ1Y28pOiByaWNvbWluY2lhPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJrdyI+RU5EX0lGPC9zcGFuPgo8c3BhbiBjbGFzcz0ia3ciPkVORF9JRjwvc3Bhbj4KPC9kaXY+Cgo8cD5MYSBkZXRlY3Rpb24gKGJsb2NjbyBGKSDDqCBhYmlsaXRhdGEgc29sbyBzZTo8L3A+CjxkaXYgY2xhc3M9ImZvcm11bGEiPgo8ZGl2IGNsYXNzPSJsYmwiPkd1YXJkIGRldGVjdGlvbiB2NC43PC9kaXY+CmJBbmdsZUV4Y2VlZGVkIDxzcGFuIGNsYXNzPSJrdyI+QU5EPC9zcGFuPiAoTk9UIElfRmxhdFdhaXRFbmFibGUgPHNwYW4gY2xhc3M9Imt3Ij5PUjwvc3Bhbj4gYkZsYXRTdXJmYWNlRm91bmQpCjwvZGl2PgoKPGRpdiBjbGFzcz0ib2siPgo8c3Ryb25nPuKckyBJbmRpY2F0b3JpIGRpYWdub3N0aWNpIG5lbCB2aWV3ZXIgUHl0aG9uIHY0LjMuOTM8L3N0cm9uZz4KSWwgcGFubmVsbG8gInY0LjYvdjQuNyIgbmVsIHNpbXVsYXRvcmUgbW9zdHJhOgo8dWwgc3R5bGU9Im1hcmdpbi10b3A6NnB4O21hcmdpbi1sZWZ0OjIwcHgiPgo8bGk+PHN0cm9uZz5CYXNlbGluZSBPSzwvc3Ryb25nPjog4pyTIGRvcG8gY2hlIGJBbmdsZUV4Y2VlZGVkPVRSVUUgKGJhc2VsaW5lIHN0YWJpbGUpPC9saT4KPGxpPjxzdHJvbmc+U3VwLiBwaWF0dGE8L3N0cm9uZz46IOKckyB0cm92YXRhIC8g4pyXIG5vbiB0cm92YXRhIC8g4oCUIGRpc2FiaWxpdGF0YTwvbGk+CjwvdWw+CkFuY2hlIG5lbCB0YWIgQW5hbGlzaSBhcHBhcmUgdW4gdGVzdG8gY29sb3JhdG8gbmVsbCdhbmdvbG8gc2luaXN0cm8gZGVsIGdyYWZpY28gbGFzZXIuCjwvZGl2Pgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0idGhyZXNob2xkcyI+CjxoMj5DYWxjb2xvIHNvZ2xpZSBhZGF0dGl2ZTwvaDI+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5Tb2dsaWUgUE9TSVRJVkUgKGxhc2VyICZndDsgYmFzZWxpbmUpPC9kaXY+ClRoSGkgID0gck0gKyBJX1NpZ21hRmFjdG9yIMOXIHJTICsgSV9NaW5BYnNEZXZpYXRpb24gICAgICAgICDihpIgc29nbGlhIGFwZXJ0dXJhIGNsdXN0ZXIKVGhMbyAgPSByTSArIChJX1NpZ21hRmFjdG9yIC0gSV9IeXN0ZXJlc2lzU2lnbWFzKSDDlyByUyArIElfTWluQWJzRGV2aWF0aW9uIMOXIDAuNSAg4oaSIHNvZ2xpYSBjaGl1c3VyYQo8L2Rpdj4KPGRpdiBjbGFzcz0iZm9ybXVsYSI+CjxkaXYgY2xhc3M9ImxibCI+U29nbGllIE5FR0FUSVZFIChsYXNlciAmbHQ7IGJhc2VsaW5lKTwvZGl2PgpUaEhpTmVnID0gck0gLSBJX1NpZ21hRmFjdG9yIMOXIHJTIC0gSV9NaW5BYnNEZXZpYXRpb24gICAgICAgICDihpIgc29nbGlhIGFwZXJ0dXJhIGNsdXN0ZXIKVGhMb05lZyA9IHJNIC0gKElfU2lnbWFGYWN0b3IgLSBJX0h5c3RlcmVzaXNTaWdtYXMpIMOXIHJTIC0gSV9NaW5BYnNEZXZpYXRpb24gw5cgMC41ICDihpIgc29nbGlhIGNoaXVzdXJhCjwvZGl2PgoKPGgzIGlkPSJwYXJhbS10aHJlc2giPlBhcmFtZXRyaSBzb2dsaWU8L2gzPgo8dGFibGU+Cjx0cj48dGg+UGFyYW1ldHJvIFNDTDwvdGg+PHRoPkRlZmF1bHQ8L3RoPjx0aD5FZmZldHRvPC90aD48L3RyPgo8dHI+PHRkPklfU2lnbWFGYWN0b3I8L3RkPjx0ZD4zLjA8L3RkPjx0ZD5Nb2x0aXBsaWNhdG9yZSBzaWdtYS4gUGnDuSBhbHRvID0gbWVubyBmYWxzaSBwb3NpdGl2aSBtYSBwacO5IGRpZmZpY2lsZSB0cm92YXJlIHNhbGRhdHVyZSBkZWJvbGkuIFJhbmdlIHRpcGljbzogMS41IChzZW5zaWJpbGUpIOKAkyA0LjAgKGNvbnNlcnZhdGl2bykuPC90ZD48L3RyPgo8dHI+PHRkPklfTWluQWJzRGV2aWF0aW9uPC90ZD48dGQ+MS41IG1tPC90ZD48dGQ+U2NhcnRvIG1pbmltbyBhc3NvbHV0byBpbmRpcGVuZGVudGUgZGFsIHJ1bW9yZS4gQW5jaGUgY29uIHNpZ21hPTAgaWwgcGljY28gZGV2ZSBzdXBlcmFyZSBxdWVzdG8gdmFsb3JlLiBDaXJjYSAxLzMgZGVsbCdhbHRlenphIGF0dGVzYSBkZWxsYSBzYWxkYXR1cmEgKG8gcHJvZm9uZGl0w6AgYnVjbykuPC90ZD48L3RyPgo8dHI+PHRkPklfSHlzdGVyZXNpc1NpZ21hczwvdGQ+PHRkPjAuNTwvdGQ+PHRkPkFiYmFzc2EgbGEgc29nbGlhIGRpIGNoaXVzdXJhIGNsdXN0ZXIuIEV2aXRhIGNoaXVzdXJlIHByZW1hdHVyZSBzdSBzZWduYWxpIGNvbiBsZWdnZXJhIG9uZHVsYXppb25lLiBUcm9wcG8gYWx0YSDihpIgY2x1c3RlciBub24gc2kgY2hpdWRvbm8gbWFpLjwvdGQ+PC90cj4KPHRyPjx0ZD5JX0Jhc2VsaW5lV2luZG93RGVnPC90ZD48dGQ+MTAuMMKwPC90ZD48dGQ+QW1waWV6emEgZmluZXN0cmEgbG9vay1iYWNrLiBGaW5lc3RyZSBncmFuZGkgPSBiYXNlbGluZSBwacO5IHN0YWJpbGUgbWEgbWVubyByZWF0dGl2YSBhIHRyZW5kIGxlbnRpLiBQZXIgY29yc2EgbGluZWFyZSAzMG1tOiAxLjDCsCDigJMgMi4wwrAuPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iY2x1c3RlciI+CjxoMj5Mb2dpY2EgY2x1c3RlciBvbmxpbmU8L2gyPgoKPGgzPkFwZXJ0dXJhIGNsdXN0ZXI8L2gzPgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5CbG9jY28gRSDigJQgYXBlcnR1cmE8L2Rpdj4KPHNwYW4gY2xhc3M9Imt3Ij5JRiBOT1Q8L3NwYW4+IGJJbkNsdXN0ZXIgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IGJDaGVja1Bvc2l0aXZlIDxzcGFuIGNsYXNzPSJrdyI+QU5EPC9zcGFuPiBsYXNlciA+PSBUaEhpIDxzcGFuIGNsYXNzPSJrdyI+VEhFTjwvc3Bhbj4KICAgIGJJbkNsdXN0ZXIgOj0gPHNwYW4gY2xhc3M9Imt3Ij5UUlVFPC9zcGFuPjsgcG9sYXJpdHkgOj0gPHNwYW4gY2xhc3M9Im51bSI+MDwvc3Bhbj47IGNvdW50IDo9IDxzcGFuIGNsYXNzPSJudW0iPjE8L3NwYW4+CiAgPHNwYW4gY2xhc3M9Imt3Ij5FTFNJRjwvc3Bhbj4gYkNoZWNrTmVnYXRpdmUgPHNwYW4gY2xhc3M9Imt3Ij5BTkQ8L3NwYW4+IGxhc2VyICZsdDs9IFRoSGlOZWcgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogICAgYkluQ2x1c3RlciA6PSA8c3BhbiBjbGFzcz0ia3ciPlRSVUU8L3NwYW4+OyBwb2xhcml0eSA6PSA8c3BhbiBjbGFzcz0ibnVtIj4xPC9zcGFuPjsgY291bnQgOj0gPHNwYW4gY2xhc3M9Im51bSI+MTwvc3Bhbj4KICA8c3BhbiBjbGFzcz0ia3ciPkVORF9JRjwvc3Bhbj4KPC9kaXY+Cgo8aDM+Q29udGludWF6aW9uZSBlIGNoaXVzdXJhPC9oMz4KPHA+Q2x1c3RlciBwb3NpdGl2bzogc2kgbWFudGllbmUgZmluY2jDqSA8Y29kZT5sYXNlciDiiaUgVGhMbzwvY29kZT4uIENsdXN0ZXIgbmVnYXRpdm86IGZpbmNow6kgPGNvZGU+bGFzZXIg4omkIFRoTG9OZWc8L2NvZGU+LiBBbGxhIGNoaXVzdXJhIHZpZW5lIHZhbHV0YXRhIGxhIHZhbGlkaXTDoC48L3A+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5WYWxpZGl0w6AgY2x1c3RlcjwvZGl2Pgp2YWxpZG8gOj0gKGNvdW50ID49IElfTWluQ29uc2VjdXRpdmUgQU5EIGNvdW50ICZsdDs9IElfTWF4Q29uc2VjdXRpdmUpCjwvZGl2PgoKPGgzIGlkPSJwYXJhbS1jbHVzdGVyIj5QYXJhbWV0cmkgY2x1c3RlcjwvaDM+Cjx0YWJsZT4KPHRyPjx0aD5QYXJhbWV0cm8gU0NMPC90aD48dGg+RGVmYXVsdDwvdGg+PHRoPkVmZmV0dG88L3RoPjwvdHI+Cjx0cj48dGQ+SV9NaW5Db25zZWN1dGl2ZTwvdGQ+PHRkPjM8L3RkPjx0ZD5DYW1waW9uaSBtaW5pbWkgcGVyIGNsdXN0ZXIgdmFsaWRvLiBBdW1lbnRhcmUgcGVyIHJpZHVycmUgZmFsc2kgcG9zaXRpdmkgc3UgcGljY2hpIGlzb2xhdGkuPC90ZD48L3RyPgo8dHI+PHRkPklfTWF4Q29uc2VjdXRpdmU8L3RkPjx0ZD42MDwvdGQ+PHRkPkNhbXBpb25pIG1hc3NpbWkuIENsdXN0ZXIgcGnDuSBsdW5naGkgdmVuZ29ubyBpZ25vcmF0aSAocHJvYmFiaWxtZW50ZSBub24gw6ggdW5hIHNhbGRhdHVyYSkuIEF1bWVudGFyZSBzZSBsYSBzYWxkYXR1cmEgw6ggbGFyZ2EuPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iZGV0ZWN0aW9uIj4KPGgyPkRldGVjdGlvbiDigJQgYmxvY2NvIEYgPHNwYW4gY2xhc3M9ImJhZGdlIG5ldyI+djQuNzwvc3Bhbj48L2gyPgoKPGRpdiBjbGFzcz0iZm9ybXVsYSI+CjxkaXYgY2xhc3M9ImxibCI+Q29uZGl6aW9uZSBkZXRlY3Rpb24gdjQuNyAoU0NMIOKAlCBibG9jY28gRik8L2Rpdj4KPHNwYW4gY2xhc3M9Imt3Ij5JRjwvc3Bhbj4gYkFuZ2xlRXhjZWVkZWQgPHNwYW4gY2xhc3M9Imt3Ij5BTkQ8L3NwYW4+IChOT1QgSV9GbGF0V2FpdEVuYWJsZSA8c3BhbiBjbGFzcz0ia3ciPk9SPC9zcGFuPiBiRmxhdFN1cmZhY2VGb3VuZCkgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IGJJbkNsdXN0ZXIgPHNwYW4gY2xhc3M9Imt3Ij5BTkQ8L3NwYW4+IGlDbHVzdGVyQ291bnQgPj0gSV9NaW5Db25zZWN1dGl2ZSA8c3BhbiBjbGFzcz0ia3ciPkFORCBOT1Q8L3NwYW4+IGJXZWxkRGV0ZWN0ZWRJbnRlcm5hbCA8c3BhbiBjbGFzcz0ia3ciPlRIRU48L3NwYW4+CiAgICBiV2VsZERldGVjdGVkSW50ZXJuYWwgOj0gPHNwYW4gY2xhc3M9Imt3Ij5UUlVFPC9zcGFuPgogICAgaURldGVjdGVkQXRTYW1wbGUgOj0gaUN1cklkeAogICAgckRldGVjdGVkQXRBbmdsZSAgOj0gYXJBbmdsZXNbaUN1cklkeF0KICAgIC4uLgogIDxzcGFuIGNsYXNzPSJrdyI+RU5EX0lGPC9zcGFuPgo8c3BhbiBjbGFzcz0ia3ciPkVORF9JRjwvc3Bhbj4KPC9kaXY+Cgo8cD48Y29kZT5iQW5nbGVFeGNlZWRlZDwvY29kZT4gZGl2ZW50YSBUUlVFIHF1YW5kbyA8Y29kZT5yVG90YWxUcmF2ZWwgPiBJX0Jhc2VsaW5lV2luZG93RGVnPC9jb2RlPiAoZmxhZyBibG9jayBBKS4gR2FyYW50aXNjZSBjaGUgYWxtZW5vIHVuYSBmaW5lc3RyYSBiYXNlbGluZSBzaWEgc3RhdGEgcGVyY29yc2EgcHJpbWEgZGkgcXVhbHNpYXNpIGRldGVjdGlvbi4gw4ggcGVyc2lzdGVudGU6IG5vbiB0b3JuYSBtYWkgRkFMU0UgZG9wbyBlc3NlcnNpIGFsemF0by48L3A+Cgo8ZGl2IGNsYXNzPSJkYW5nZXIiPgo8c3Ryb25nPvCflLQgU2NhbiByZXRyb2F0dGl2byBSSU1PU1NPIGluIHY0Ljc8L3N0cm9uZz4KSWwgYmxvY2NvIEYtYmlzIChzY2FuIHJldHJvYXR0aXZvIHN1aSBjYW1waW9uaSBpbiBkZWFkIHpvbmUpIMOoIHN0YXRvIGVsaW1pbmF0by4gRXJhIGxhIGNhdXNhIHByaW5jaXBhbGUgZGkgZmFsc2kgcG9zaXRpdmkgc3Ugc3VwZXJmaWNpIGNvbiBkcmlmdCBsZW50bzogbGEgYmFzZWxpbmUgdGFyZGEgKGFsdGEpIGNvbmZyb250YXRhIGNvbiBjYW1waW9uaSBpbml6aWFsaSAoYmFzc2kpIGxpIGNsYXNzaWZpY2F2YSBlcnJvbmVhbWVudGUgY29tZSBidWNoZS4gVmVyaWZpY2F0byBhbmFsaXRpY2FtZW50ZSBzdSByaWdhIGlkPTEzMDc6IGRyaWZ0ICsxLjI5bW0gaW4gMTLCsCBwcm9kdWNldmEgZmFsc2EgZGV0ZWN0aW9uIGEgMS4wNcKwLgo8L2Rpdj4KCjxkaXYgY2xhc3M9Im5vdGUiPgo8c3Ryb25nPvCfk4wgckRldGVjdGVkQXRBbmdsZSB2cyBPdXRQb3NpemlvbmVBc3NlPC9zdHJvbmc+ClNvbm8gZHVlIHZhbG9yaSBkaXZlcnNpOiA8Y29kZT5yRGV0ZWN0ZWRBdEFuZ2xlPC9jb2RlPiA9IGFuZ29sbyBhbCBtb21lbnRvIGRlbGxhIGNvbmZlcm1hIChjYW1waW9uZSAjTWluQ29uc2VjIGRlbCBjbHVzdGVyKS4gPGNvZGU+T3V0UG9zaXppb25lQXNzZTwvY29kZT4gPSBhbmdvbG8gZGVsIHBpY2NvIG1hc3NpbW8gKGFnZ2lvcm5hdG8gbGl2ZSBkdXJhbnRlIGlsIGNsdXN0ZXIgZSBpIDLCsCBwb3N0LWRldGVjdGlvbikuCjwvZGl2Pgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0icG9zdGRldCI+CjxoMj5Qb3N0LWRldGVjdGlvbiB0cmF2ZWwgPHNwYW4gY2xhc3M9ImJhZGdlIG5ldyI+djQuNTwvc3Bhbj48L2gyPgoKPHA+Q29uIDxjb2RlPklfU3RvcE9uV2VsZD1UUlVFPC9jb2RlPiwgaWwgRkIgbm9uIHZhIGluIERvbmUgaW1tZWRpYXRhbWVudGUgYWwgcmlsZXZhbWVudG8uIENvbnRpbnVhIGFkIGFjcXVpc2lyZSBjYW1waW9uaSBwZXIgPGNvZGU+clBvc3REZXRUcmF2ZWxEZWdDID0gMi4wwrA8L2NvZGU+IChjb3N0YW50ZSBpbnRlcm5hKSBkb3BvIGxhIGRldGVjdGlvbiwgcG9pIHZhIGluIERvbmUuIER1cmFudGUgcXVlc3RpIDLCsCBpbCBjbHVzdGVyIGUgPGNvZGU+T3V0UG9zaXppb25lQXNzZTwvY29kZT4gY29udGludWFubyBhZCBhZ2dpb3JuYXJzaS48L3A+Cgo8ZGl2IGNsYXNzPSJmb3JtdWxhIj4KPGRpdiBjbGFzcz0ibGJsIj5BY2N1bXVsYXRvcmUgcG9zdC1kZXRlY3Rpb24gKGJsb2NjbyBBLWJpcyk8L2Rpdj4KPHNwYW4gY2xhc3M9Imt3Ij5JRjwvc3Bhbj4gYldlbGREZXRlY3RlZEludGVybmFsIDxzcGFuIGNsYXNzPSJrdyI+QU5EPC9zcGFuPiBJX1N0b3BPbldlbGQgPHNwYW4gY2xhc3M9Imt3Ij5BTkQgTk9UPC9zcGFuPiBJT19SaWNlcmNhU2FsZGF0dXJhLlRyb3ZhdGEgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogIHJUcmF2ZWxBZnRlckRldCArPSBBQlMoYXJBbmdsZXNbaUN1cklkeF0gLSBhckFuZ2xlc1tpQ3VySWR4LTFdKQogIDxzcGFuIGNsYXNzPSJrdyI+SUY8L3NwYW4+IHJUcmF2ZWxBZnRlckRldCA+PSByUG9zdERldFRyYXZlbERlZ0MgPHNwYW4gY2xhc3M9Imt3Ij5USEVOPC9zcGFuPgogICAgSU9fUmljZXJjYVNhbGRhdHVyYS5Ucm92YXRhIDo9IDxzcGFuIGNsYXNzPSJrdyI+VFJVRTwvc3Bhbj47IGlTdGF0ZSA6PSA8c3BhbiBjbGFzcz0ibnVtIj4yPC9zcGFuPjsgPHNwYW4gY2xhc3M9Imt3Ij5SRVRVUk48L3NwYW4+CiAgPHNwYW4gY2xhc3M9Imt3Ij5FTkRfSUY8L3NwYW4+CjxzcGFuIGNsYXNzPSJrdyI+RU5EX0lGPC9zcGFuPgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9InBvbGFyaXR5Ij4KPGgyPlBvbGFyaXTDoCBwaWNjbyA8c3BhbiBjbGFzcz0iYmFkZ2UgbmV3Ij52NC4zPC9zcGFuPjwvaDI+Cjx0YWJsZT4KPHRyPjx0aD5JX1BlYWtQb2xhcml0eTwvdGg+PHRoPkVmZmV0dG88L3RoPjx0aD5RdWFuZG8gdXNhcmU8L3RoPjwvdHI+Cjx0cj48dGQ+MCAoZGVmYXVsdCk8L3RkPjx0ZD5Tb2xvIHBpY2NoaSBQT1NJVElWSSAobGFzZXIgJmd0OyBiYXNlbGluZSk8L3RkPjx0ZD5TYWxkYXR1cmEgY3JlYSByaWxpZXZvIOKGkiBpbCBsYXNlciBtaXN1cmEgZGlzdGFuemEgbWlub3JlICh2YWxvcmUgcGnDuSBhbHRvKTwvdGQ+PC90cj4KPHRyPjx0ZD4xPC90ZD48dGQ+U29sbyBwaWNjaGkgTkVHQVRJVkkgKGxhc2VyICZsdDsgYmFzZWxpbmUpPC90ZD48dGQ+QnVjaGkgbyBkZXByZXNzaW9uaTwvdGQ+PC90cj4KPHRyPjx0ZD4yPC90ZD48dGQ+RU5UUkFNQkkg4oCUIHZpbmNlIHF1ZWxsbyBjb24gfGRldmlhemlvbmV8IG1hZ2dpb3JlPC90ZD48dGQ+VGlwbyBkaSBzYWxkYXR1cmEgbm9uIG5vdG8gYSBwcmlvcmk8L3RkPjwvdHI+CjwvdGFibGU+CjxkaXYgY2xhc3M9ImRhbmdlciI+CjxzdHJvbmc+8J+UtCBFcnJvckNvZGUgNDwvc3Ryb25nPgpTZSA8Y29kZT5JX1BlYWtQb2xhcml0eTwvY29kZT4gbm9uIMOoIDAsIDEgbyAyIGlsIEZCIHZhIGluIGVycm9yZSAoRXJyb3JDb2RlPTQpLgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9InRhYnMiPgo8aDI+U3RydXR0dXJhIHRhYiBzb2Z0d2FyZSBQeXRob24gdjQuMy45MzwvaDI+Cjx0YWJsZT4KPHRyPjx0aD5UYWI8L3RoPjx0aD5GdW56aW9uZSBwcmluY2lwYWxlPC90aD48L3RyPgo8dHI+PHRkPvCfk4ogQW5hbGlzaTwvdGQ+PHRkPkNhcmljYSBmaWxlIC5kYiBvIHJpZ2EgU1FMaXRlLCB2aXN1YWxpenphIHNlZ25hbGUrc29nbGllLCBpbmRpY2F0b3JpIHN1cC5waWF0dGEgdjQuNywgcmVwb3J0IGRpYWdub3N0aWNvLiBJbCBzaW11bGF0b3JlIHBhcnRlIGF1dG9tYXRpY2FtZW50ZSBhbCBjYXJpY2FtZW50by48L3RkPjwvdHI+Cjx0cj48dGQ+4pa2IFNpbXVsYXRvcmUgUExDPC90ZD48dGQ+U2ltdWxhemlvbmUgcGFyYW1ldHJpY2EgKyBncmFmaWNpIGRldHRhZ2xpYXRpICsgYW5hbGlzaSBkaXNjb3JkYW56ZS4gUGFubmVsbG8gInY0LjYvdjQuNyIgY29uIGluZGljYXRvcmkgQmFzZWxpbmUgT0sgZSBTdXAucGlhdHRhLjwvdGQ+PC90cj4KPHRyPjx0ZD7wn5SMIFBMQyBSZWFkZXI8L3RkPjx0ZD5MZXR0dXJhIGRpcmV0dGEgREIgUExDIHZpYSBTbmFwNywgbW9uaXRvciByZWFsLXRpbWUsIEF1dG8gRXhwb3J0IG11bHRpLURCIGNvbiBzZWxlemlvbmUgcmVhbC10aW1lLjwvdGQ+PC90cj4KPHRyPjx0ZD7wn4yQIE9QQyBVQTwvdGQ+PHRkPkxldHR1cmEgdHJhbWl0ZSBwcm90b2NvbGxvIE9QQyBVQS48L3RkPjwvdHI+Cjx0cj48dGQ+8J+TiCBTdGF0aXN0aWNoZTwvdGQ+PHRkPkdyaWQgc2VhcmNoIHBhcmFsbGVsbyBtdWx0aS1maWxlLCBvdHRpbWl6emF6aW9uZSBwYXJhbWV0cmksIGFuYWxpc2kgZGlzY29yZGFuemUgYmF0Y2guIFByb2dyZXNzIGxhYmVsIG1vc3RyYSBsYSBjb21ibyBjb3JyZW50ZSBpbiBlbGFib3JhemlvbmUuPC90ZD48L3RyPgo8dHI+PHRkPvCfk6UgU1FMaXRlIEltcG9ydDwvdGQ+PHRkPkltcG9ydCBiYXRjaCBkaSBmaWxlIC5kYiBpbiBhcmNoaXZpbyBTUUxpdGUuPC90ZD48L3RyPgo8dHI+PHRkPvCfk4sgU1FMaXRlIFF1ZXJ5PC90ZD48dGQ+UXVlcnkgdmlzdWFsZSBvIFNRTCBsaWJlcm8gc3VsbCdhcmNoaXZpbywgZWxpbWluYXppb25lIHJpZ2hlLjwvdGQ+PC90cj4KPC90YWJsZT4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9InNpbSI+CjxoMj5UYWIgU2ltdWxhdG9yZSDigJQgZGV0dGFnbGlvPC9oMj4KCjxoMz5Db2xvbm5lIHBhcmFtZXRyaSAodjQuMy45MSspPC9oMz4KPHA+TGUgc2V6aW9uaSBTb2dsaWUgZSBDbHVzdGVyIG1vc3RyYW5vIGZpbm8gYSAzIGNvbG9ubmUgcGVyIG9nbmkgcGFyYW1ldHJvOjwvcD4KPHRhYmxlPgo8dHI+PHRoPkNvbG9ubmE8L3RoPjx0aD5TZm9uZG88L3RoPjx0aD5Db250ZW51dG88L3RoPjwvdHI+Cjx0cj48dGQ+REI8L3RkPjx0ZD4jMGQxMTE3IChuZXJvIHNjdXJvKTwvdGQ+PHRkPlZhbG9yZSBvcmlnaW5hbGUgY2FyaWNhdG8gZGFsIGZpbGUgLmRiIChzb2xhIGxldHR1cmEpPC90ZD48L3RyPgo8dHI+PHRkPlN0YXRzPC90ZD48dGQ+IzBkMWYzOCAoYmx1IG5hdnkpPC90ZD48dGQ+VmFsb3JlIG90dGltaXp6YXRvIGRhbCBncmlkIHNlYXJjaCDigJQgYXBwYXJlIHNvbG8gZG9wbyAiQXBwbGljYSBhbCBTaW11bGF0b3JlIjwvdGQ+PC90cj4KPHRyPjx0ZD5Db3JyZW50ZTwvdGQ+PHRkPkVOVFJZX0JHPC90ZD48dGQ+VmFsb3JlIGF0dHVhbG1lbnRlIHVzYXRvIGRhbGxhIHNpbXVsYXppb25lIChlZGl0YWJpbGUpPC90ZD48L3RyPgo8L3RhYmxlPgoKPGgzPk1vZGFsaXTDoCBvcGVyYXRpdmU8L2gzPgo8dGFibGU+Cjx0cj48dGg+TW9kYWxpdMOgPC90aD48dGg+Q29uZGl6aW9uZTwvdGg+PHRoPkNvbXBvcnRhbWVudG88L3RoPjwvdHI+Cjx0cj48dGQ+VkVSSUZJQ0EgUExDPC90ZD48dGQ+UGFyYW1ldHJpIHNvZ2xpYSA9IERCIG9yaWdpbmFsZSBFIGFyVGhyZXNoSGlnaCBub24gbnVsbG88L3RkPjx0ZD5Vc2Egc29nbGllIHByZWNhbGNvbGF0ZSBkYWwgUExDIOKAlCByZXBsaWNhIGVzYXR0YTwvdGQ+PC90cj4KPHRyPjx0ZD5BTkFMSVNJPC90ZD48dGQ+UGFyYW1ldHJpIG1vZGlmaWNhdGkgbyBzb2dsaWUgbm9uIGRpc3BvbmliaWxpPC90ZD48dGQ+UmljYWxjb2xhIHNvZ2xpZSBpbiBQeXRob24gZmxvYXQzMiDigJQgc2ltdWxhIGlsIGNvbXBvcnRhbWVudG8gUExDPC90ZD48L3RyPgo8L3RhYmxlPgoKPGgzPkdyYWZpY2kgZGlzcG9uaWJpbGk8L2gzPgo8dGFibGU+Cjx0cj48dGg+R3JhZmljbzwvdGg+PHRoPkNvbnRlbnV0bzwvdGg+PC90cj4KPHRyPjx0ZD5TZWduYWxlICZhbXA7IERldGVjdGlvbjwvdGQ+PHRkPkxhc2VyIGZpbHRyYXRvICsgc29nbGllIFRoSGkvVGhMbyAoZ2lhbGxvPXBvc2l0aXZvLCBhenp1cnJvPW5lZ2F0aXZvKSArIG1hcmtlciBkZXRlY3Rpb24gKyBpbmRpY2F0b3JlIHN1cC5waWF0dGEuIFNvdHRvOiBkZWx0YSBkYWxsYSBiYXNlbGluZS48L3RkPjwvdHI+Cjx0cj48dGQ+RWZmZXR0byBGaWx0cm88L3RkPjx0ZD5Db25mcm9udG8gZ3JlenpvIHZzIGZpbHRyYXRvICsgZGVuc2l0w6AgY2FtcGlvbmkgcGVyIGdyYWRvLjwvdGQ+PC90cj4KPHRyPjx0ZD5WaXN0YSBQb2xhcmU8L3RkPjx0ZD5TZWduYWxlIHN1IGdyYWZpY28gcG9sYXJlIOKAlCB1dGlsZSBwZXIgdmVkZXJlIGxhIHNhbGRhdHVyYSByaXNwZXR0byBhbGxhIHJvdGF6aW9uZS48L3RkPjwvdHI+Cjx0cj48dGQ+U29nbGllIHBlciBjYW1waW9uZTwvdGQ+PHRkPkV2b2x1emlvbmUgZGkgTWVhbiBlIFNpZ21hICsgVGhyZXNoSGlnaC9Mb3cgY2FtcGlvbmUgcGVyIGNhbXBpb25lLiBNb3N0cmEgc29nbGllIHBlciBsYSBwb2xhcml0w6AgY29uZmlndXJhdGEuPC90ZD48L3RyPgo8dHI+PHRkPlRpbWVsaW5lIERldGVjdGlvbjwvdGQ+PHRkPkRldHRhZ2xpbyBjYW1waW9uZS1wZXItY2FtcGlvbmUgZmlubyBhbCByaWxldmFtZW50bzogbGFzZXIsIM6UIHZzIHNvZ2xpYSwgY29udGF0b3JlIGNsdXN0ZXIuPC90ZD48L3RyPgo8dHI+PHRkPkRCIHZzIFNpbTwvdGQ+PHRkPkNvbmZyb250byBNZWFuIFBMQyB2cyBTaW0sIFRocmVzaEhpZ2ggUExDIHZzIFNpbSwgcmVzaWR1by48L3RkPjwvdHI+Cjx0cj48dGQ+Q29wZXJ0dXJhIEZpbmVzdHJhPC90ZD48dGQ+d2luX24gKGNhbXBpb25pIG5lbGxhIGZpbmVzdHJhIGxvb2stYmFjaykgcGVyIGNhbXBpb25lIOKAlCBldmlkZW56aWEgem9uZSBjb24gYmFzZWxpbmUgaW5zdGFiaWxlLjwvdGQ+PC90cj4KPHRyPjx0ZD5DYW1waW9uaSBSaWZpdXRhdGk8L3RkPjx0ZD5NYXBwYSBkZWkgY2FtcGlvbmkgc2NhcnRhdGkgZGFsIGZpbHRybyArIGRpc3RyaWJ1emlvbmUgcGVyIG1vdGl2by48L3RkPjwvdHI+Cjx0cj48dGQ+U05SIFJvbGxpbmc8L3RkPjx0ZD5TaWdtYSBsb2NhbGUgZSBTTlIgPSB8ZGV2fC/PgyBjYW1waW9uZSBwZXIgY2FtcGlvbmUuPC90ZD48L3RyPgo8dHI+PHRkPkRldHRhZ2xpbyBDbHVzdGVyPC90ZD48dGQ+VGFiZWxsYSB0ZXN0dWFsZSBkaSB0dXR0aSBpIGNsdXN0ZXIgdHJvdmF0aSBjb24gc3RhdGlzdGljaGUuPC90ZD48L3RyPgo8dHI+PHRkPlJlcG9ydCBEaWFnbm9zdGljbzwvdGQ+PHRkPlZlcmRldHRvLCBwYXJhbWV0cmksIGRpYWdub3N0aWNhIGJhc2VsaW5lLCBzdWdnZXJpbWVudGkuPC90ZD48L3RyPgo8dHI+PHRkPuKaoSBEaXNjb3JkYW56ZTwvdGQ+PHRkPlRhYmVsbGEgZGlzY29yZGFuemUgUExDIHZzIFNpbSBkb3BvIGdyaWQgc2VhcmNoLiBUYXN0byBkZXN0cm8g4oaSIGNhcmljYSBuZWwgc2ltdWxhdG9yZS48L3RkPjwvdHI+CjwvdGFibGU+Cgo8aDM+UGFyYW1ldHJpIHY0LjYvdjQuNyBuZWwgc2ltdWxhdG9yZTwvaDM+CjxwPlNlemlvbmUgInY0LjY6IEJ1Y2hpIGFuZWxsbyI6PC9wPgo8dGFibGU+Cjx0cj48dGg+Q29udHJvbGxvPC90aD48dGg+RnVuemlvbmU8L3RoPjwvdHI+Cjx0cj48dGQ+Q2hlY2tib3ggIkJhc2VsaW5lIGFkYXR0aXZhIjwvdGQ+PHRkPkFiaWxpdGEgSV9BZGFwdEJhc2VsaW5lRW5hYmxlIG5lbGxhIHNpbXVsYXppb25lPC90ZD48L3RyPgo8dHI+PHRkPk9mZnNldCBbbW1dPC90ZD48dGQ+SV9BZGFwdEJhc2VsaW5lT2Zmc2V0PC90ZD48L3RyPgo8dHI+PHRkPkNoZWNrYm94ICJBdHRlc2Egc3VwLiBwaWF0dGEiPC90ZD48dGQ+QWJpbGl0YSBJX0ZsYXRXYWl0RW5hYmxlPC90ZD48L3RyPgo8dHI+PHRkPkNhbXBpb25pPC90ZD48dGQ+SV9GbGF0V2FpdFNhbXBsZXM8L3RkPjwvdHI+Cjx0cj48dGQ+VG9sbC4gW21tXTwvdGQ+PHRkPklfRmxhdFdhaXRUb2xsPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0ic3RhdHMiPgo8aDI+VGFiIFN0YXRpc3RpY2hlIOKAlCBHcmlkIFNlYXJjaDwvaDI+Cgo8cD5JbCBncmlkIHNlYXJjaCBlc2VndWUgbGEgc2ltdWxhemlvbmUgc3UgTiBmaWxlIMOXIE0gY29tYmluYXppb25pIGRpIHBhcmFtZXRyaSBpbiBwYXJhbGxlbG8gdXNhbmRvIDxjb2RlPlByb2Nlc3NQb29sRXhlY3V0b3I8L2NvZGU+LjwvcD4KCjxoMz5BcmNoaXRldHR1cmEgcGFyYWxsZWxhPC9oMz4KPGRpdiBjbGFzcz0iZm9ybXVsYSI+CjxkaXYgY2xhc3M9ImxibCI+UGlwZWxpbmU8L2Rpdj4KRmlsZSBwcmVjYXJpY2F0aSBuZWxsJ2luaXRpYWxpemVyIHdvcmtlciAoemVybyBJL08gcGVyIHRhc2spCiAg4oaTCkZlZWRlciB0aHJlYWQg4oaSIHNsaWRpbmcgd2luZG93IE5fd29ya2Vyw5cxNiB0YXNrIGluIHZvbG8KICDihpMKV29ya2VyOiAoZmksIGNvbWJvX3R1cGxlKSDihpIgc2ltdWxhdGVfcGxjX3JlYWx0aW1lIOKGkiAoZm91bmQsIHNuciwgcGVha19kZXYsIGRldF9hbmdsZSkKICDihpMKUXVldWUg4oaSIFVJIHRocmVhZCDihpIgYWdnaW9ybmFtZW50byBwcm9ncmVzc2l2byBvZ25pIDE1MG1zCjxzcGFuIGNsYXNzPSJjbXQiPi8vIFByb2dyZXNzIGxhYmVsIG1vc3RyYSBwYXJhbWV0cmkgZGVsbGEgY29tYm8gY29ycmVudGU8L3NwYW4+CjwvZGl2PgoKPGgzPlBhcmFtZXRyaSB2NC42IG5lbCBncmlkIHNlYXJjaDwvaDM+CjxwPlR1dHRpIGkgcGFyYW1ldHJpIHY0LjYgKDxjb2RlPmFkYXB0X2Jhc2VsaW5lX2VuYWJsZS9vZmZzZXQ8L2NvZGU+LCA8Y29kZT5mbGF0X3dhaXRfZW5hYmxlL3NhbXBsZXMvdG9sbDwvY29kZT4pIHZlbmdvbm8gcGFzc2F0aSBjb21lIGZpc3NpIGFsIHdvcmtlciDigJQgbm9uIHNvbm8gbWFpIG9nZ2V0dG8gZGkgc3dlZXAgKG5vbiBoYSBzZW5zbyBvdHRpbWl6emFybGkgY29uIGdyaWQgc2VhcmNoIGxpbmVhcmUpLiBWZW5nb25vIGxldHRpIGRhaSBjYW1waSBVSSBhbCBtb21lbnRvIGRlbGwnYXZ2aW8uPC9wPgoKPGRpdiBjbGFzcz0id2FybiI+CjxzdHJvbmc+4pqgIENhbXBpb25lIGNvcnJldHRvIHBlciBpbCBncmlkIHNlYXJjaDwvc3Ryb25nPgpFc2VndWlyZSBpbCBncmlkIHNlYXJjaCBTT0xPIHN1IGZpbGUgZG92ZSBpbCBQTEMgaGEgdHJvdmF0byBsYSBzYWxkYXR1cmEgKDxjb2RlPndlbGRfZm91bmQ9MTwvY29kZT4pLiBVc2FyZSBmaWxlIGRpIHNjYXJ0byBwcm9kdWNlIHBhcmFtZXRyaSBvdHRpbWl6emF0aSBzdWwgcnVtb3JlLCBub24gc3VsbGEgc2FsZGF0dXJhIHJlYWxlLgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9ImF1dG9leHAiPgo8aDI+QXV0byBFeHBvcnQgbXVsdGktREI8L2gyPgoKPHA+TW9uaXRvcmEgZmlubyBhIDEwIERCIFBMQyBzaW11bHRhbmVhbWVudGUgdHJhbWl0ZSB1bid1bmljYSBjb25uZXNzaW9uZSBTbmFwNy4gSWwgcG9sbGluZyDDqCBzZXF1ZW56aWFsZSBzdSB0dXR0aSBpIERCIGFiaWxpdGF0aSBvZ25pIE4gbXMgKGRlZmF1bHQgMTAwbXMpLjwvcD4KCjxoMz5TZWxlemlvbmUgREIgcmVhbC10aW1lICh2NC4zLjg4Kyk8L2gzPgo8cD5MYSBzZWxlemlvbmUgZGVpIERCIHZpZW5lIHJpbGV0dGEgYWQgb2duaSBjaWNsbyBkaSBwb2xsaW5nOjwvcD4KPHVsIHN0eWxlPSJtYXJnaW46IDhweCAwIDEycHggMjRweDsgbGluZS1oZWlnaHQ6IDIuMiI+CiAgPGxpPjxzdHJvbmc+RGlzYWJpbGl0YSBjaGVja2JveCBhIHJ1bnRpbWU8L3N0cm9uZz46IGlsIERCIHZpZW5lIHNhbHRhdG8gaW1tZWRpYXRhbWVudGUgYWwgY2ljbG8gc3VjY2Vzc2l2by4gTmVsIGxvZyBhcHBhcmUgPGNvZGU+4o+4IERCMjh4eHggc29zcGVzbzwvY29kZT4uPC9saT4KICA8bGk+PHN0cm9uZz5BYmlsaXRhIG51b3ZvIERCIGEgcnVudGltZTwvc3Ryb25nPjogdmllbmUgYWdnaXVudG8gYWwgdm9sbyBjb24gdGVzdC1yZWFkIGUgaW5pemlhbGl6emF6aW9uZSB0cmlnZ2VyLiBOZWwgbG9nIGFwcGFyZSA8Y29kZT4rIERCMjh4eHggYWdnaXVudG88L2NvZGU+IGUgPGNvZGU+4pa2IERCMjh4eHggcmlhdHRpdmF0bzwvY29kZT4uPC9saT4KPC91bD4KCjxoMz5UcmlnZ2VyIGRpc3BvbmliaWxpPC9oMz4KPHRhYmxlPgo8dHI+PHRoPlRyaWdnZXI8L3RoPjx0aD5Db25kaXppb25lPC90aD48dGg+VXNvIHRpcGljbzwvdGg+PC90cj4KPHRyPjx0ZD5iU3RhcnRfUHJldiDihpM8L3RkPjx0ZD5Gcm9udGUgZGkgZGlzY2VzYTwvdGQ+PHRkPkZpbmUgYWNxdWlzaXppb25lIChiU3RhcnRBY3F1aXNpdGlvbiB0b3JuYSBGQUxTRSk8L3RkPjwvdHI+Cjx0cj48dGQ+T19Eb25lIOKGkTwvdGQ+PHRkPkZyb250ZSBkaSBzYWxpdGE8L3RkPjx0ZD5JbXB1bHNvIERvbmUgZGVsIEZCPC90ZD48L3RyPgo8dHI+PHRkPmlTdGF0ZSDihpIgRG9uZTwvdGQ+PHRkPmlTdGF0ZSA9PSAyPC90ZD48dGQ+U3RhdG8gbWFjY2hpbmEgPSAyPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0ic3FsaXRlIj4KPGgyPlNjaGVtYSBTUUxpdGU8L2gyPgo8cD5UYWJlbGxhIDxjb2RlPmFjcXVpc2l0aW9uczwvY29kZT4g4oCUIDIxIGNvbG9ubmU6PC9wPgo8cHJlPgo8c3BhbiBjbGFzcz0ia3ciPmlkPC9zcGFuPiAgICAgICAgICAgIElOVEVHRVIgUFJJTUFSWSBLRVkgQVVUT0lOQ1JFTUVOVAo8c3BhbiBjbGFzcz0idmFyIj50aW1lc3RhbXA8L3NwYW4+ICAgICBURVhUICAgICAiMjAyNi0wMy0yNiAxNDoxNTozMiIKPHNwYW4gY2xhc3M9InZhciI+ZGJfbnVtYmVyPC9zcGFuPiAgICAgSU5URUdFUiAgbnVtZXJvIERCIFBMQwo8c3BhbiBjbGFzcz0idmFyIj5maWxlbmFtZTwvc3Bhbj4gICAgICBURVhUICAgICBub21lIGZpbGUgLmRiIG9yaWdpbmFsZQo8c3BhbiBjbGFzcz0idmFyIj53ZWxkX2ZvdW5kPC9zcGFuPiAgICBJTlRFR0VSICAwLzEKPHNwYW4gY2xhc3M9InZhciI+ZGV0X2FuZ2xlPC9zcGFuPiAgICAgUkVBTCAgICAgckRldGVjdGVkQXRBbmdsZSBbwrBdCjxzcGFuIGNsYXNzPSJ2YXIiPmRldF9zYW1wbGU8L3NwYW4+ICAgIElOVEVHRVIgIGlEZXRlY3RlZEF0U2FtcGxlCjxzcGFuIGNsYXNzPSJ2YXIiPnBlYWtfdmFsdWU8L3NwYW4+ICAgIFJFQUwgICAgIHJQZWFrVmFsdWUgW21tXQo8c3BhbiBjbGFzcz0idmFyIj5wZWFrX2Rldjwvc3Bhbj4gICAgICBSRUFMICAgICByUGVha0RldmlhdGlvbiBbbW1dCjxzcGFuIGNsYXNzPSJ2YXIiPnBlYWtfc2lnbWFzPC9zcGFuPiAgIFJFQUwgICAgIHJQZWFrU2lnbWFzIChTTlIpCjxzcGFuIGNsYXNzPSJ2YXIiPm5fc2FtcGxlczwvc3Bhbj4gICAgIElOVEVHRVIgIGlTYW1wbGVzQWNxdWlyZWQKPHNwYW4gY2xhc3M9InZhciI+c2lnbWFfZmFjdG9yPC9zcGFuPiAgUkVBTCAgICAgSV9TaWdtYUZhY3Rvcgo8c3BhbiBjbGFzcz0idmFyIj5taW5fYWJzX2Rldjwvc3Bhbj4gICBSRUFMICAgICBJX01pbkFic0RldmlhdGlvbgo8c3BhbiBjbGFzcz0idmFyIj5oeXN0X3NpZ21hczwvc3Bhbj4gICBSRUFMICAgICBJX0h5c3RlcmVzaXNTaWdtYXMKPHNwYW4gY2xhc3M9InZhciI+d2luZG93X2RlZzwvc3Bhbj4gICAgUkVBTCAgICAgSV9CYXNlbGluZVdpbmRvd0RlZwo8c3BhbiBjbGFzcz0idmFyIj5taW5fY29uc2VjPC9zcGFuPiAgICBJTlRFR0VSICBJX01pbkNvbnNlY3V0aXZlCjxzcGFuIGNsYXNzPSJ2YXIiPm1heF9jb25zZWM8L3NwYW4+ICAgIElOVEVHRVIgIElfTWF4Q29uc2VjdXRpdmUKPHNwYW4gY2xhc3M9InZhciI+cG9sYXJpdHk8L3NwYW4+ICAgICAgSU5URUdFUiAgSV9QZWFrUG9sYXJpdHkKPHNwYW4gY2xhc3M9InZhciI+cmF3X3NhbXBsZXM8L3NwYW4+ICAgQkxPQiAgICAgYXJTYW1wbGVzW10gZmxvYXQzMiBsaXR0bGUtZW5kaWFuCjxzcGFuIGNsYXNzPSJ2YXIiPnJhd19hbmdsZXM8L3NwYW4+ICAgIEJMT0IgICAgIGFyQW5nbGVzW10gIGZsb2F0MzIgbGl0dGxlLWVuZGlhbgo8c3BhbiBjbGFzcz0idmFyIj5zY2FsYXJzX2pzb248L3NwYW4+ICBURVhUICAgICBKU09OIGNvbiB0dXR0aSBnbGkgc2NhbGFyaSBkZWwgREIgKGluY2x1c2kgdjQuNikKPC9wcmU+Cgo8ZGl2IGNsYXNzPSJub3RlIj4KPHN0cm9uZz7wn5OMIHNjYWxhcnNfanNvbjwvc3Ryb25nPgpJbCBjYW1wbyBKU09OIGNvbnRpZW5lIHR1dHRpIGdsaSBzY2FsYXJpIGxldHRpIGRhbCBEQiBhbCBtb21lbnRvIGRlbGwnYWNxdWlzaXppb25lLCBpbmNsdXNpIGkgbnVvdmkgY2FtcGkgdjQuNiAoPGNvZGU+YkFkYXB0QmFzZWxpbmVTZXQ8L2NvZGU+LCA8Y29kZT5iRmxhdFN1cmZhY2VGb3VuZDwvY29kZT4sIDxjb2RlPmlGbGF0Q29uc2VjQ291bnQ8L2NvZGU+LCA8Y29kZT5yQWRhcHRCYXNlbGluZU1pbi9NYXg8L2NvZGU+LCA8Y29kZT5JX0FkYXB0QmFzZWxpbmVFbmFibGU8L2NvZGU+LCA8Y29kZT5JX0ZsYXRXYWl0RW5hYmxlPC9jb2RlPiwgZWNjLikuIFF1ZXN0byBwZXJtZXR0ZSBkaSB2ZXJpZmljYXJlIGxvIHN0YXRvIGNvbXBsZXRvIGRlbGwnRkIgYWwgbW9tZW50byBkZWxsYSBkZXRlY3Rpb24gYW5jaGUgYW5uaSBkb3BvLgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgoKPHNlY3Rpb24gaWQ9ImVycm9ycyI+CjxoMj5Db2RpY2kgZXJyb3JlIFBMQzwvaDI+Cjx0YWJsZT4KPHRyPjx0aD5FcnJvckNvZGU8L3RoPjx0aD5DYXVzYTwvdGg+PHRoPlJpbWVkaW88L3RoPjwvdHI+Cjx0cj48dGQ+MDwvdGQ+PHRkPk9LIOKAlCBuZXNzdW4gZXJyb3JlPC90ZD48dGQ+4oCUPC90ZD48L3RyPgo8dHI+PHRkPjE8L3RkPjx0ZD5CdWZmZXIgb3ZlcmZsb3c6IHBpw7kgZGkgMjAwMCBjYW1waW9uaSBhY3F1aXNpdGk8L3RkPjx0ZD5BdW1lbnRhcmUgSV9NaW5BbmdsZURlbHRhIG8gSV9NaW5MYXNlckRlbHRhIHBlciByaWR1cnJlIGxhIGRlbnNpdMOgPC90ZD48L3RyPgo8dHI+PHRkPjI8L3RkPjx0ZD5OZXNzdW4gY2FtcGlvbmUgYWNxdWlzaXRvIGEgZmluZSBnaXJvPC90ZD48dGQ+VmVyaWZpY2FyZSBpbCBmaWx0cm8g4oCUIElfTWluQW5nbGVEZWx0YSBvIElfTWluTGFzZXJEZWx0YSB0cm9wcG8gcmVzdHJpdHRpdmk8L3RkPjwvdHI+Cjx0cj48dGQ+MzwvdGQ+PHRkPklfTWluQ29uc2VjdXRpdmUgJmd0OyBJX01heENvbnNlY3V0aXZlPC90ZD48dGQ+Q29ycmVnZ2VyZSBpIHBhcmFtZXRyaTwvdGQ+PC90cj4KPHRyPjx0ZD40PC90ZD48dGQ+SV9QZWFrUG9sYXJpdHkgbm9uIMOoIDAsIDEgbyAyPC90ZD48dGQ+SW1wb3N0YXJlIHVuIHZhbG9yZSB2YWxpZG88L3RkPjwvdHI+CjwvdGFibGU+Cjwvc2VjdGlvbj4KCjxocj4KCjxzZWN0aW9uIGlkPSJ0dW5pbmciPgo8aDI+R3VpZGEgYWwgdHVuaW5nIHBhcmFtZXRyaTwvaDI+Cgo8aDM+UGVyIGNvcnNhIHJvdGFudGUgMzYwwrA8L2gzPgo8dGFibGU+Cjx0cj48dGg+UGFyYW1ldHJvPC90aD48dGg+VmFsb3JlIGNvbnNpZ2xpYXRvPC90aD48dGg+Tm90ZTwvdGg+PC90cj4KPHRyPjx0ZD5JX01pbkFuZ2xlRGVsdGE8L3RkPjx0ZD4wLjPCsCDigJMgMC41wrA8L3RkPjx0ZD5+MSBjYW1waW9uZSBvZ25pIDAuNcKwIOKGkiB+NzIwIGNhbXBpb25pL2dpcm88L3RkPjwvdHI+Cjx0cj48dGQ+SV9NaW5MYXNlckRlbHRhPC90ZD48dGQ+MC4wNSDigJMgMC4xIG1tPC90ZD48dGQ+UmlkdXJyZSBzdSBzdXBlcmZpY2kgbGlzY2U8L3RkPjwvdHI+Cjx0cj48dGQ+SV9CYXNlbGluZVdpbmRvd0RlZzwvdGQ+PHRkPjXCsCDigJMgMTXCsDwvdGQ+PHRkPkNpcmNhIDEwLTMwIGNhbXBpb25pIG5lbGxhIGZpbmVzdHJhPC90ZD48L3RyPgo8dHI+PHRkPklfU2lnbWFGYWN0b3I8L3RkPjx0ZD4yLjUg4oCTIDMuNTwvdGQ+PHRkPjMuMCDDqCBpbCBwdW50byBkaSBwYXJ0ZW56YSBzdGFuZGFyZDwvdGQ+PC90cj4KPHRyPjx0ZD5JX01pbkFic0RldmlhdGlvbjwvdGQ+PHRkPjAuMyDigJMgMi4wIG1tPC90ZD48dGQ+fjEvMyBhbHRlenphIHNhbGRhdHVyYSBhdHRlc2E8L3RkPjwvdHI+Cjx0cj48dGQ+SV9NaW5Db25zZWN1dGl2ZTwvdGQ+PHRkPjMg4oCTIDY8L3RkPjx0ZD5TYWxkYXR1cmUgc3RyZXR0ZTogMy4gTGFyZ2hlOiA1LTg8L3RkPjwvdHI+CjwvdGFibGU+Cgo8aDM+UGVyIGNvcnNhIGxpbmVhcmUgYnJldmUgKH4zMG1tLCAxNS0yMCBjYW1waW9uaSk8L2gzPgo8dGFibGU+Cjx0cj48dGg+UGFyYW1ldHJvPC90aD48dGg+VmFsb3JlIGNvbnNpZ2xpYXRvPC90aD48L3RyPgo8dHI+PHRkPklfTWluQW5nbGVEZWx0YTwvdGQ+PHRkPjAuMDAxwrA8L3RkPjwvdHI+Cjx0cj48dGQ+SV9NaW5MYXNlckRlbHRhPC90ZD48dGQ+MC4wMDEgbW08L3RkPjwvdHI+Cjx0cj48dGQ+SV9CYXNlbGluZVdpbmRvd0RlZzwvdGQ+PHRkPjEuMMKwIOKAkyAyLjDCsDwvdGQ+PC90cj4KPHRyPjx0ZD5JX01pbkNvbnNlY3V0aXZlPC90ZD48dGQ+MyDigJMgNDwvdGQ+PC90cj4KPC90YWJsZT4KCjxoMz5JbnRlcnByZXRhcmUgclBlYWtTaWdtYXMgKFNOUik8L2gzPgo8dGFibGU+Cjx0cj48dGg+VmFsb3JlPC90aD48dGg+UXVhbGl0w6A8L3RoPjwvdHI+Cjx0cj48dGQ+Jmx0OyAzz4M8L3RkPjx0ZD7imqAgRGVib2xlIOKAlCByaXZlZGVyZSBwYXJhbWV0cmkgbyBjb25kaXppb25pIGFjcXVpc2l6aW9uZTwvdGQ+PC90cj4KPHRyPjx0ZD4zz4Mg4oCTIDXPgzwvdGQ+PHRkPuKckyBBY2NldHRhYmlsZTwvdGQ+PC90cj4KPHRyPjx0ZD4mZ3Q7IDXPgzwvdGQ+PHRkPuKck+KckyBSb2J1c3RvPC90ZD48L3RyPgo8dHI+PHRkPiZndDsgMTDPgzwvdGQ+PHRkPuKck+Kck+KckyBFY2NlbGxlbnRlPC90ZD48L3RyPgo8L3RhYmxlPgo8L3NlY3Rpb24+Cgo8aHI+Cgo8c2VjdGlvbiBpZD0iZmxvYXQzMiI+CjxoMj5Ob3RlIHN1bGwnYXJpdG1ldGljYSBmbG9hdDMyPC9oMj4KCjxwPklsIFBMQyBTNy0xNTAwIHVzYSBlc2NsdXNpdmFtZW50ZSBSRUFMIChJRUVFIDc1NCBzaW5nbGUgcHJlY2lzaW9uLCAzMiBiaXQpIHBlciB0dXR0aSBpIGNhbGNvbGkuIExhIHNpbXVsYXppb25lIFB5dGhvbiByZXBsaWNhIHF1ZXN0byBjb21wb3J0YW1lbnRvIGNvbiA8Y29kZT51c2VfZmxvYXQzMj1UcnVlPC9jb2RlPi48L3A+Cgo8ZGl2IGNsYXNzPSJkYW5nZXIiPgo8c3Ryb25nPvCflLQgUGVyY2jDqSBmbG9hdDY0IFB5dGhvbiBkw6AgcmlzdWx0YXRpIGRpdmVyc2k8L3N0cm9uZz4KQ29uIHZhbG9yaSBsYXNlciB+NDAwbW0sIGxhIHNvbW1hIGRpIDIwLTMwIHZhbG9yaSBmbG9hdDMyIGFjY3VtdWxhdGkgc2VxdWVuemlhbG1lbnRlIGRpZmZlcmlzY2UgZGFsbGEgc29tbWEgbnVtcHkgZmxvYXQ2NCBhIGNhdXNhIGRlZ2xpIGVycm9yaSBkaSBhcnJvdG9uZGFtZW50by4gUXVlc3RhIGRpZmZlcmVuemEgcHXDsiBzcG9zdGFyZSBsYSBzb2dsaWEgZGkgcG9jaGkgY2VudGVzaW1pIGRpIG1tIOKAlCBzdWZmaWNpZW50ZSBwZXIgcGVyZGVyZSBvIGFjcXVpc2lyZSAxIGNhbXBpb25lIGFsIGJvcmRvIGRlbCBjbHVzdGVyLgo8L2Rpdj4KCjxkaXYgY2xhc3M9Im9rIj4KPHN0cm9uZz7inJMgU29sdXppb25lIGltcGxlbWVudGF0YSBpbiB2NC4zLjY0PC9zdHJvbmc+CklsIHdvcmtlciBkZWwgZ3JpZCBzZWFyY2ggdXNhIGRpcmV0dGFtZW50ZSBsZSBzb2dsaWUgPGNvZGU+YXJUaHJlc2hIaWdoL0xvdzwvY29kZT4gc2FsdmF0ZSBkYWwgUExDIHF1YW5kbyBkaXNwb25pYmlsaS4gRWxpbWluYSBjb21wbGV0YW1lbnRlIGlsIHByb2JsZW1hIGRpIHJpY2FsY29sbyBmbG9hdDMyIGUgZ2FyYW50aXNjZSBjaGUgaWwgd29ya2VyIHZlZGEgZXNhdHRhbWVudGUgbGUgc3Rlc3NlIHNvZ2xpZSBkZWwgUExDIHJlYWxlLgo8L2Rpdj4KCjxoMz5Eb3ZlIGZsb2F0MzIgw6ggY3JpdGljbzwvaDM+Cjx0YWJsZT4KPHRyPjx0aD5PcGVyYXppb25lPC90aD48dGg+SW1wYXR0bzwvdGg+PC90cj4KPHRyPjx0ZD5BY2N1bXVsbyByV2luU3VtIC8gcldpblN1bVNxPC90ZD48dGQ+QWx0byDigJQgc29tbWEgc2VxdWVuemlhbGUgZmxvYXQzMiB2cyBiYXRjaCBudW1weSBwdcOyIGRpZmZlcmlyZTwvdGQ+PC90cj4KPHRyPjx0ZD5EaXZpc2lvbmUgcGVyIGlXaW5OPC90ZD48dGQ+TWVkaW8g4oCUIElOVF9UT19SRUFMKCkgaW4gUExDID0gZmxvYXQzMiwgbnAuZmxvYXQzMih3aW5fbikgaW4gUHl0aG9uPC90ZD48L3RyPgo8dHI+PHRkPlNRUlQoclYpPC90ZD48dGQ+QmFzc28g4oCUIFNRUlQgw6ggZGV0ZXJtaW5pc3RpY28gc3UgZmxvYXQzMjwvdGQ+PC90cj4KPHRyPjx0ZD5Nb2x0aXBsaWNhemlvbmkgc2lnbWEgw5cgU2lnbWFGYWN0b3I8L3RkPjx0ZD5CYXNzbyDigJQgc2luZ29sYSBvcGVyYXppb25lIGZsb2F0MzI8L3RkPjwvdHI+CjwvdGFibGU+Cjwvc2VjdGlvbj4KCjxocj4KCjxzZWN0aW9uIGlkPSJ2NDctZmFsc2UtcG9zIj4KPGgyPkFuYWxpc2kgZmFsc2kgcG9zaXRpdmkg4oCUIHJpc29sdGkgaW4gdjQuNzwvaDI+Cgo8aDM+TWVjY2FuaXNtbyBkZWwgYnVnIChGLWJpcyBzdSBzdXBlcmZpY2llIGNvbiBkcmlmdCk8L2gzPgo8cD5JbCBjYXNvIHRpcGljbyBpZGVudGlmaWNhdG8gc3UgYWNxdWlzaXppb25pIHJlYWxpIChpZD0xMzA3LCBEQiBhbmVsbG8gQ0NXKTo8L3A+CjxkaXYgY2xhc3M9ImZvcm11bGEiPgo8ZGl2IGNsYXNzPSJsYmwiPkVzZW1waW86IGRyaWZ0ICsxLjI5bW0gaW4gMTLCsCwgcG9sYXJpdHk9MSAobmVnYXRpdm8pPC9kaXY+CkNhbXBpb25pIDAtOSAoaW5pemlvKTogIGxhc2VyIOKJiCAyMjUuNzMgbW0KQ2FtcGlvbmUgNDIgKDkuODXCsCk6ICAgIHJNID0gMjI2LjU0IG1tICDihpIgIFRoSGlOZWcgPSAyMjUuODQgbW0KCkYtYmlzIHJldHJvYXR0aXZvOiBjYW1waW9uaSA1LTkgKDAuNjPCsOKAkzEuMDXCsCk6CiAgaj01ICBsYXNlcj0yMjUuNTY3ICDiiaQgMjI1Ljg0NCAg4oaSIGNvbnRhCiAgaj02ICBsYXNlcj0yMjUuNjkzICDiiaQgMjI1Ljg0NCAg4oaSIGNvbnRhCiAgaj03ICBsYXNlcj0yMjUuNzc4ICDiiaQgMjI1Ljg0NCAg4oaSIGNvbnRhCiAgaj04ICBsYXNlcj0yMjUuODI5ICDiiaQgMjI1Ljg0NCAg4oaSIGNvbnRhCiAgaj05ICBsYXNlcj0yMjUuNzY2ICDiiaQgMjI1Ljg0NCAg4oaSIOKJpSBJX01pbkNvbnNlY3V0aXZlPTUg4oaSIEZBTFNBIERFVEVDVElPTiEKCkNhdXNhOiBiYXNlbGluZSB0YXJkYSAoMjI2LjU0KSA+PiBjYW1waW9uaSBpbml6aWFsaSAoMjI1LjczKQogICAgICAg4oaSIGkgY2FtcGlvbmkgaW5pemlhbGkgc2VtYnJhbm8gImJ1Y2hlIiByZXRyb2F0dGl2YW1lbnRlCiAgICAgICDihpIgbWEgc29ubyBzb2xvIGlsIHB1bnRvIGRpIHBhcnRlbnphIGRpIHVuIHNlZ25hbGUgaW4gc2FsaXRhCjwvZGl2PgoKPGgzPkZpeCB2NC43OiBlbGltaW5hemlvbmUgRi1iaXM8L2gzPgo8cD5Db24gRi1iaXMgcmltb3NzYSwgbGEgZGV0ZWN0aW9uIGF2dmllbmUgc29sbyBpbiBmb3J3YXJkIChibG9jY28gRikgY29uIDxjb2RlPmJBbmdsZUV4Y2VlZGVkPVRSVUU8L2NvZGU+LiBJbCBzZWduYWxlIGNyZXNjZW50ZSB2aWVuZSB2aXN0byBjb21lIGJhc2VsaW5lIGNyZXNjZW50ZSDihpIgc29nbGlhIGNyZXNjZSBjb24gbHVpIOKGkiBuZXNzdW5hIGZhbHNhIGRldGVjdGlvbi48L3A+Cgo8aDM+U2UgbGEgc2FsZGF0dXJhIMOoIGluIGRlYWQgem9uZTwvaDM+CjxwPkNvbiBGLWJpcyByaW1vc3NhLCBzZSBpbCBwZXp6byBwYXJ0ZSBlc2F0dGFtZW50ZSBzb3ByYSBsYSBzYWxkYXR1cmEgbmVpIHByaW1pIDxjb2RlPklfQmFzZWxpbmVXaW5kb3dEZWc8L2NvZGU+IGdyYWRpLCBsYSBzYWxkYXR1cmEgbm9uIHNhcsOgIHJpbGV2YXRhIGluIHF1ZXN0byBnaXJvLiBJbCBzaXN0ZW1hIHJpbGV2ZXLDoCBsYSBzYWxkYXR1cmEgYWwgZ2lybyBzdWNjZXNzaXZvIChjb24gPGNvZGU+SV9TdG9wT25XZWxkPUZBTFNFPC9jb2RlPikgbyBhbCBzZWNvbmRvIGdpcm8gKGNvbiByZS10cmlnZ2VyKS4gUGVyIGFwcGxpY2F6aW9uaSBjb24gYnVjaGkgc3UgYW5lbGxvLCBxdWVzdG8gc2NlbmFyaW8gw6ggbW9sdG8gaW1wcm9iYWJpbGU6IGkgYnVjaGkgc29ubyBwaWNjb2xpIHJpc3BldHRvIGFsIGdpcm8gZSBsYSBwcm9iYWJpbGl0w6AgZGkgcGFydGlyZSBlc2F0dGFtZW50ZSBzdSB1bm8gw6ggYmFzc2EuCjwvcD4KCjxkaXYgY2xhc3M9Im5vdGUiPgo8c3Ryb25nPvCfk4wgQWx0ZXJuYXRpdmEgcGVyIGRlYWQgem9uZSBjcml0aWNhPC9zdHJvbmc+ClVzYXJlIDxjb2RlPklfRmxhdFdhaXRFbmFibGU9RkFMU0U8L2NvZGU+IGUgPGNvZGU+SV9CYXNlbGluZVdpbmRvd0RlZzwvY29kZT4gbW9sdG8gcGljY29sbyAoMC41wrDigJMxLjDCsCkgcGVyIHJpZHVycmUgbGEgZGVhZCB6b25lIGFsIG1pbmltbyBuZWNlc3NhcmlvIHBlciBhdmVyZSBhbG1lbm8gMyBjYW1waW9uaSBuZWwgbG9vay1iYWNrLgo8L2Rpdj4KPC9zZWN0aW9uPgoKPGhyPgo8cCBzdHlsZT0iY29sb3I6IHZhcigtLW11dGVkKTsgZm9udC1zaXplOiAxMnB4OyB0ZXh0LWFsaWduOiBjZW50ZXI7IHBhZGRpbmc6IDIwcHggMDsiPgpXZWxkRmluZCB2NC43IOKAlCBNYW51YWxlIHRlY25pY28gY29tcGxldG8g4oCUIFNDTCB2NC43IMK3IFB5dGhvbiB2NC4zLjkzIMK3IFNpZW1lbnMgUzctMTUwMEYKPC9wPgoKPC9tYWluPgo8L2Rpdj4KCjxzY3JpcHQ+CmNvbnN0IHNlY3MgPSBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdzZWN0aW9uW2lkXScpOwpjb25zdCBsbmtzID0gZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnbmF2IGEnKTsKd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ3Njcm9sbCcsICgpID0+IHsKICBsZXQgY3VyID0gJyc7CiAgc2Vjcy5mb3JFYWNoKHMgPT4geyBpZiAod2luZG93LnNjcm9sbFkgPj0gcy5vZmZzZXRUb3AgLSA5MCkgY3VyID0gcy5pZDsgfSk7CiAgbG5rcy5mb3JFYWNoKGEgPT4geyBhLmNsYXNzTGlzdC50b2dnbGUoJ2FjdGl2ZScsIGEuZ2V0QXR0cmlidXRlKCdocmVmJykgPT09ICcjJyArIGN1cik7IH0pOwp9KTsKbG5rcy5mb3JFYWNoKGEgPT4gYS5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsIGUgPT4gewogIGUucHJldmVudERlZmF1bHQoKTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yKGEuZ2V0QXR0cmlidXRlKCdocmVmJykpLnNjcm9sbEludG9WaWV3KHtiZWhhdmlvcjonc21vb3RoJ30pOwp9KSk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"

    def _open_manual(self):
        """Apre il manuale HTML incorporato nel browser predefinito."""
        import webbrowser, base64, tempfile, os
        html = base64.b64decode(self._MANUAL_B64)
        tmp = os.path.join(tempfile.gettempdir(), "weld_manual_v43.html")
        with open(tmp, "wb") as f:
            f.write(html)
        webbrowser.open(f"file:///" + tmp.replace("\\", "/"))



    def _open_from_sqlite(self):
        import sqlite3 as _sq3
        default_path = (getattr(self, '_pv_sqlqry_path', None)
                        and self._pv_sqlqry_path.get().strip()
                        or os.path.join(os.path.expanduser("~"),
                                        "WeldExport", "weld_archive.sqlite"))

        dlg = tk.Toplevel(self)
        dlg.title("Seleziona riga SQLite - Analisi")
        dlg.configure(bg=DARK_BG)
        dlg.geometry("840x500")
        dlg.grab_set()

        bar = ttk.Frame(dlg); bar.pack(fill="x", padx=8, pady=6)
        ttk.Label(bar, text="File SQLite:", style="Muted.TLabel").pack(side="left")
        pv = tk.StringVar(value=default_path)
        ttk.Entry(bar, textvariable=pv, width=50).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(bar, text="\U0001f4c1", width=3, command=lambda: pv.set(
            filedialog.askopenfilename(
                title="Seleziona SQLite",
                filetypes=[("SQLite","*.sqlite *.db3 *.db"),("Tutti","*.*")]
            ) or pv.get())).pack(side="left")

        flt = ttk.Frame(dlg); flt.pack(fill="x", padx=8, pady=2)
        ttk.Label(flt, text="DB:", style="Muted.TLabel").pack(side="left")
        db_v = tk.StringVar(value="")
        ttk.Entry(flt, textvariable=db_v, width=8).pack(side="left", padx=(2,10))
        ttk.Label(flt, text="Trovata:", style="Muted.TLabel").pack(side="left")
        found_v = tk.StringVar(value="Tutti")
        ttk.Combobox(flt, textvariable=found_v, width=8, state="readonly",
                     values=["Tutti", "Si", "No"]).pack(side="left", padx=2)
        n_lbl_v = tk.StringVar(value="")
        ttk.Button(flt, text="\U0001f50d Aggiorna",
                   command=lambda: _refresh()).pack(side="left", padx=8)
        ttk.Label(flt, textvariable=n_lbl_v, style="Muted.TLabel").pack(side="left")

        tree_frm = ttk.Frame(dlg); tree_frm.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("id","timestamp","db","trovata","n_samp","det_ang","snr","filename")
        tree = ttk.Treeview(tree_frm, columns=cols, show="headings", height=16)
        for col, lbl, w in [
            ("id","ID",50),("timestamp","Timestamp",140),("db","DB",60),
            ("trovata","Trovata",60),("n_samp","Camp.",55),
            ("det_ang","Ang",65),("snr","SNR",60),("filename","File",220)]:
            tree.heading(col, text=lbl)
            tree.column(col, width=w, anchor="center" if col != "filename" else "w")
        tree.tag_configure("found", background="#0f2d0f", foreground=OK_CLR)
        tree.tag_configure("nofnd", background=ENTRY_BG,  foreground=TEXT_CLR)
        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        _rows_cache = []

        def _refresh():
            nonlocal _rows_cache
            for item in tree.get_children(): tree.delete(item)
            _rows_cache = []
            path = pv.get().strip()
            if not path or not os.path.isfile(path):
                n_lbl_v.set("File non trovato"); return
            try:
                con = _sq3.connect(path)
                where = []
                if db_v.get().strip().isdigit():
                    where.append(f"db_number={db_v.get().strip()}")
                if found_v.get() == "Si":  where.append("weld_found=1")
                elif found_v.get() == "No": where.append("weld_found=0")
                w_sql = ("WHERE " + " AND ".join(where)) if where else ""
                rows = con.execute(
                    f"SELECT * FROM acquisitions {w_sql} "
                    "ORDER BY timestamp DESC LIMIT 500").fetchall()
                con.close()
                _rows_cache = rows
                for r in rows:
                    tag = "found" if r[4] else "nofnd"
                    tree.insert("", "end", iid=str(r[0]), tags=(tag,), values=(
                        r[0], str(r[1])[:19] if r[1] else "",
                        r[2], "\u2713" if r[4] else "\u2717",
                        r[10], f"{r[5]:.1f}" if r[5] else "",
                        f"{r[9]:.2f}" if r[9] else "",
                        r[3] or ""))
                n_lbl_v.set(f"{len(rows)} righe")
            except Exception as e:
                n_lbl_v.set(f"Errore: {e}")

        def _load_selected():
            sel = tree.selection()
            if not sel: return
            row_id = int(sel[0])
            row = next((r for r in _rows_cache if r[0] == row_id), None)
            if row is None: return
            try:
                data = weld_sqlite_load_row(row)
                self.db_data = data
                fname = data.get("filename", f"SQLite_row{row_id}")
                self.lbl_file.config(text=f"\xf0\x9f\x97\x84 {fname}")
                self._update_results_panel()
                self._preload_sim_params()
                self._recompute()
                self._update_raw_tab()
                self.nb.select(0); self._sub_nb.select(0)
                self.app_log(f"Caricato da SQLite: {fname}", "ok")
                # *** v4.7 *** auto-run simulazione
                try: self.after(50, self._run_simulation)
                except Exception: pass
                dlg.destroy()
            except Exception as e:
                messagebox.showerror("Errore caricamento", str(e), parent=dlg)

        tree.bind("<Double-1>", lambda e: _load_selected())
        btn_bar = ttk.Frame(dlg); btn_bar.pack(fill="x", padx=8, pady=(2,8))
        ttk.Button(btn_bar, text="\u2714 Carica in Analisi", style="Accent.TButton",
                   command=_load_selected).pack(side="left")
        ttk.Button(btn_bar, text="Chiudi", command=dlg.destroy).pack(side="right")
        _refresh()

    def _open_file(self):
        path = filedialog.askopenfilename(title="Seleziona file .db TIA Portal",
            filetypes=[("TIA Portal DB", "*.db"), ("Tutti i file", "*.*")])
        if not path:
            return
        try:
            self.db_data = parse_db_file(path)
            fname = os.path.basename(path)
            self.lbl_file.config(text=fname)
            n = int(self.db_data["scalars"].get("iSamplesAcquired", 0))
            self.app_log(f"File caricato: {fname} ({n} campioni)", "ok")
            self._update_results_panel()
            self._preload_sim_params()
            self._recompute()
            self._update_raw_tab()
            self.nb.select(0); self._sub_nb.select(0)
            # *** v4.7 *** auto-run simulazione al caricamento
            try: self.after(50, self._run_simulation)
            except Exception: pass
        except Exception as e:
            self.app_log(f"Errore parsing file: {e}", "err")
            messagebox.showerror("Errore parsing", str(e))

    # ── UPDATE RESULTS PANEL ──────────────────────────────────
    def _generate_report(self):
        r = self.sim_result
        if not r:
            return
        self.txt_report.config(state="normal")
        self.txt_report.delete("1.0", "end")

        T = self.txt_report
        T.insert("end", "\n  ╔══════════════════════════════════════════════════╗\n", "title")
        T.insert("end", "  ║   REPORT DIAGNOSTICO — Simulazione v4.3         ║\n", "title")
        T.insert("end", "  ╚══════════════════════════════════════════════════╝\n\n", "title")

        # 1. VERDETTO
        T.insert("end", "  ▌ VERDETTO\n", "section")
        if r["weld_found"]:
            ps = r["peak_sigmas"]
            if ps > 10:   T.insert("end", f"    ✔ RILEVAMENTO ECCELLENTE  (SNR = {ps:.1f}σ)\n\n", "ok")
            elif ps > 5:  T.insert("end", f"    ✔ RILEVAMENTO ROBUSTO  (SNR = {ps:.1f}σ)\n\n", "ok")
            elif ps > 3:  T.insert("end", f"    ⚠ RILEVAMENTO ACCETTABILE  (SNR = {ps:.1f}σ)\n\n", "warn")
            else:         T.insert("end", f"    ⚠ RILEVAMENTO DEBOLE  (SNR = {ps:.1f}σ) — rivedere parametri\n\n", "err")
        else:
            T.insert("end", "    ✗ NESSUNA SALDATURA RILEVATA\n\n", "err")

        # 2. STATISTICHE ACQUISIZIONE
        fs = r["filter_stats"]
        T.insert("end", "  ▌ ACQUISIZIONE\n", "section")
        T.insert("end", f"    Campioni grezzi:     {fs['n_raw']}\n", "info")
        T.insert("end", f"    Campioni accettati:  {r['n_acquired']}  ({100 - fs['filter_ratio']:.1f}%)\n", "info")
        T.insert("end", f"    Riduzione filtro:    {fs['filter_ratio']:.1f}%\n", "info")
        T.insert("end", f"    Rifiutati Δ angolo:  {fs['n_rejected_angle']}\n", "info")
        T.insert("end", f"    Rifiutati Δ laser:   {fs['n_rejected_laser']}\n", "info")
        T.insert("end", f"    Rifiutati entrambi:  {fs['n_rejected_both']}\n", "info")
        T.insert("end", f"    Rifiutati range:     {fs['n_rejected_range']}\n", "info")
        T.insert("end", f"    Rifiutati asse fermo:{fs['n_rejected_axis']}\n\n", "info")

        if fs["filter_ratio"] > 80:
            T.insert("end", "    ⚠ ATTENZIONE: oltre 80% dei campioni rifiutati!\n", "err")
            T.insert("end", "      → Controllare MinAngleDelta e MinLaserDelta\n\n", "warn")

        # 3. BASELINE
        T.insert("end", "  ▌ BASELINE\n", "section")
        T.insert("end", f"    Media baseline:    {r['baseline_mean']:.3f}\n", "info")
        T.insert("end", f"    Sigma baseline:    {r['baseline_sigma']:.4f}\n", "info")
        T.insert("end", f"    Soglia adattiva:   {r['adaptive_threshold']:.3f}\n", "info")

        sg = r["sigma_arr"]
        if len(sg) > 0:
            sg_cv = np.std(sg) / (np.mean(sg) + 1e-9)
            if sg_cv > 1.0:
                T.insert("end", f"    ⚠ Sigma molto variabile (CV={sg_cv:.2f}) — superficie irregolare\n", "warn")
            else:
                T.insert("end", f"    ✔ Sigma stabile (CV={sg_cv:.2f})\n", "ok")

        # 4. COPERTURA FINESTRA
        wn = r["win_n_arr"]
        if len(wn) > 0:
            weak_pct = 100.0 * np.sum(wn < 3) / len(wn)
            T.insert("end", f"\n  ▌ COPERTURA FINESTRA LOOK-BACK\n", "section")
            T.insert("end", f"    win_n min: {int(np.min(wn))}  max: {int(np.max(wn))}  medio: {np.mean(wn):.1f}\n", "info")
            if weak_pct > 20:
                T.insert("end", f"    ⚠ {weak_pct:.1f}% campioni con win_n < 3 (soglia ereditata)\n", "err")
                T.insert("end", "      → Aumentare BaselineWindowDeg o ridurre MinAngleDelta\n", "warn")
            elif weak_pct > 5:
                T.insert("end", f"    ⚠ {weak_pct:.1f}% campioni con soglia debole\n", "warn")
            else:
                T.insert("end", f"    ✔ Copertura buona ({weak_pct:.1f}% zone deboli)\n", "ok")

        # 5. CLUSTER
        T.insert("end", f"\n  ▌ CLUSTER\n", "section")
        T.insert("end", f"    Cluster trovati:  {r['clusters_found']}\n", "info")
        T.insert("end", f"    Cluster validi:   {r['clusters_valid']}\n", "info")
        if r["clusters_valid"] > 1:
            T.insert("end", "    ⚠ Cluster multipli: possibili falsi positivi\n", "warn")
            T.insert("end", "      → Verificare bStopOnWeld=FALSE per analisi completa\n", "warn")
        if r["weld_found"]:
            T.insert("end", f"    Consecutivi:      {r['consecutive_count']}\n", "info")
            T.insert("end", f"    Delay detection:  {r['detection_delay_samples']} campioni dal fronte\n", "info")

        # 6. RISULTATO
        if r["weld_found"]:
            T.insert("end", f"\n  ▌ RISULTATO\n", "section")
            T.insert("end", f"    Detection:         {r['detection_angle']:.2f}°  (campione #{r['detection_sample']})\n", "info")
            T.insert("end", f"    Centro finale:     {r['weld_center']:.2f}°\n", "info")
            T.insert("end", f"    Arco saldatura:    {r['weld_start']:.2f}° → {r['weld_end']:.2f}°", "info")
            arc_w = abs(r['weld_end'] - r['weld_start'])  # *** v4.3.1 *** supporta CCW
            if arc_w > 180:
                arc_w = 360 - arc_w
            T.insert("end", f"  ({arc_w:.2f}° largo)\n", "info")
            T.insert("end", f"    Picco assoluto:    {r['peak_value']:.3f}\n", "info")
            T.insert("end", f"    Deviazione picco:  {r['peak_deviation']:.3f}\n", "info")
            T.insert("end", f"    SNR:               {r['peak_sigmas']:.1f}σ\n", "info")

        T.insert("end", "\n  " + "═" * 52 + "\n", "section")
        T.config(state="disabled")

    # ── BUILD: Tab Confronto Multi-DB ─────────────────────────
    def _param_eq(current_str, orig_str):
        """Confronto numerico robusto: '2' == '2.0' == '2.00' → True."""
        try:
            return float(current_str) == float(orig_str)
        except (ValueError, TypeError):
            return str(current_str).strip() == str(orig_str).strip()

    def _update_results_panel(self):
        if not self.db_data:
            return
        sc = self.db_data["scalars"]
        i_state  = int(sc.get("iState", 0))
        o_error  = sc.get("O_Error",  sc.get("bError", False))
        o_err_cd = int(sc.get("O_ErrorCode", sc.get("iErrorCode", 0)))
        o_done   = sc.get("O_Done",   sc.get("bDone",  False))
        n_acq    = int(sc.get("iSamplesAcquired", 0))
        trovata  = _sc(sc, "IO_RicercaSaldatura.Trovata", "bWeldFound", default=False)
        snames   = {0:"Idle", 1:"Online", 2:"Done", 3:"Error"}
        enames   = {0:"", 1:"Buffer overflow", 2:"No campioni", 3:"Parametri invalidi"}

        if o_error:
            msg = (f"⚠ ERRORE  iState={i_state}:{snames.get(i_state,'?')}"
                   f"  cod={o_err_cd}:{enames.get(o_err_cd,'?')}")
            self._status_var.set(msg)
            self._status_lbl.config(fg="#FF6B6B", bg="#3A1515")
        elif o_done:
            weld_str = "✓ SALDATURA" if trovata else "✗ No saldatura"
            self._status_var.set(f"✔ DONE  {weld_str}  ({n_acq} campioni)")
            self._status_lbl.config(fg=OK_CLR, bg="#153A15")
        else:
            self._status_var.set(f"  Stato PLC: {snames.get(i_state, str(i_state))}  ({n_acq} camp.)")
            self._status_lbl.config(fg=MUTED_CLR, bg=PANEL_BG)

        def _fmt(val):
            if val is None:            return "—"
            if isinstance(val, bool):  return "✓ Sì" if val else "✗ No"
            if isinstance(val, float): return f"{val:.3f}"
            return str(val)

        for key, var in self._udt_vars.items():
            var.set(_fmt(sc.get(key)))
        for key, var in self._out_vars.items():
            var.set(_fmt(sc.get(key)))
        for key, var in self._result_vars.items():
            var.set(_fmt(sc.get(key)))


    # ── AUTO-PRELOAD PARAMETRI DAL DB ────────────────────────
    def _preload_sim_params(self):
        """Legge I_* dal DB, precompila i campi del simulatore E salva i valori originali."""
        if not self.db_data:
            return
        sc = self.db_data["scalars"]
        
        # Mapping parametri GUI -> DB
        mapping = {
            "min_ang":   "I_MinAngleDelta",
            "min_las":   "I_MinLaserDelta",
            "win":       "I_BaselineWindowDeg",
            "sig_f":     "I_SigmaFactor",
            "min_abs":   "I_MinAbsDeviation",
            "hyst":      "I_HysteresisSigmas",
            "min_cons":  "I_MinConsecutive",
            "max_cons":  "I_MaxConsecutive",
            "min_valid": "I_MinLaserValidValue",
            "max_valid": "I_MaxLaserValidValue",
        }
        int_keys = {"min_cons", "max_cons"}
        
        # Resetta dizionario valori originali
        self._db_original_params = {}
        
        for sv_key, db_key in mapping.items():
            val = sc.get(db_key)
            if val is not None:
                try:
                    if sv_key in int_keys:
                        str_val = str(int(float(val)))
                    else:
                        str_val = str(round(float(val), 4))
                    self._sv[sv_key].set(str_val)
                    # Salva il valore originale
                    self._db_original_params[sv_key] = str_val
                except (ValueError, TypeError):
                    pass
                    
        # StopOnWeld
        stop = sc.get("I_StopOnWeld")
        if stop is not None:
            self._sim_stop_var.set(bool(stop))
            self._db_original_params["stop_on_weld"] = bool(stop)
            
        # *** v4.6 *** Carica parametri baseline adattiva e flat wait
        _v46_map = {
            'adapt_offset':  'I_AdaptBaselineOffset',
            'flat_samples':  'I_FlatWaitSamples',
            'flat_toll':     'I_FlatWaitToll',
            'det_start':     'I_DetectionStartDeg',   # *** v4.9 ***
        }
        for sv_k, db_k in _v46_map.items():
            val = sc.get(db_k)
            if val is not None and sv_k in self._sv:
                try:
                    if sv_k == 'flat_samples':
                        self._sv[sv_k].set(str(int(float(val))))
                    else:
                        self._sv[sv_k].set(str(round(float(val), 4)))
                    self._db_original_params[sv_k] = self._sv[sv_k].get()
                except (ValueError, TypeError): pass
        for bool_k, sv_var in [('I_AdaptBaselineEnable', '_sv46_adapt_en'),
                               ('I_FlatWaitEnable',      '_sv46_flat_en')]:
            val = sc.get(bool_k)
            if val is not None and hasattr(self, sv_var):
                _bval = bool(float(val))
                getattr(self, sv_var).set(_bval)
                self._db_original_params[bool_k] = _bval  # salva per restore

        # *** v4.3: Carica I_PeakPolarity ***
        polarity = sc.get("I_PeakPolarity")
        if polarity is not None:
            try:
                pol_val = int(float(polarity))
                self._sim_polarity_var.set(pol_val)
                self._db_original_params["polarity"] = pol_val
            except (ValueError, TypeError):
                pass
                
        # AxisStandStill: forza FALSE (l'asse era fermo durante export)
        self._sim_axis_var.set(False)
        self._db_original_params["axis_stand"] = False
        
        # Aggiorna indicatore stato
        # *** Aggiorna colonna DB origine nelle righe Soglie/Cluster ***
        if hasattr(self, '_sv_orig'):
            _orig_map = {
                "win":      self._db_original_params.get("win",      "—"),
                "sig_f":    self._db_original_params.get("sig_f",    "—"),
                "min_abs":  self._db_original_params.get("min_abs",  "—"),
                "hyst":     self._db_original_params.get("hyst",     "—"),
                "min_cons": self._db_original_params.get("min_cons", "—"),
                "max_cons": self._db_original_params.get("max_cons", "—"),
            }
            for _k, _v in _orig_map.items():
                if _k in self._sv_orig: self._sv_orig[_k].set(str(_v))
        self._update_param_status()
    
    def _restore_db_defaults(self):
        """Ripristina tutti i parametri ai valori originali del DB."""
        if not self._db_original_params:
            messagebox.showinfo("Ripristina", "Nessun DB caricato - nessun valore da ripristinare.")
            return
        
        # Ripristina tutti i parametri salvati
        for key, val in self._db_original_params.items():
            if key in self._sv:
                self._sv[key].set(val)
            elif key == "stop_on_weld":
                self._sim_stop_var.set(val)
            elif key == "polarity":
                self._sim_polarity_var.set(val)
            elif key == "axis_stand":
                self._sim_axis_var.set(val)
        # *** v4.6 *** Ripristina anche parametri v4.6
        for sv_k in ("adapt_offset", "flat_samples", "flat_toll"):
            orig = self._db_original_params.get(sv_k)
            if orig is not None and sv_k in self._sv:
                self._sv[sv_k].set(str(orig))
        if hasattr(self, '_sv46_adapt_en'):
            orig = self._db_original_params.get('I_AdaptBaselineEnable')
            if orig is not None: self._sv46_adapt_en.set(bool(orig))
        if hasattr(self, '_sv46_flat_en'):
            orig = self._db_original_params.get('I_FlatWaitEnable')
            if orig is not None: self._sv46_flat_en.set(bool(orig))
        
        self._update_param_status()
        
    @staticmethod
    def _param_eq(current_str, orig_str):
        """Confronto numerico robusto: '2' == '2.0' == '2.00' → True."""
        try:
            return float(current_str) == float(orig_str)
        except (ValueError, TypeError):
            return str(current_str).strip() == str(orig_str).strip()

    def _update_param_status(self):
        """Aggiorna l'indicatore che mostra se i parametri sono stati modificati."""
        if not self._db_original_params:
            self._param_status_var.set("")
            return
            
        # Conta parametri modificati
        modified = []
        for key, orig_val in self._db_original_params.items():
            if key in self._sv:
                current = self._sv[key].get()
                if not self._param_eq(current, orig_val):
                    modified.append(key)
            elif key == "stop_on_weld":
                if self._sim_stop_var.get() != orig_val:
                    modified.append("StopOnWeld")
            elif key == "polarity":
                if self._sim_polarity_var.get() != orig_val:
                    modified.append("Polarita")
        
        if modified:
            self._param_status_var.set(f"⚠ Modificati: {', '.join(modified[:3])}{'...' if len(modified)>3 else ''}")
        else:
            self._param_status_var.set("✓ Parametri = DB")

    # ── RECOMPUTE ─────────────────────────────────────────────
    def _recompute(self):
        """Ricalcola baseline in thread — UI sempre reattiva."""
        if not self.db_data:
            return
        arrays = self.db_data["arrays"]
        sc     = self.db_data["scalars"]
        samples = arrays.get("arSamples", [])
        if not samples:
            return
        n_acq = int(sc.get("iSamplesAcquired", 0)) or int(sc.get("iSampleIndex", 0))
        if n_acq == 0:
            return
        samples = samples[:n_acq]
        raw_ang = arrays.get("arAngles", [])
        ang     = raw_ang[:n_acq] if len(raw_ang) >= n_acq else None
        win = float(sc.get("I_BaselineWindowDeg", 10.0))
        sf  = float(sc.get("I_SigmaFactor",       3.0))
        mad = float(sc.get("I_MinAbsDeviation",    1.5))
        self._param_vars["window_deg"].set(f"{win}") if hasattr(self, '_param_vars') else None
        arrays_snap = dict(arrays); n_snap = n_acq

        def _compute():
            cd = compute_adaptive_baseline(samples, win, sf, mad, ang)
            def _tnz(arr, n):
                a = np.array(arr[:n], dtype=float)
                return a if np.any(a != 0) else None
            for db_key, cd_key in [("arMean","mean_db"),("arThreshHigh","thresh_hi_db"),
                                    ("arThreshLow","thresh_lo_db"),("arSigmaArr","sigma_db"),
                                    ("arThreshHighNeg","thresh_hi_neg_db"),  # *** v4.3.83 ***
                                    ("arThreshLowNeg","thresh_lo_neg_db")]:
                v = _tnz(arrays_snap.get(db_key, []), n_snap)
                if v is not None:
                    cd[cd_key] = v
            return cd

        def _done(cd):
            self.comp_data = cd
            self._analisi_dirty = {0,1,2,3,4}
            draw_map = {0: self._draw_signal_tab, 1: self._draw_polar_tab,
                        2: self._draw_hist_tab,   3: self._draw_raw_graphs_tab,
                        4: self._draw_samples_angles_tab}
            try:
                cur = self._sub_nb.index("current")
            except Exception:
                cur = 0
            fn = draw_map.get(cur)
            if fn:
                fn()
                self._analisi_dirty.discard(cur)

        self._run_in_thread(_compute, on_done=_done)

    # ── RUN SIMULATION ────────────────────────────────────────
    def _run_simulation(self):
        if not self.db_data:
            messagebox.showwarning("Nessun dato", "Caricare prima un file .db");  return
        arrays = self.db_data["arrays"];  sc = self.db_data["scalars"]
        raw_s = arrays.get("arSamples", []);  raw_a = arrays.get("arAngles", [])
        if not raw_s or not raw_a:
            messagebox.showwarning("Dati mancanti", "arSamples o arAngles non trovati");  return
        n_db = max(int(sc.get("iSamplesAcquired",0)), int(sc.get("iSampleIndex",0))) or len(raw_s)
        raw_s = raw_s[:n_db];  raw_a = raw_a[:n_db]

        # Aggiorna indicatore stato parametri
        self._update_param_status()

        try:
            sv = self._sv
            
            # ═══════════════════════════════════════════════════════════
            # LOGICA AUTOMATICA: confronta parametri correnti con DB originali
            # ═══════════════════════════════════════════════════════════
            
            # Parametri FILTRO: se uguali a DB → skip filter (dati già filtrati)
            filter_keys = ["min_ang", "min_las", "min_valid", "max_valid"]
            filter_matches_db = all(
                self._param_eq(sv[k].get(), self._db_original_params.get(k, "---NOMATCH---"))
                for k in filter_keys
            ) if self._db_original_params else False
            
            # Parametri SOGLIE: se uguali a DB → usa soglie DB
            thresh_keys = ["win", "sig_f", "min_abs", "hyst"]
            thresh_matches_db = all(
                self._param_eq(sv[k].get(), self._db_original_params.get(k, "---NOMATCH---"))
                for k in thresh_keys
            ) if self._db_original_params else False
            
            # Verifica disponibilità soglie nel DB
            db_th = arrays.get("arThreshHigh", [])[:n_db]
            has_db_thresholds = len(db_th) > 0 and any(v != 0 for v in db_th[:min(100, len(db_th))])
            
            # Decisione automatica
            skip_filter = filter_matches_db  # Se filtro = DB, i dati sono già filtrati
            use_db_thresholds = thresh_matches_db and has_db_thresholds  # Se soglie = DB e disponibili
            
            kwargs = dict(
                min_angle_delta = float(sv["min_ang"].get()),
                min_laser_delta = float(sv["min_las"].get()),
                max_samples     = int(float(sv["max_samp"].get())),
                min_laser_valid = float(sv["min_valid"].get()),
                max_laser_valid = float(sv["max_valid"].get()),
                axis_stand_still= self._sim_axis_var.get(),
                window_deg      = float(sv["win"].get()),
                sigma_factor    = float(sv["sig_f"].get()),
                min_abs_dev     = float(sv["min_abs"].get()),
                hyst_sigmas     = float(sv["hyst"].get()),
                min_consecutive = int(float(sv["min_cons"].get())),
                max_consecutive = int(float(sv["max_cons"].get())),
                stop_on_weld    = self._sim_stop_var.get(),
                peak_polarity   = self._sim_polarity_var.get(),
                skip_filter     = skip_filter,
                use_float32     = True,
                # *** v4.6 ***
                adapt_baseline_enable = getattr(self, '_sv46_adapt_en', None) and self._sv46_adapt_en.get(),
                adapt_baseline_offset = float(sv.get('adapt_offset', tk.StringVar(value='3.0')).get() if 'adapt_offset' in sv else 3.0),
                flat_wait_enable  = getattr(self, '_sv46_flat_en', None) and self._sv46_flat_en.get(),
                flat_wait_samples = int(float(sv['flat_samples'].get())) if 'flat_samples' in sv else 5,
                flat_wait_toll    = float(sv['flat_toll'].get()) if 'flat_toll' in sv else 0.5,
            )
            
            # Se usiamo soglie DB, passale
            if use_db_thresholds:
                kwargs["use_db_thresholds"] = True
                kwargs["db_thresh_hi"] = db_th
                kwargs["db_thresh_lo"] = arrays.get("arThreshLow", [])[:n_db]
                kwargs["db_thresh_hi_neg"] = arrays.get("arThreshHighNeg", [])[:n_db] if "arThreshHighNeg" in arrays else None
                kwargs["db_thresh_lo_neg"] = arrays.get("arThreshLowNeg", [])[:n_db] if "arThreshLowNeg" in arrays else None
                kwargs["db_mean"] = arrays.get("arMean", [])[:n_db]
                kwargs["db_sigma"] = arrays.get("arSigmaArr", [])[:n_db]
                
        except ValueError:
            messagebox.showerror("Parametri", "Verificare i valori numerici");  return

        try:
            self.sim_result = simulate_plc_realtime(raw_s, raw_a, **kwargs)
        except Exception as e:
            messagebox.showerror("Errore simulazione", str(e));  return

        # *** v4.3.74 *** Campioni post-detection: esistono nel DB ma la simulazione
        # con stop_on_weld=True si ferma alla detection. Li aggiungiamo al result
        # per la sola visualizzazione (nessun impatto sull'algoritmo).
        _n_sim = self.sim_result["n_acquired"]
        if _n_sim < len(raw_s):
            self.sim_result["post_det_samples"] = np.array(raw_s[_n_sim:], dtype=float)
            self.sim_result["post_det_angles"]  = np.array(raw_a[_n_sim:], dtype=float)
        else:
            self.sim_result["post_det_samples"] = np.array([], dtype=float)
            self.sim_result["post_det_angles"]  = np.array([], dtype=float)

        r = self.sim_result
        fs = r["filter_stats"]
        sr = self._sr

        # Banner con confronto PLC e indicazione modalità
        plc_det_sample = int(sc.get("iDetectedAtSample", -1))
        plc_det_angle = sc.get("rDetectedAtAngle", 0)
        plc_found = plc_det_sample > 0
        
        # Indicatore modalità
        mode_parts = []
        if skip_filter:
            mode_parts.append("Filtro=DB")
        else:
            mode_parts.append("Filtro=CALC")
        if use_db_thresholds:
            mode_parts.append("Soglie=DB")
        else:
            mode_parts.append("Soglie=CALC")
        mode_txt = f" [{'/'.join(mode_parts)}]"
        
        if r["weld_found"]:
            qual = ("ROBUSTO" if r["peak_sigmas"]>5 else "ACCETTABILE" if r["peak_sigmas"]>3 else "DEBOLE")
            det_a = r["detection_angle"]
            det_s = r["detection_sample"]
            pol_txt = "POS" if r.get("detected_polarity", 0) == 0 else "NEG"
            # Confronto con PLC
            match_plc = (det_s == plc_det_sample) if plc_found else False
            match_txt = " ✓MATCH" if match_plc else (f" ⚠PLC@{plc_det_sample}" if plc_found else "")
            self._sim_banner_var.set(f"✓ SALDATURA {det_a:.1f}° {qual} {pol_txt}{match_txt}{mode_txt}")
            self._sim_banner.config(fg=OK_CLR, bg="#153A15")
        else:
            if plc_found:
                self._sim_banner_var.set(f"✗ NON TROVATA (PLC@{plc_det_sample} {plc_det_angle:.1f}°){mode_txt}")
            else:
                self._sim_banner_var.set(f"✗ Nessuna saldatura{mode_txt}")
            self._sim_banner.config(fg="#FF6B6B", bg="#3A1515")

        sr["n_raw"].set(str(fs["n_raw"]));           sr["n_acq"].set(str(r["n_acquired"]))
        sr["rej_ang"].set(str(fs["n_rejected_angle"]))
        sr["rej_las"].set(str(fs["n_rejected_laser"]))
        sr["rej_range"].set(str(fs.get("n_rejected_range", 0)))
        sr["rej_axis"].set(str(fs.get("n_rejected_axis", 0)))
        ds = r["detection_sample"]
        sr["det_samp"].set(str(ds) if ds >= 0 else "—")
        sr["det_ang"].set(f"{r['detection_angle']:.2f}°" if ds >= 0 else "—")
        sr["det_delay"].set(str(r["detection_delay_samples"]) if r["detection_delay_samples"] >= 0 else "—")
        sr["trovata"].set("✓ Sì" if r["weld_found"] else "✗ No")
        sr["out_pos"].set(f"{r['weld_center']:.2f}")
        sr["start"].set(f"{r['weld_start']:.2f}")
        sr["end"].set(f"{r['weld_end']:.2f}")
        sr["peak"].set(f"{r['peak_value']:.3f}")
        sr["dev"].set(f"{r['peak_deviation']:.3f}")
        sr["sigmas"].set(f"{r['peak_sigmas']:.2f} σ")
        sr["clusters"].set(f"{r['clusters_valid']} / {r['clusters_found']}")
        sr["consec"].set(str(r["consecutive_count"]))
        sr["bl_mean"].set(f"{r['baseline_mean']:.3f}")
        sr["bl_sig"].set(f"{r['baseline_sigma']:.4f}")
        # *** v4.7 *** indicatore superficie piatta
        if hasattr(self, '_sr_flat_found'):
            ff = r.get('flat_found', False)
            ae = r.get('angle_exceeded', False)
            self._sr_flat_found.set(
                '✓ trovata' if ff else ('✗ non trovata' if self._sv46_flat_en.get() else '— disabilitata'))
            self._sr_angle_exc.set('✓' if ae else '✗')

        self._sim_draw_all()
        self._generate_report()
        self.nb.select(1)

    def _sim_draw_all(self):
        """Ridisegna subito il tab simulatore attivo; marca gli altri sporchi."""
        _sim_draw_map = [
            self._draw_sim_signal,
            self._draw_sim_filter_wrapper,
            self._draw_sim_polar,
            self._draw_sim_thresholds,
            self._draw_sim_timeline,
            self._draw_sim_db_compare,
            self._draw_sim_window_coverage,
            self._draw_sim_rejections,
            self._draw_sim_snr,
            self._draw_sim_cluster_detail,
        ]
        self._sim_dirty = set(range(len(_sim_draw_map)))
        try:
            cur = self.sim_nb.index("current")
        except Exception:
            cur = 0
        fn = _sim_draw_map[cur] if cur < len(_sim_draw_map) else _sim_draw_map[0]
        fn()
        self._sim_dirty.discard(cur)
        # Salva il map per uso futuro nel handler tab change
        self._sim_draw_map = _sim_draw_map

    def _draw_sim_filter_wrapper(self):
        """Wrapper per _draw_sim_filter che recupera i dati dal db_data."""
        if not self.db_data or not self.sim_result:
            return
        sc    = self.db_data["scalars"]
        ar    = self.db_data["arrays"]
        n_db  = max(int(sc.get("iSamplesAcquired", 0)),
                    int(sc.get("iSampleIndex", 0))) or len(ar.get("arSamples", []))
        raw_s = ar.get("arSamples", [])[:n_db]
        raw_a = ar.get("arAngles",  [])[:n_db]
        self._draw_sim_filter(raw_s, raw_a)

    # ── DRAW SIM: Segnale + Detection point ───────────────────
    def _draw_sim_signal(self):
        r = self.sim_result
        fa = r["filt_angles"];  fs = r["filt_samples"]
        th = r["thresh_hi"];    tl = r["thresh_lo"];  mn = r["mean_arr"]
        delta = fs - mn

        for ax in (self.ax_sa1, self.ax_sa2):
            ax.cla()
        self._style_axes(self.ax_sa1,
            f"Simulazione v4.3  —  {r['n_acquired']} campioni  |  "
            f"{'★ Detection: ' + str(r['detection_angle'])+'°' if r['weld_found'] else 'Nessuna saldatura'}",
            "Angolo (°)", "Valore laser")
        self._style_axes(self.ax_sa2, "Δ segnale − baseline", "Angolo (°)", "Δ")

        self.ax_sa1.plot(fa, fs, color=SIM_CLR, linewidth=1.3, label="Laser filtrato", zorder=3)
        self.ax_sa1.plot(fa, mn, color=OK_CLR,  linewidth=1.5, linestyle="--", label="Baseline live", zorder=4)
        # *** v4.3.83 *** Soglie in base alla polarita configurata
        _pol = self._sim_polarity_var.get()  # usa sempre la polarity CONFIGURATA (non detected)
        _thn = r.get("thresh_hi_neg"); _tln = r.get("thresh_lo_neg")
        _show_pos = (_pol in (0, 2)) or _thn is None
        _show_neg = (_pol in (1, 2)) and _thn is not None and len(_thn) > 0
        if _show_pos:
            self.ax_sa1.plot(fa, th, color=WARN_CLR, linewidth=1.2, linestyle="-.",
                             label="ThreshHigh (+)", zorder=4)
            self.ax_sa1.plot(fa, tl, color=WARN_CLR, linewidth=0.6, linestyle=":",
                             alpha=0.6, label="ThreshLow (+)", zorder=4)
            self.ax_sa1.fill_between(fa, tl, th, alpha=0.05, color=WARN_CLR)
        if _show_neg:
            self.ax_sa1.plot(fa, _thn, color="#4fc3f7", linewidth=1.2, linestyle="-.",
                             label="ThreshHigh (\xe2\x88\x92)", zorder=4)
            self.ax_sa1.plot(fa, _tln, color="#4fc3f7", linewidth=0.6, linestyle=":",
                             alpha=0.6, label="ThreshLow (\xe2\x88\x92)", zorder=4)
            self.ax_sa1.fill_between(fa, _thn, _tln, alpha=0.05, color="#4fc3f7")
        if not _show_pos and not _show_neg:  # fallback
            self.ax_sa1.plot(fa, th, color=WARN_CLR, linewidth=1.2, linestyle="-.",
                             label="ThreshHigh", zorder=4)
            self.ax_sa1.fill_between(fa, tl, th, alpha=0.05, color=WARN_CLR)

        # Cluster
        for c in r["clusters"]:
            ca = fa[c["start"]:c["end"]+1]
            if len(ca) > 0:
                col = WELD_CLR if c["valid"] else MUTED_CLR
                self.ax_sa1.axvspan(float(ca[0]), float(ca[-1]),
                                    alpha=0.3 if c["valid"] else 0.1, color=col, zorder=1)

        # *** v4.7.1 *** Flat surface: linea verticale + counter orizzontale animato
        if self._sv46_flat_en.get() and len(fa) > 0 and len(fs) > 0:
            ff  = r.get('flat_found', False)
            ffa = r.get('flat_found_at_angle', None)
            fws = max(1, r.get('flat_wait_samples', 5))
            frames = r.get('frames', [])
            # -- Linea orizzontale animata: iFlatConsecCount normalizzato 0-1 --
            if frames:
                _fc_a = np.array([f['angle']             for f in frames], dtype=np.float32)
                _fc_v = np.array([f.get('flat_consec',0) for f in frames], dtype=np.float32)
                _fc_n = np.clip(_fc_v / fws, 0.0, 1.0)
                ax2r = self.ax_sa2.twinx()
                ax2r.set_ylim(-0.08, 1.25)
                ax2r.set_ylabel('Flat counter', color='#3fb950', fontsize=7)
                ax2r.tick_params(axis='y', colors='#3fb950', labelsize=6)
                ax2r.spines['right'].set_color('#3fb950'); ax2r.spines['right'].set_alpha(0.4)
                for _sp in ('top','left','bottom'): ax2r.spines[_sp].set_visible(False)
                # Linea target
                ax2r.axhline(1.0, color='#3fb950', lw=0.7, ls=':', alpha=0.4)
                # Traccia segmento per segmento con colore variabile
                for _i in range(1, len(_fc_n)):
                    _seg_x = [float(_fc_a[_i-1]), float(_fc_a[_i])]
                    _seg_y = [float(_fc_n[_i-1]), float(_fc_n[_i])]
                    _clr = ('#3fb950' if _fc_n[_i] >= 1.0
                            else '#ff6b6b' if (_fc_n[_i] == 0.0 and _fc_n[_i-1] > 0.0)
                            else '#8b949e')
                    ax2r.plot(_seg_x, _seg_y, color=_clr, lw=1.4, alpha=0.85, solid_capstyle='round')
                ax2r.set_zorder(self.ax_sa2.get_zorder() + 1)
                ax2r.patch.set_visible(False)
            # -- Linea verticale: angolo in cui bFlatSurfaceFound diventa TRUE --
            if ff and ffa is not None:
                self.ax_sa1.axvline(ffa, color='#3fb950', lw=1.4, ls='--',
                                    alpha=0.9, zorder=5, label=f'✓ Sup.piatta {ffa:.1f}°')
                self.ax_sa2.axvline(ffa, color='#3fb950', lw=1.0, ls='--', alpha=0.6, zorder=5)
                self.ax_sa1.axvspan(float(fa[0]), ffa, alpha=0.04, color='#3fb950', zorder=0)
            elif not ff:
                _ymn = float(np.nanmin(fs)); _ymx = float(np.nanmax(fs))
                self.ax_sa1.text(float(fa[0]), _ymn + (_ymx-_ymn)*0.05,
                                 '✗ Sup.piatta non trovata', color='#ff6b6b', fontsize=8, va='bottom')

        # ★ Linea detection immediata (diversa dal centro finale!)
        ds = r["detection_sample"]
        if ds >= 0:
            det_ang = r["detection_angle"]
            self.ax_sa1.axvline(det_ang, color=DET_CLR, linewidth=2.5, linestyle="-",
                                label=f"★ Detection immediata {det_ang:.1f}°", zorder=6)
            # Freccia con annotazione
            y_pos = float(fs[ds]) if ds < len(fs) else float(np.nanmax(fs))
            self.ax_sa1.annotate(
                f"★ {det_ang:.1f}°",
                xy=(det_ang, y_pos),
                xytext=(det_ang + max((fa[-1]-fa[0])*0.05, 2.0), y_pos),
                color=DET_CLR, fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=DET_CLR, lw=1.5),
            )

        if r["weld_found"] and r["weld_center"] != r["detection_angle"]:
            self.ax_sa1.axvline(r["weld_center"], color=WELD_CLR, linewidth=1.5,
                                linestyle="--", alpha=0.7,
                                label=f"Centro finale {r['weld_center']:.1f}°")

        # *** v4.3.74 *** Tratto post-detection (solo visualizzazione)
        _pda8 = r.get("post_det_angles",  np.array([]))
        _pds8 = r.get("post_det_samples", np.array([]))
        if len(_pda8) > 0:
            _ja8 = np.concatenate([[fa[-1]], _pda8]) if len(fa) > 0 else _pda8
            _js8 = np.concatenate([[fs[-1]], _pds8]) if len(fs) > 0 else _pds8
            self.ax_sa1.plot(_ja8, _js8, color=SIM_CLR, linewidth=1.0,
                             linestyle="--", alpha=0.45,
                             label=f"Post-det ({len(_pda8)} camp.)", zorder=2)
            self.ax_sa1.axvspan(float(_pda8[0]), float(_pda8[-1]),
                                alpha=0.04, color=SIM_CLR, zorder=0)
        self.ax_sa1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        # Soglia delta: usa thresh negativa se polarity=1, abs delta per polarity=2
        if _pol == 1 and _thn is not None and len(_thn) > 0:
            td = mn - _thn  # distanza dalla soglia negativa (positiva per definizione)
            _delta_check = -delta  # delta negativo -> inverti per il confronto
            self.ax_sa2.fill_between(fa, delta, 0, where=_delta_check >= td, color=WELD_CLR, alpha=0.5, label="Sotto soglia (\xe2\x88\x92)")
            self.ax_sa2.fill_between(fa, delta, 0, where=_delta_check < td,  color=SIM_CLR,  alpha=0.2)
            self.ax_sa2.plot(fa, -td, color="#4fc3f7", linewidth=1.2, linestyle="-.", label="Soglia \xce\x94 (\xe2\x88\x92)")
        else:
            td = th - mn
            self.ax_sa2.fill_between(fa, delta, 0, where=delta >= td, color=WELD_CLR, alpha=0.5, label="Sopra soglia")
            self.ax_sa2.fill_between(fa, delta, 0, where=delta < td,  color=SIM_CLR,  alpha=0.2)
            self.ax_sa2.plot(fa, td, color=WARN_CLR, linewidth=1.2, linestyle="-.", label="Soglia Δ")
        self.ax_sa2.axhline(0, color=MUTED_CLR, linewidth=0.8)
        if ds >= 0:
            self.ax_sa2.axvline(r["detection_angle"], color=DET_CLR, linewidth=2, linestyle="-", alpha=0.8)
        # Post-det nel delta (nessuna soglia disponibile: usa ultima mean)
        if len(_pda8) > 0:
            _ja8b = np.concatenate([[fa[-1]], _pda8]) if len(fa) > 0 else _pda8
            _lm8b = float(mn[-1]) if len(mn) > 0 else 0.0
            _jd8b = np.concatenate([[delta[-1]], _pds8 - _lm8b]) if len(delta) > 0 else _pds8 - _lm8b
            self.ax_sa2.plot(_ja8b, _jd8b, color=SIM_CLR, linewidth=0.8,
                             linestyle="--", alpha=0.40, label="Post-det delta")
        self.ax_sa2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        self.fig_sa.tight_layout(pad=2.5);  self.canvas_sa.draw_idle()

    # ── DRAW SIM: Effetto filtro ───────────────────────────────
    def _draw_sim_filter(self, raw_s, raw_a):
        r  = self.sim_result
        fa = r["filt_angles"];  fv = r["filt_samples"]
        ra = np.array(raw_a, dtype=float);  rs = np.array(raw_s, dtype=float)  # v4.3.2 angoli grezzi
        st = r["filter_stats"]

        for ax in (self.ax_sb1, self.ax_sb2):
            ax.cla()
        self._style_axes(self.ax_sb1,
            f"Grezzo ({st['n_raw']}) vs Filtrato ({st['n_acquired']})  —  {st['filter_ratio']:.1f}% riduzione",
            "Angolo (°)", "Valore laser")
        self._style_axes(self.ax_sb2, "Densità campioni / grado", "Angolo (°)", "N")

        self.ax_sb1.scatter(ra, rs, color=MUTED_CLR, s=3, alpha=0.35, label="Grezzo")
        self.ax_sb1.scatter(fa, fv, color=SIM_CLR, s=8, alpha=0.85, label="Filtrato")
        self.ax_sb1.plot(fa, fv, color=SIM_CLR, linewidth=0.7, alpha=0.4)
        ds = r["detection_sample"]
        if ds >= 0:
            self.ax_sb1.axvline(r["detection_angle"], color=DET_CLR, linewidth=2,
                                label=f"★ Detection {r['detection_angle']:.1f}°")
        if r["weld_found"]:
            self.ax_sb1.axvspan(r["weld_start"], r["weld_end"], alpha=0.18, color=WELD_CLR)
        self.ax_sb1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        bins = np.linspace(0, 360, 73)
        hr, _ = np.histogram(ra, bins=bins);  hf, _ = np.histogram(fa, bins=bins)
        cx = (bins[:-1]+bins[1:])/2
        self.ax_sb2.bar(cx, hr, width=4.5, color=MUTED_CLR, alpha=0.5, label="Grezzo")
        self.ax_sb2.bar(cx, hf, width=3.0, color=SIM_CLR,   alpha=0.8, label="Filtrato")
        if ds >= 0:
            self.ax_sb2.axvline(r["detection_angle"], color=DET_CLR, linewidth=2)
        self.ax_sb2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_sb.tight_layout(pad=2.5);  self.canvas_sb.draw_idle()

    # ── DRAW SIM: Polare ──────────────────────────────────────
    def _draw_sim_polar(self):
        r = self.sim_result;  fa = r["filt_angles"];  fs = r["filt_samples"]
        mn = r["mean_arr"];  th = r["thresh_hi"]
        self.ax_sc.cla();  self._style_polar(self.ax_sc)

        ar = np.radians(fa)
        # Profilo normalizzato: baseline = 1.0, deviazioni amplificate
        dev = fs - mn
        dev_max = max(np.nanmax(np.abs(dev)), 1e-6)
        rn = 1.0 + 0.6 * dev / dev_max
        rn_bl = np.ones_like(ar)
        rn_th = 1.0 + 0.6 * (th - mn) / dev_max

        # Profilo laser
        self.ax_sc.plot(ar, rn, color=SIM_CLR, linewidth=1.3, alpha=0.9, label="Profilo laser")
        self.ax_sc.fill_between(ar, rn_bl, rn, where=rn > rn_bl,
                                 alpha=0.15, color=WELD_CLR, interpolate=True)
        self.ax_sc.fill_between(ar, rn, rn_bl, where=rn <= rn_bl,
                                 alpha=0.08, color=SIM_CLR, interpolate=True)
        # Cerchio baseline
        self.ax_sc.plot(ar, rn_bl, color=OK_CLR, linewidth=1.0, linestyle="--", alpha=0.6, label="Baseline")
        # Cerchio soglia
        self.ax_sc.plot(ar, rn_th, color=WARN_CLR, linewidth=0.8, linestyle=":", alpha=0.5, label="Soglia")

        ds = r["detection_sample"]
        if ds >= 0:
            da = r["detection_angle"]
            self.ax_sc.axvline(np.radians(da), color=DET_CLR, linewidth=2.5, alpha=0.9,
                                label=f"★ Detection {da:.1f}°")
        if r["weld_found"]:
            ws, we, wc = r["weld_start"], r["weld_end"], r["weld_center"]
            if ws != we:
                arc = np.radians(np.linspace(ws, we, 60))
                r_max = np.nanmax(rn) * 1.05
                self.ax_sc.fill_between(arc, 0, np.full_like(arc, r_max),
                                         alpha=0.20, color=WELD_CLR, label="Zona saldatura")
            self.ax_sc.set_title(
                f"Profilo rilevato  |  OutPos {wc:.1f}°  |  {r['peak_sigmas']:.1f}σ" +
                (f"  |  ★ {r['detection_angle']:.1f}°" if ds >= 0 else ""),
                color=DET_CLR, fontsize=9, pad=14)
        else:
            self.ax_sc.set_title("Nessuna saldatura trovata", color=MUTED_CLR, fontsize=10, pad=14)
        self.ax_sc.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR,
                           loc="upper right", bbox_to_anchor=(1.15, 1.0))
        self.ax_sc.set_xticks(np.radians([0, 90, 180, 270]))
        self.ax_sc.set_xticklabels(["0°","90°","180°","270°"], color=MUTED_CLR, fontsize=9)
        self.fig_sc.tight_layout(pad=2.5);  self.canvas_sc.draw_idle()

    # ── DRAW SIM: Soglie per campione ─────────────────────────
    def _draw_sim_thresholds(self):
        r = self.sim_result
        fa = r["filt_angles"];  fs = r["filt_samples"]
        mn = r["mean_arr"];     sg = r["sigma_arr"]
        th = r["thresh_hi"];    tl = r["thresh_lo"]
        for ax in (self.ax_sd1, self.ax_sd2):
            ax.cla()
        self._style_axes(self.ax_sd1, "Baseline e Sigma (calcolati live per campione)", "Angolo (°)", "Valore")
        self._style_axes(self.ax_sd2, "ThreshHigh / ThreshLow per campione", "Angolo (°)", "Soglia")

        self.ax_sd1.plot(fa, fs, color=SIM_CLR,  linewidth=0.9, alpha=0.5, label="Segnale")
        self.ax_sd1.plot(fa, mn, color=OK_CLR,   linewidth=1.5, label="Mean")
        ax1b = self.ax_sd1.twinx()
        ax1b.plot(fa, sg, color=WARN_CLR, linewidth=1.0, linestyle="--", alpha=0.7, label="Sigma")
        ax1b.set_ylabel("Sigma", color=WARN_CLR, fontsize=8)
        ax1b.tick_params(colors=WARN_CLR, labelsize=8)
        for sp in ax1b.spines.values():
            sp.set_edgecolor(BORDER_CLR)
        self.ax_sd1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        # *** v4.3.83 *** Soglie in base alla polarita configurata
        _pol3 = self._sim_polarity_var.get()  # usa sempre la polarity CONFIGURATA
        _thn3 = r.get("thresh_hi_neg"); _tln3 = r.get("thresh_lo_neg")
        _show_pos3 = (_pol3 in (0, 2)) or _thn3 is None
        _show_neg3 = (_pol3 in (1, 2)) and _thn3 is not None and len(_thn3) > 0
        self.ax_sd2.plot(fa, fs, color=SIM_CLR,  linewidth=0.9, alpha=0.5, label="Segnale")
        if _show_pos3:
            self.ax_sd2.plot(fa, th, color=WELD_CLR, linewidth=1.5, label="ThreshHigh (+)")
            self.ax_sd2.plot(fa, tl, color=WARN_CLR, linewidth=1.0, linestyle="--", label="ThreshLow (+)")
            self.ax_sd2.fill_between(fa, tl, th, alpha=0.06, color=WARN_CLR)
        if _show_neg3:
            self.ax_sd2.plot(fa, _thn3, color="#4fc3f7", linewidth=1.5, label="ThreshHigh (\xe2\x88\x92)")
            self.ax_sd2.plot(fa, _tln3, color="#4fc3f7", linewidth=1.0, linestyle="--", label="ThreshLow (\xe2\x88\x92)")
            self.ax_sd2.fill_between(fa, _thn3, _tln3, alpha=0.06, color="#4fc3f7")
        if not _show_pos3 and not _show_neg3:  # fallback
            self.ax_sd2.plot(fa, th, color=WELD_CLR, linewidth=1.5, label="ThreshHigh")
            self.ax_sd2.fill_between(fa, tl, th, alpha=0.06, color=WARN_CLR)
        ds = r["detection_sample"]
        if ds >= 0:
            self.ax_sd2.axvline(r["detection_angle"], color=DET_CLR, linewidth=2,
                                linestyle="-", label=f"★ {r['detection_angle']:.1f}°")
        for c in r["clusters"]:
            if c["valid"]:
                ca = r["filt_angles"][c["start"]:c["end"]+1]
                if len(ca) > 0:
                    self.ax_sd2.axvspan(float(ca[0]), float(ca[-1]), alpha=0.20, color=WELD_CLR)
        # *** v4.3.74 *** Laser post-detection (nessuna soglia disponibile)
        _pda9 = r.get("post_det_angles",  np.array([]))
        _pds9 = r.get("post_det_samples", np.array([]))
        if len(_pda9) > 0:
            _ja9 = np.concatenate([[fa[-1]], _pda9]) if len(fa) > 0 else _pda9
            _js9 = np.concatenate([[fs[-1]], _pds9]) if len(fs) > 0 else _pds9
            self.ax_sd2.plot(_ja9, _js9, color=SIM_CLR, linewidth=0.9,
                             linestyle="--", alpha=0.40, label="Post-det (no soglia)")
        self.ax_sd2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_sd.tight_layout(pad=2.5);  self.canvas_sd.draw_idle()

    # ── DRAW SIM: Timeline detection ─────────────────────────
    def _draw_sim_timeline(self):
        """
        Mostra campione per campione l'andamento verso il detection:
          - Valore laser (con ThreshHigh)
          - Δ segnale − baseline
          - Contatore cluster (da 0 fino a iMinConsecutive e oltre)
        La linea verticale rossa mostra esattamente DOVE e QUANDO
        il flag bWeldFound è scattato.
        """
        r = self.sim_result
        frames = r["frames"]
        if not frames:
            return

        idxs    = np.array([f["idx"]       for f in frames])
        lasers  = np.array([f["laser"]     for f in frames])
        thhi    = np.array([f["th_hi"]     for f in frames])
        means   = np.array([f["mean"]      for f in frames])
        counts  = np.array([f["cl_count"]  for f in frames])
        in_cl   = np.array([f["in_cluster"] for f in frames])
        delta   = lasers - means
        td      = thhi - means

        for ax in (self.ax_se1, self.ax_se2, self.ax_se3):
            ax.cla()
        self._style_axes(self.ax_se1, "Valore laser campione per campione", "Indice campione", "Valore")
        self._style_axes(self.ax_se2, "Δ segnale − baseline  vs soglia Δ", "Indice campione", "Δ")
        self._style_axes(self.ax_se3, "Contatore cluster  (→ detection quando ≥ min_consecutivi)",
                         "Indice campione", "Consecutivi")

        ds = r["detection_sample"]

        # ── Grafico 1: laser + thresh ──
        self.ax_se1.plot(idxs, lasers, color=SIM_CLR, linewidth=1.2, label="Laser")
        self.ax_se1.plot(idxs, means,  color=OK_CLR,  linewidth=1.0, linestyle="--", label="Mean", alpha=0.7)
        self.ax_se1.plot(idxs, thhi,   color=WARN_CLR, linewidth=1.0, linestyle="-.", label="ThreshHigh", alpha=0.8)
        # Colora zona cluster
        self.ax_se1.fill_between(idxs, lasers, means,
                                  where=in_cl, color=WELD_CLR, alpha=0.25, label="In cluster")
        if ds >= 0:
            self.ax_se1.axvline(ds, color=DET_CLR, linewidth=2.5,
                                label=f"★ Detection campione {ds}")
        self.ax_se1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR,
                            labelcolor=TEXT_CLR, loc="upper left")

        # ── Grafico 2: delta vs soglia ──
        self.ax_se2.fill_between(idxs, delta, 0, where=delta >= td,
                                  color=WELD_CLR, alpha=0.5, label="Sopra soglia Δ")
        self.ax_se2.fill_between(idxs, delta, 0, where=delta < td,
                                  color=SIM_CLR, alpha=0.2)
        self.ax_se2.plot(idxs, td, color=WARN_CLR, linewidth=1.0, linestyle="-.", label="Soglia Δ")
        self.ax_se2.axhline(0, color=MUTED_CLR, linewidth=0.8)
        if ds >= 0:
            self.ax_se2.axvline(ds, color=DET_CLR, linewidth=2.5)
        self.ax_se2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        # ── Grafico 3: contatore cluster ──
        self.ax_se3.fill_between(idxs, counts, 0, where=counts > 0,
                                  color=SIM_CLR, alpha=0.4, step="post")
        self.ax_se3.step(idxs, counts, color=SIM_CLR, linewidth=1.5, where="post", label="Contatore cluster")

        # Linea soglia iMinConsecutive
        min_cons = int(float(self._sv["min_cons"].get()))
        self.ax_se3.axhline(min_cons, color=WARN_CLR, linewidth=1.5, linestyle="--",
                             label=f"iMinConsecutive = {min_cons}")

        if ds >= 0:
            self.ax_se3.axvline(ds, color=DET_CLR, linewidth=2.5, label=f"★ Detection")
            # Annotazione sul punto esatto
            cnt_at_det = int(counts[np.searchsorted(idxs, ds)])
            self.ax_se3.annotate(
                f"★ cnt={cnt_at_det}",
                xy=(ds, cnt_at_det),
                xytext=(ds + max(len(idxs)*0.03, 2), cnt_at_det + 0.3),
                color=DET_CLR, fontsize=9, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=DET_CLR, lw=1.5),
            )
        self.ax_se3.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        # *** v4.3.74 *** Campioni post-detection nella timeline
        _pda10 = r.get("post_det_angles",  np.array([]))
        _pds10 = r.get("post_det_samples", np.array([]))
        if len(_pds10) > 0:
            _n_end10 = r["n_acquired"]
            _pi10 = np.arange(_n_end10, _n_end10 + len(_pds10))
            _ji10 = np.concatenate([[idxs[-1]], _pi10]) if len(idxs) > 0 else _pi10
            _jl10 = np.concatenate([[lasers[-1]], _pds10]) if len(lasers) > 0 else _pds10
            _lm10 = float(means[-1]) if len(means) > 0 else 0.0
            _jd10 = np.concatenate([[delta[-1]], _pds10 - _lm10]) if len(delta) > 0 else _pds10 - _lm10
            _post_lbl10 = f"Post-det ({len(_pds10)} camp.)"
            self.ax_se1.plot(_ji10, _jl10, color=SIM_CLR, linewidth=0.9,
                              linestyle="--", alpha=0.40, label=_post_lbl10)
            self.ax_se2.plot(_ji10, _jd10, color=SIM_CLR, linewidth=0.8,
                              linestyle="--", alpha=0.40, label="Post-det delta")
            _z10 = np.zeros(len(_pds10) + 1)
            self.ax_se3.plot(_ji10, _z10, color=SIM_CLR, linewidth=0.8,
                              linestyle="--", alpha=0.35)
            for _ax10 in (self.ax_se1, self.ax_se2, self.ax_se3):
                _ax10.axvspan(float(_pi10[0]), float(_pi10[-1]),
                               alpha=0.04, color=SIM_CLR, zorder=0)

        self.fig_se.tight_layout(pad=2.8);  self.canvas_se.draw_idle()

    # ── DRAW SIM: Confronto DB vs Sim ─────────────────────────
    def _draw_sim_db_compare(self):
        r = self.sim_result
        if not self.db_data:
            return
        arrays = self.db_data["arrays"];  sc = self.db_data["scalars"]
        n_db = self._resolve_n_acq()
        fa = r["filt_angles"];  n_sim = r["n_acquired"]
        n = min(n_db, n_sim)
        if n < 2:
            return

        mean_db = np.array(arrays.get("arMean", [])[:n], dtype=float)
        th_db   = np.array(arrays.get("arThreshHigh", [])[:n], dtype=float)
        mean_sim = r["mean_arr"][:n]
        th_sim   = r["thresh_hi"][:n]
        ang      = fa[:n]

        for ax in (self.ax_sf1, self.ax_sf2, self.ax_sf3):
            ax.cla()
        self._style_axes(self.ax_sf1, f"Baseline Mean: PLC vs Simulatore  (n={n})", "Angolo (°)", "Mean")
        self._style_axes(self.ax_sf2, "ThreshHigh: PLC vs Simulatore", "Angolo (°)", "ThreshHigh")
        self._style_axes(self.ax_sf3, "Residuo (PLC − Sim)", "Angolo (°)", "Δ")

        vm = mean_db != 0
        if vm.any():
            self.ax_sf1.plot(ang[vm], mean_db[vm], color=OK_CLR, linewidth=1.5, label="Mean PLC")
            self.ax_sf1.plot(ang[vm], mean_sim[vm], color=SIM_CLR, linewidth=1.5, linestyle="--", label="Mean Sim")
            res_m = mean_db[vm] - mean_sim[vm]
            self.ax_sf3.plot(ang[vm], res_m, color=ACCENT, linewidth=1.0, label="Δ Mean")
            self.ax_sf3.fill_between(ang[vm], res_m, 0, alpha=0.2, color=ACCENT)
        self.ax_sf1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        vt = th_db != 0
        if vt.any():
            self.ax_sf2.plot(ang[vt], th_db[vt], color=WARN_CLR, linewidth=1.5, label="ThHi PLC")
            self.ax_sf2.plot(ang[vt], th_sim[vt], color=SIM_CLR, linewidth=1.5, linestyle="--", label="ThHi Sim")
            res_t = th_db[vt] - th_sim[vt]
            self.ax_sf3.plot(ang[vt], res_t, color=WELD_CLR, linewidth=1.0, label="Δ ThreshHi")
            self.ax_sf3.fill_between(ang[vt], res_t, 0, alpha=0.15, color=WELD_CLR)
        self.ax_sf2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.ax_sf3.axhline(0, color=MUTED_CLR, linewidth=0.8)
        self.ax_sf3.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_sf.tight_layout(pad=2.8);  self.canvas_sf.draw_idle()

    # ── DRAW SIM: Copertura Finestra Look-back ────────────────
    def _draw_sim_window_coverage(self):
        r = self.sim_result
        fa = r["filt_angles"];  fs = r["filt_samples"]
        wn = r["win_n_arr"]
        mn = r["mean_arr"];  th = r["thresh_hi"]

        for ax in (self.ax_sg1, self.ax_sg2):
            ax.cla()
        self._style_axes(self.ax_sg1,
            f"Copertura finestra look-back  (min richiesto: 3)  —  min={int(np.min(wn))}, max={int(np.max(wn))}, medio={np.mean(wn):.1f}",
            "Angolo (°)", "win_n")
        self._style_axes(self.ax_sg2, "Segnale con zone a soglia debole (win_n < 3)", "Angolo (°)", "Valore")

        # Colormap per win_n
        colors = np.where(wn >= 3, OK_CLR, "#FF6B6B")
        self.ax_sg1.bar(fa, wn, width=np.median(np.diff(fa)) if len(fa) > 1 else 0.5,
                        color=[OK_CLR if w >= 3 else "#FF6B6B" for w in wn], alpha=0.7)
        self.ax_sg1.axhline(3, color=WARN_CLR, linewidth=1.5, linestyle="--", label="Soglia minima (3)")
        weak_pct = 100.0 * np.sum(wn < 3) / max(len(wn), 1)
        self.ax_sg1.annotate(f"Zone deboli: {weak_pct:.1f}%", xy=(0.02, 0.95),
                              xycoords="axes fraction", color=WARN_CLR, fontsize=9)
        self.ax_sg1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        self.ax_sg2.plot(fa, fs, color=SIM_CLR, linewidth=1.0, label="Laser", alpha=0.8)
        self.ax_sg2.plot(fa, mn, color=OK_CLR, linewidth=1.2, linestyle="--", label="Mean", alpha=0.7)
        self.ax_sg2.plot(fa, th, color=WARN_CLR, linewidth=1.0, linestyle="-.", label="ThreshHi", alpha=0.7)
        # Evidenzia zone deboli
        weak = wn < 3
        if np.any(weak):
            self.ax_sg2.fill_between(fa, np.nanmin(fs), np.nanmax(fs),
                                      where=weak, alpha=0.15, color="#FF6B6B", label="win_n < 3")
        self.ax_sg2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_sg.tight_layout(pad=2.5);  self.canvas_sg.draw_idle()

    # ── DRAW SIM: Mappa Campioni Rifiutati ────────────────────
    def _draw_sim_rejections(self):
        r = self.sim_result
        rejs = r["rejections"]
        fa = r["filt_angles"];  fs = r["filt_samples"]

        for ax in (self.ax_sh1, self.ax_sh2):
            ax.cla()
        self._style_axes(self.ax_sh1,
            f"Mappa rifiuti  ({len(rejs)} rifiutati su {r['filter_stats']['n_raw']} grezzi)",
            "Angolo (°)", "Valore laser")
        self._style_axes(self.ax_sh2, "Distribuzione rifiuti per motivo e zona angolare", "Angolo (°)", "Conteggio")

        # Scatter accettati
        self.ax_sh1.scatter(fa, fs, color=SIM_CLR, s=6, alpha=0.4, label="Accettati", zorder=2)

        # Scatter rifiutati per motivo
        rej_colors = {"angle": WARN_CLR, "laser": ACCENT, "both": MUTED_CLR,
                       "range": "#FF6B6B", "axis": "#9B59B6"}
        rej_labels = {"angle": "Δ angolo", "laser": "Δ laser", "both": "Δ ang+las",
                       "range": "Fuori range", "axis": "Asse fermo"}
        for reason in ["angle", "laser", "both", "range", "axis"]:
            pts = [(r[1], r[2]) for r in rejs if r[3] == reason]
            if pts:
                angs, vals = zip(*pts)
                self.ax_sh1.scatter(angs, vals, color=rej_colors[reason], s=10, alpha=0.7,
                                     marker="x", linewidth=0.8, label=rej_labels[reason], zorder=3)
        self.ax_sh1.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR, ncol=2)

        # Istogramma per zona
        bins = np.linspace(0, 360, 37)
        cx = (bins[:-1] + bins[1:]) / 2
        bottom = np.zeros(len(cx))
        for reason in ["angle", "laser", "both", "range", "axis"]:
            angs_r = [r[1] for r in rejs if r[3] == reason]
            if angs_r:
                h, _ = np.histogram(angs_r, bins=bins)
                self.ax_sh2.bar(cx, h, width=9, bottom=bottom, color=rej_colors[reason],
                                 alpha=0.7, label=rej_labels[reason])
                bottom += h
        if self.ax_sh2.get_legend_handles_labels()[1]:
            self.ax_sh2.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR, ncol=2)
        self.fig_sh.tight_layout(pad=2.5);  self.canvas_sh.draw_idle()

    # ── DRAW SIM: SNR Rolling ─────────────────────────────────
    def _draw_sim_snr(self):
        r = self.sim_result
        fa = r["filt_angles"];  sg = r["sigma_arr"];  snr = r["rolling_snr"]

        for ax in (self.ax_si1, self.ax_si2):
            ax.cla()
        self._style_axes(self.ax_si1,
            f"Sigma rolling (noise floor)  —  media={np.mean(sg):.4f}  max={np.max(sg):.4f}",
            "Angolo (°)", "σ locale")
        self._style_axes(self.ax_si2,
            f"SNR locale  |dev| / σ  —  picco={np.max(snr):.1f}",
            "Angolo (°)", "SNR")

        self.ax_si1.fill_between(fa, sg, 0, alpha=0.3, color=ACCENT)
        self.ax_si1.plot(fa, sg, color=ACCENT, linewidth=1.2, label="σ locale")
        mean_sg = np.mean(sg)
        self.ax_si1.axhline(mean_sg, color=OK_CLR, linewidth=1, linestyle="--", label=f"Media σ = {mean_sg:.4f}")
        p95 = np.percentile(sg, 95) if len(sg) > 5 else mean_sg * 2
        self.ax_si1.axhline(p95, color=WARN_CLR, linewidth=1, linestyle=":", label=f"95° pctl = {p95:.4f}")
        # Evidenzia zone rumorose
        noisy = sg > p95
        if np.any(noisy):
            self.ax_si1.fill_between(fa, 0, np.max(sg) * 1.1, where=noisy, alpha=0.1, color="#FF6B6B")
        self.ax_si1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        self.ax_si2.fill_between(fa, snr, 0, alpha=0.3, color=SIM_CLR)
        self.ax_si2.plot(fa, snr, color=SIM_CLR, linewidth=1.0)
        self.ax_si2.axhline(3, color=WARN_CLR, linewidth=1, linestyle="--", label="SNR = 3")
        self.ax_si2.axhline(5, color=OK_CLR, linewidth=1, linestyle="--", label="SNR = 5")
        ds = r["detection_sample"]
        if ds >= 0:
            self.ax_si2.axvline(r["detection_angle"], color=DET_CLR, linewidth=2, label=f"★ Detection")
        self.ax_si2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_si.tight_layout(pad=2.5);  self.canvas_si.draw_idle()

    # ── DRAW SIM: Dettaglio Cluster ───────────────────────────
    def _draw_sim_cluster_detail(self):
        r = self.sim_result
        fa = r["filt_angles"]
        self.txt_clusters.config(state="normal")
        self.txt_clusters.delete("1.0", "end")

        self.txt_clusters.insert("end", "═══════════════════════════════════════════════════════════════\n", "header")
        self.txt_clusters.insert("end", "  DETTAGLIO CLUSTER — Simulazione v4.3\n", "header")
        self.txt_clusters.insert("end", "═══════════════════════════════════════════════════════════════\n\n", "header")

        clusters = r["clusters"]
        if not clusters:
            self.txt_clusters.insert("end", "  Nessun cluster trovato.\n", "invalid")
        else:
            best_idx = -1
            best_dev = -1e10
            for ci, c in enumerate(clusters):
                if c["valid"] and c["peak_dev"] > best_dev:
                    best_dev = c["peak_dev"];  best_idx = ci

            hdr = f"  {'#':>3}  {'Start°':>8}  {'End°':>8}  {'Cnt':>4}  {'Peak':>10}  {'PeakDev':>10}  {'Valid':>6}  Note\n"
            self.txt_clusters.insert("end", hdr, "header")
            self.txt_clusters.insert("end", "  " + "─" * 72 + "\n", "header")

            for ci, c in enumerate(clusters):
                sa = fa[c["start"]] if c["start"] < len(fa) else 0
                ea = fa[c["end"]]   if c["end"]   < len(fa) else 0
                note = ""
                if ci == best_idx:
                    note = "★ BEST"
                if c.get("wrap"):
                    note += " [WRAP]"
                tag = "best" if ci == best_idx else ("valid" if c["valid"] else "invalid")
                line = f"  {ci+1:>3}  {sa:>8.2f}  {ea:>8.2f}  {c['count']:>4}  {c['peak']:>10.3f}  {c['peak_dev']:>10.3f}  {'✓' if c['valid'] else '✗':>6}  {note}\n"
                self.txt_clusters.insert("end", line, tag)

            self.txt_clusters.insert("end", "\n\n")
            self.txt_clusters.insert("end", f"  Totale cluster: {len(clusters)}  |  Validi: {r['clusters_valid']}  |  Invalidi: {len(clusters) - r['clusters_valid']}\n", "info")

            if r["weld_found"]:
                self.txt_clusters.insert("end", f"\n  ★ Rilevamento: angolo {r['detection_angle']:.2f}°  |  campione #{r['detection_sample']}\n", "best")
                self.txt_clusters.insert("end", f"    Centro finale: {r['weld_center']:.2f}°  |  Arco: {r['weld_start']:.2f}° → {r['weld_end']:.2f}°\n", "valid")
                self.txt_clusters.insert("end", f"    Peak: {r['peak_value']:.3f}  |  Dev: {r['peak_deviation']:.3f}  |  SNR: {r['peak_sigmas']:.1f}σ\n", "valid")

        self.txt_clusters.config(state="disabled")

    # ── PARAMETER SWEEP ───────────────────────────────────────
    def _build_multidb_tab(self, parent):
        top = ttk.Frame(parent);  top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="Sovrapponi profili da DB diversi (es. Anello vs Virola)",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="📂  Carica 2° DB", style="Accent.TButton",
                   command=self._open_second_db).pack(side="right", padx=4)
        self.lbl_file2 = ttk.Label(top, text="Nessun 2° file", style="Muted.TLabel")
        self.lbl_file2.pack(side="right", padx=6)

        self.db_data2 = None

        self.fig_multi = Figure(facecolor=DARK_BG)
        self.ax_m1 = self.fig_multi.add_subplot(211)
        self.ax_m2 = self.fig_multi.add_subplot(212)
        self._style_axes(self.ax_m1, "Confronto profili laser", "Angolo (°)", "Valore")
        self._style_axes(self.ax_m2, "Confronto baseline + soglie", "Angolo (°)", "Valore")
        self.fig_multi.tight_layout(pad=2.5)
        c = FigureCanvasTkAgg(self.fig_multi, parent)
        c.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(c, parent).pack(fill="x")
        self.canvas_multi = c

    def _open_second_db(self):
        path = filedialog.askopenfilename(title="Seleziona 2° file .db",
            filetypes=[("TIA Portal DB", "*.db"), ("Tutti i file", "*.*")])
        if not path:
            return
        try:
            self.db_data2 = parse_db_file(path)
            self.lbl_file2.config(text=os.path.basename(path))
            self._draw_multidb()
        except Exception as e:
            messagebox.showerror("Errore parsing 2° DB", str(e))

    def _draw_multidb(self):
        if not self.db_data or not self.db_data2:
            return
        for ax in (self.ax_m1, self.ax_m2):
            ax.cla()

        colors_db = [(ACCENT, OK_CLR), (WELD_CLR, SIM_CLR)]
        for di, (db, clr) in enumerate([(self.db_data, colors_db[0]), (self.db_data2, colors_db[1])]):
            sc = db["scalars"];  ar = db["arrays"]
            name = db.get("filename", f"DB{di+1}")
            n = int(sc.get("iSamplesAcquired", 0)) or int(sc.get("iSampleIndex", 0))
            if n < 1:
                continue
            s = np.array(ar.get("arSamples", [])[:n], dtype=float)
            a = np.array(ar.get("arAngles", [])[:n], dtype=float)  # v4.3.2 angoli grezzi
            m = np.array(ar.get("arMean", [])[:n], dtype=float)
            th = np.array(ar.get("arThreshHigh", [])[:n], dtype=float)

            self.ax_m1.plot(a, s, color=clr[0], linewidth=1.2, alpha=0.8, label=f"{name} laser")

            wc = _sc(sc, "IO_RicercaSaldatura.OutPosizioneAsse", "rWeldAngleCenter")
            ws = sc.get("rWeldAngleStart");  we = sc.get("rWeldAngleEnd")
            trov = _sc(sc, "IO_RicercaSaldatura.Trovata", "bWeldFound", default=False)
            if trov and ws:
                self.ax_m1.axvspan(float(ws), float(we), alpha=0.15, color=clr[0])
                self.ax_m1.axvline(float(wc), color=clr[0], linewidth=2, linestyle="--", alpha=0.7)

            vm = m != 0
            if vm.any():
                self.ax_m2.plot(a[vm], m[vm], color=clr[1], linewidth=1.2, alpha=0.8, label=f"{name} mean")
            vt = th != 0
            if vt.any():
                self.ax_m2.plot(a[vt], th[vt], color=clr[0], linewidth=1.0, linestyle="--", alpha=0.6, label=f"{name} thresh")

        self._style_axes(self.ax_m1, "Confronto profili laser", "Angolo (°)", "Valore laser")
        self._style_axes(self.ax_m2, "Confronto baseline + soglie", "Angolo (°)", "Valore")
        self.ax_m1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.ax_m2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig_multi.tight_layout(pad=2.5);  self.canvas_multi.draw_idle()

    # ── TAB PLC READER ──────────────────────────────────────────
    def _build_plc_tab(self, parent):
        self._plc_decoded = None
        # ── Sotto-notebook: DB Reader | Monitor RT | Auto Export ──
        plc_nb = ttk.Notebook(parent)
        plc_nb.pack(fill="both", expand=True)
        fr_db = ttk.Frame(plc_nb);  plc_nb.add(fr_db, text="  📥  DB Reader  ")
        fr_rt = ttk.Frame(plc_nb);  plc_nb.add(fr_rt, text="  📡  Monitor Real-Time  ")
        fr_ae = ttk.Frame(plc_nb);  plc_nb.add(fr_ae, text="  ⚡  Auto Export  ")
        self._build_plc_dbreader(fr_db)
        self._build_plc_realtime(fr_rt)
        self._build_plc_autoexport(fr_ae)

    def _build_plc_dbreader(self, parent):
        """Sub-tab DB Reader: lettura one-shot e salvataggio."""
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane, width=400);  pane.add(left, weight=0)
        right = ttk.Frame(pane);  pane.add(right, weight=1)

        frm = ttk.LabelFrame(left, text="  Connessione PLC  ", padding=8)
        frm.pack(fill="x", padx=6, pady=6)
        row1 = ttk.Frame(frm);  row1.pack(fill="x", pady=2)
        for label, attr, default, w in [
            ("IP:",   "plc_ip",   "172.28.2.1",   18),
            ("DB:",   "plc_db",   "28160",          7),
        ]:
            ttk.Label(row1, text=label).pack(side="left", padx=(4, 0))
            sv = tk.StringVar(value=default)
            setattr(self, f"_pv_{attr}", sv)
            ttk.Entry(row1, textvariable=sv, width=w).pack(side="left", padx=(2, 6))
        row2 = ttk.Frame(frm);  row2.pack(fill="x", pady=2)
        for label, attr, default, w in [
            ("Rack:", "plc_rack", "0", 4),
            ("Slot:", "plc_slot", "1", 4),
        ]:
            ttk.Label(row2, text=label).pack(side="left", padx=(4, 0))
            sv = tk.StringVar(value=default)
            setattr(self, f"_pv_{attr}", sv)
            ttk.Entry(row2, textvariable=sv, width=w).pack(side="left", padx=(2, 6))

        frm3 = ttk.LabelFrame(left, text="  Azioni  ", padding=8)
        frm3.pack(fill="x", padx=6, pady=4)
        for text, cmd, style in [
            ("🔌  Test Connessione",          self._plc_test_connection, "TButton"),
            ("📥  Leggi DB dal PLC",          self._plc_read_db,         "Plc.TButton"),
            ("💾  Connetti + Leggi + Salva",  self._plc_auto_save,       "TButton"),
            ("📊  Connetti + Leggi + Viewer", self._plc_auto_viewer,     "Accent.TButton"),
        ]:
            ttk.Button(frm3, text=text, command=cmd, style=style).pack(
                fill="x", pady=3, padx=4)

        frm4 = ttk.LabelFrame(left, text="  Risultato  ", padding=8)
        frm4.pack(fill="x", padx=6, pady=4)
        self._plc_result_vars = {}
        for label in ("Stato:", "Campioni:", "LaserValue:", "Angle:", "PeakValue:",
                       "PeakDev:", "Polarity:", "DB Size:"):
            row_r = ttk.Frame(frm4);  row_r.pack(fill="x", pady=1)
            ttk.Label(row_r, text=label, width=12, anchor="w").pack(side="left", padx=4)
            sv = tk.StringVar(value="—")
            self._plc_result_vars[label] = sv
            ttk.Label(row_r, textvariable=sv, style="PlcResult.TLabel").pack(side="left")
        self._pv_plc_status = tk.StringVar(value="Pronto")
        ttk.Label(left, textvariable=self._pv_plc_status, style="Muted.TLabel",
                  anchor="w").pack(fill="x", padx=10, pady=(6, 2))

        log_lf = ttk.LabelFrame(right, text="  Log  ", padding=4)
        log_lf.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(log_lf);  sb.pack(side="right", fill="y")
        self._plc_log = tk.Text(log_lf, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas", 9),
                                wrap="word", yscrollcommand=sb.set,
                                insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self._plc_log.pack(fill="both", expand=True)
        sb.config(command=self._plc_log.yview)
        self._plc_log.tag_config("ok",   foreground=OK_CLR)
        self._plc_log.tag_config("err",  foreground=WELD_CLR)
        self._plc_log.tag_config("info", foreground=ACCENT)
        self._plc_log.tag_config("warn", foreground=WARN_CLR)
        self._plc_log_msg("=== PLC DB Reader - WeldFind v4.3 ===\n", "info")
        if not SNAP7_AVAILABLE:
            self._plc_log_msg("\u26a0 python-snap7 NON installato!\n", "err")
            self._plc_log_msg("  pip install python-snap7\n\n", "warn")
        else:
            self._plc_log_msg("\u2713 python-snap7 disponibile\n\n", "ok")
        self._plc_log_msg("PREREQUISITI PLC:\n", "info")
        self._plc_log_msg("  1. S7_Optimized_Access := FALSE\n")
        self._plc_log_msg("  2. PUT/GET abilitato\n")
        self._plc_log_msg("  3. S7-1500: Rack=0 Slot=1\n\n")
        self._plc_log_msg("Ogni bottone e autonomo: connette, legge ed esegue.\n", "info")

    def _build_plc_autoexport(self, parent):
        """Sub-tab Auto Export: trigger + smistamento + fino a 10 DB simultanei."""
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left_outer = ttk.Frame(pane, width=400); pane.add(left_outer, weight=0)
        left_outer.pack_propagate(False)
        right = ttk.Frame(pane); pane.add(right, weight=1)

        ae_canv = tk.Canvas(left_outer, bg=DARK_BG, highlightthickness=0)
        ae_scrl = ttk.Scrollbar(left_outer, orient="vertical", command=ae_canv.yview)
        left = ttk.Frame(ae_canv)
        left.bind("<Configure>", lambda e: ae_canv.configure(scrollregion=ae_canv.bbox("all")))
        _ae_win = ae_canv.create_window((0,0), window=left, anchor="nw")
        ae_canv.configure(yscrollcommand=ae_scrl.set)
        ae_canv.bind("<Configure>", lambda e: ae_canv.itemconfigure(_ae_win, width=e.width))
        ae_canv.bind("<MouseWheel>", lambda e: ae_canv.yview_scroll(int(-1*(e.delta/120)),"units"))
        ae_scrl.pack(side="right", fill="y")
        ae_canv.pack(side="left", fill="both", expand=True)

        ae_frm = ttk.LabelFrame(left, text="  Auto Export  ", padding=6)
        ae_frm.pack(fill="x", padx=6, pady=4)

        ae_r1 = ttk.Frame(ae_frm); ae_r1.pack(fill="x", pady=2)
        self._pv_autoexp_on = tk.BooleanVar(value=False)
        tk.Checkbutton(ae_r1, text="Abilita", variable=self._pv_autoexp_on,
            bg=DARK_BG, fg=OK_CLR, selectcolor="#1f6feb",
            activebackground=DARK_BG, activeforeground=OK_CLR,
            font=("Consolas",9,"bold")).pack(side="left")
        ttk.Label(ae_r1, text="Trigger:").pack(side="left", padx=(8,2))
        self._pv_autoexp_trigger = tk.StringVar(value="bStart_Prev \u2193")
        cmb = ttk.Combobox(ae_r1, textvariable=self._pv_autoexp_trigger,
            width=18, state="readonly",
            values=["bStart_Prev \u2193", "O_Done \u2191", "iState \u2192 Done"])
        cmb.current(0); cmb.pack(side="left", padx=2)

        ae_r2 = ttk.Frame(ae_frm); ae_r2.pack(fill="x", pady=2)
        ttk.Label(ae_r2, text="Poll:").pack(side="left")
        self._pv_autoexp_poll = tk.StringVar(value="100")
        ttk.Entry(ae_r2, textvariable=self._pv_autoexp_poll, width=6).pack(side="left", padx=2)
        ttk.Label(ae_r2, text="ms", style="Muted.TLabel").pack(side="left")
        self._pv_autoexp_viewer = tk.BooleanVar(value=False)  # mantenuta per compatibilita

        ae_r3 = ttk.Frame(ae_frm); ae_r3.pack(fill="x", pady=2)
        tk.Label(ae_r3, text="\u2713 Buoni:", bg=DARK_BG, fg=OK_CLR,
                 font=("Consolas",9), width=9, anchor="w").pack(side="left")
        self._pv_autoexp_path = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "Buoni"))
        ttk.Entry(ae_r3, textvariable=self._pv_autoexp_path, width=20).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(ae_r3, text="\U0001f4c1", width=3,
                   command=self._autoexp_browse_path).pack(side="left")

        ae_r3b = ttk.Frame(ae_frm); ae_r3b.pack(fill="x", pady=2)
        tk.Label(ae_r3b, text="\u2717 Scarti:", bg=DARK_BG, fg=WELD_CLR,
                 font=("Consolas",9), width=9, anchor="w").pack(side="left")
        self._pv_autoexp_path_rej = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "Scarti"))
        ttk.Entry(ae_r3b, textvariable=self._pv_autoexp_path_rej, width=20).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(ae_r3b, text="\U0001f4c1", width=3,
                   command=self._autoexp_browse_path_rej).pack(side="left")

        # ── Tabella 10 DB ─────────────────────────────────────────
        db_lf = ttk.LabelFrame(left, text="  DB da monitorare (max 10)  ", padding=6)
        db_lf.pack(fill="x", padx=6, pady=4)
        hdr = ttk.Frame(db_lf); hdr.pack(fill="x", pady=(0,2))
        for txt, w in [("\u2611",2),("DB number",9),("Viewer",5),("\u2713 / \u2717",12)]:
            ttk.Label(hdr, text=txt, style="Muted.TLabel", width=w,
                      anchor="center").pack(side="left", padx=3)

        _AE_DB_DEFAULTS = ["28010","28160","28300","28320","28340","28360","28380","28400","28420","28440"]
        self._ae_db_rows = []
        for i in range(10):
            r = ttk.Frame(db_lf); r.pack(fill="x", pady=1)
            en  = tk.BooleanVar(value=(i < len([x for x in _AE_DB_DEFAULTS if x])))
            db  = tk.StringVar(value=_AE_DB_DEFAULTS[i] if i < len(_AE_DB_DEFAULTS) else "")
            vw  = tk.BooleanVar(value=(i == 0))
            cnt = tk.StringVar(value="\u2013")
            tk.Checkbutton(r, variable=en,
                bg=DARK_BG, selectcolor="#1f6feb", activebackground=DARK_BG,
                width=2).pack(side="left")
            ttk.Entry(r, textvariable=db, width=9,
                      font=("Consolas",9)).pack(side="left", padx=3)
            tk.Checkbutton(r, variable=vw,
                bg=DARK_BG, selectcolor="#1f6feb", activebackground=DARK_BG,
                width=3, command=lambda idx=i: self._ae_viewer_select(idx)
                ).pack(side="left")
            tk.Label(r, textvariable=cnt, bg=DARK_BG, fg=CIAN_CLR,
                     font=("Consolas",8), width=14).pack(side="left", padx=3)
            self._ae_db_rows.append({"en":en,"db":db,"viewer":vw,"count":cnt,
                                      "ok":0,"rej":0})

        # ── SQLite destinazione ───────────────────────────────────
        sql_lf = ttk.LabelFrame(left, text="  SQLite  ", padding=4)
        sql_lf.pack(fill="x", padx=6, pady=2)
        sql_r0 = ttk.Frame(sql_lf); sql_r0.pack(fill="x", pady=1)
        self._pv_autoexp_save_file = tk.BooleanVar(value=False)
        tk.Checkbutton(sql_r0, text="\u2713 Salva file .db",
            variable=self._pv_autoexp_save_file,
            bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
            activebackground=DARK_BG, font=("Consolas",9)).pack(side="left")
        sql_r1 = ttk.Frame(sql_lf); sql_r1.pack(fill="x", pady=1)
        self._pv_autoexp_save_sql = tk.BooleanVar(value=True)
        tk.Checkbutton(sql_r1, text="\U0001f5c4 Salva su SQLite",
            variable=self._pv_autoexp_save_sql,
            bg=DARK_BG, fg=TEXT_CLR, selectcolor="#1f6feb",
            activebackground=DARK_BG, font=("Consolas",9),
            command=self._autoexp_sql_toggle).pack(side="left")
        sql_r2 = ttk.Frame(sql_lf); sql_r2.pack(fill="x", pady=1)
        ttk.Label(sql_r2, text="File:", style="Muted.TLabel", width=5).pack(side="left")
        self._pv_autoexp_sql_path = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "weld_archive.sqlite"))
        self._ae_sql_entry = ttk.Entry(sql_r2, textvariable=self._pv_autoexp_sql_path,
                                        width=18, state="normal")
        self._ae_sql_entry.pack(side="left", fill="x", expand=True, padx=2)
        self._ae_sql_btn = ttk.Button(sql_r2, text="\U0001f4c1", width=3,
                   command=self._autoexp_browse_sql, state="normal")
        self._ae_sql_btn.pack(side="left")
        self._pv_autoexp_sql_status = tk.StringVar(value="")
        tk.Label(sql_lf, textvariable=self._pv_autoexp_sql_status,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(anchor="w")
        self._autoexp_sql_con = None

        # ── Pulsanti avvio/stop ───────────────────────────────────
        ae_r4 = ttk.Frame(left); ae_r4.pack(fill="x", padx=6, pady=(4,2))
        self._btn_autoexp_start = tk.Button(ae_r4, text="\u25b6  Avvia Monitoraggio",
            bg=OK_CLR, fg=DARK_BG, font=("Consolas",9,"bold"),
            command=self._autoexp_start)
        self._btn_autoexp_start.pack(side="left", padx=(0,4))
        self._btn_autoexp_stop = tk.Button(ae_r4, text="\u25a0  Stop",
            bg=WELD_CLR, fg=DARK_BG, font=("Consolas",9,"bold"),
            state="disabled", command=self._autoexp_stop)
        self._btn_autoexp_stop.pack(side="left")
        self._pv_autoexp_status = tk.StringVar(value="Fermo")
        tk.Label(ae_r4, textvariable=self._pv_autoexp_status,
            bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",9)).pack(side="right", padx=4)

        ae_r5 = ttk.Frame(left); ae_r5.pack(fill="x", padx=6, pady=1)
        self._pv_autoexp_count = tk.StringVar(value="\u2713 0  \u2717 0")
        tk.Label(ae_r5, textvariable=self._pv_autoexp_count,
            bg=DARK_BG, fg=PLC_CLR, font=("Consolas",9,"bold")).pack(side="left")
        self._pv_autoexp_last = tk.StringVar(value="")
        tk.Label(ae_r5, textvariable=self._pv_autoexp_last,
            bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="right")

        self._autoexp_running       = False
        self._autoexp_timer_id      = None
        self._autoexp_client        = None
        self._autoexp_prev_triggers = {}
        self._autoexp_export_count  = 0
        self._autoexp_reject_count  = 0

        # Log
        log_lf2 = ttk.LabelFrame(right, text="  Log Auto Export  ", padding=4)
        log_lf2.pack(fill="both", expand=True, padx=6, pady=6)
        sb2 = ttk.Scrollbar(log_lf2); sb2.pack(side="right", fill="y")
        self._ae_log = tk.Text(log_lf2, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas",9),
                               wrap="word", yscrollcommand=sb2.set,
                               insertbackground=TEXT_CLR, selectbackground=ACCENT)
        self._ae_log.pack(fill="both", expand=True)
        sb2.config(command=self._ae_log.yview)
        self._ae_log.tag_config("ok",   foreground=OK_CLR)
        self._ae_log.tag_config("err",  foreground=WELD_CLR)
        self._ae_log.tag_config("info", foreground=ACCENT)
        self._ae_log.tag_config("warn", foreground=WARN_CLR)
        self._ae_log.insert("end", "=== Auto Export Log ===\n", "info")
        self._ae_log.insert("end", "Avvia il monitoraggio per vedere l'attivita.\n")
    def _build_plc_realtime(self, parent):
        """Sub-tab Monitor Real-Time: polling continuo variabili scalari."""
        RT_CLR = "#4fc3f7"
        self._rt_running    = False
        self._rt_timer_id   = None
        self._rt_client     = None
        self._rt_poll_count = 0
        self._rt_buf_data   = []

        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left  = ttk.Frame(pane, width=400);  pane.add(left,  weight=0)
        right = ttk.Frame(pane);             pane.add(right, weight=1)

        left.pack_propagate(False)
        lcanv = tk.Canvas(left, bg=DARK_BG, highlightthickness=0)
        lscrl = ttk.Scrollbar(left, orient="vertical", command=lcanv.yview)
        lfrm  = ttk.Frame(lcanv)
        lfrm.bind("<Configure>", lambda e: lcanv.configure(scrollregion=lcanv.bbox("all")))
        lwin = lcanv.create_window((0,0), window=lfrm, anchor="nw")
        lcanv.configure(yscrollcommand=lscrl.set)
        lcanv.bind("<Configure>", lambda e: lcanv.itemconfigure(lwin, width=e.width))
        lcanv.bind("<MouseWheel>", lambda e: lcanv.yview_scroll(int(-1*(e.delta/120)), "units"))
        lscrl.pack(side="right", fill="y")
        lcanv.pack(side="left", fill="both", expand=True)

        # Connessione condivisa
        frm_conn = ttk.LabelFrame(lfrm, text="  Connessione (condivisa)  ", padding=6)
        frm_conn.pack(fill="x", padx=4, pady=(6,2))
        rc1 = ttk.Frame(frm_conn);  rc1.pack(fill="x", pady=1)
        ttk.Label(rc1, text="IP:", width=4).pack(side="left")
        ttk.Entry(rc1, textvariable=self._pv_plc_ip, width=16).pack(side="left", padx=2)
        ttk.Label(rc1, text="DB:", width=3).pack(side="left")
        ttk.Entry(rc1, textvariable=self._pv_plc_db, width=7).pack(side="left", padx=2)
        rc2 = ttk.Frame(frm_conn);  rc2.pack(fill="x", pady=1)
        ttk.Label(rc2, text="Rack:", width=5).pack(side="left")
        ttk.Entry(rc2, textvariable=self._pv_plc_rack, width=4).pack(side="left", padx=2)
        ttk.Label(rc2, text="Slot:", width=4).pack(side="left")
        ttk.Entry(rc2, textvariable=self._pv_plc_slot, width=4).pack(side="left", padx=2)

        # Polling
        frm_poll = ttk.LabelFrame(lfrm, text="  Polling  ", padding=6)
        frm_poll.pack(fill="x", padx=4, pady=2)
        rp1 = ttk.Frame(frm_poll);  rp1.pack(fill="x", pady=1)
        ttk.Label(rp1, text="Intervallo:", width=12, style="Muted.TLabel").pack(side="left")
        self._pv_rt_poll = tk.StringVar(value="200")
        ttk.Entry(rp1, textvariable=self._pv_rt_poll, width=6).pack(side="left", padx=2)
        ttk.Label(rp1, text="ms", style="Muted.TLabel").pack(side="left")
        rp2 = ttk.Frame(frm_poll);  rp2.pack(fill="x", pady=1)
        ttk.Label(rp2, text="Buffer grafico:", width=12, style="Muted.TLabel").pack(side="left")
        self._pv_rt_buf = tk.StringVar(value="150")
        ttk.Entry(rp2, textvariable=self._pv_rt_buf, width=6).pack(side="left", padx=2)
        ttk.Label(rp2, text="campioni", style="Muted.TLabel").pack(side="left")

        # Variabile grafico
        frm_trace = ttk.LabelFrame(lfrm, text="  Traccia grafico  ", padding=6)
        frm_trace.pack(fill="x", padx=4, pady=2)
        self._pv_rt_trace = tk.StringVar(value="I_LaserValue")
        ttk.Combobox(frm_trace, textvariable=self._pv_rt_trace, state="readonly", width=18,
                     values=["I_LaserValue","rPeakDeviation","rBaselineSigma",
                             "iSamplesAcquired","iClustersValid","iConsecutiveCount"]
                     ).pack(fill="x")

        # Pulsanti
        btn_f = ttk.Frame(lfrm);  btn_f.pack(fill="x", padx=4, pady=6)
        self._btn_rt_start = tk.Button(btn_f, text="\u25b6  Avvia Monitor",
            bg=RT_CLR, fg=DARK_BG, font=("Consolas", 9, "bold"),
            command=self._rt_start)
        self._btn_rt_start.pack(fill="x", pady=(0,3))
        self._btn_rt_stop = tk.Button(btn_f, text="\u25a0  Stop",
            bg=WELD_CLR, fg=DARK_BG, font=("Consolas", 9, "bold"),
            state="disabled", command=self._rt_stop)
        self._btn_rt_stop.pack(fill="x")
        self._pv_rt_status = tk.StringVar(value="Fermo")
        tk.Label(btn_f, textvariable=self._pv_rt_status,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 8)).pack(pady=(3,0))

        # ── DESTRA ──
        top_bar = ttk.Frame(right)
        top_bar.pack(fill="x", padx=6, pady=(6,2))
        self._pv_rt_badge = tk.StringVar(value="\u2b24  Disconnesso")
        self._rt_badge_lbl = tk.Label(top_bar, textvariable=self._pv_rt_badge,
                 bg=PANEL_BG, fg=MUTED_CLR,
                 font=("Consolas", 10, "bold"),
                 relief="flat", padx=10, pady=4)
        self._rt_badge_lbl.pack(side="left")
        self._pv_rt_count = tk.StringVar(value="Letture: 0")
        tk.Label(top_bar, textvariable=self._pv_rt_count,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 9)).pack(side="right")

        # Griglia card valori (3 colonne)
        grid_frm = ttk.Frame(right)
        grid_frm.pack(fill="x", padx=6, pady=2)

        RT_VARS = [
            ("I_LaserValue",     "LaserValue",     "mm",  "#58a6ff"),
            ("I_CurrentAngle",   "CurrentAngle",   "\u00b0",   "#bc8cff"),
            ("iState",           "iState",         "",    "#f0883e"),
            ("iSamplesAcquired", "Samples",        "",    "#f0883e"),
            ("IO_RicercaSaldatura.Trovata", "Trovata", "", "#3fb950"),
            ("rPeakValue",       "PeakValue",      "mm",  "#f0883e"),
            ("rPeakDeviation",   "PeakDeviation",  "mm",  "#f0883e"),
            ("rDetectedAtAngle", "DetectedAngle",  "\u00b0",  "#3fb950"),
            ("rBaselineMean",    "BaselineMean",   "mm",  "#58a6ff"),
            ("rBaselineSigma",   "BaselineSigma",  "mm",  "#58a6ff"),
            ("iClustersValid",   "ClustersValid",  "",    "#f9c74f"),
            ("iConsecutiveCount","ConsecCount",    "",    "#f9c74f"),
            ("O_Done",           "O_Done",         "",    "#3fb950"),
            ("O_Error",          "O_Error",        "",    "#cc2222"),
            ("O_Busy",           "O_Busy",         "",    "#d29922"),
        ]

        self._rt_vars = {}
        COLS = 3
        for idx, (key, lbl, unit, clr) in enumerate(RT_VARS):
            row = idx // COLS;  col = idx % COLS
            card = tk.Frame(grid_frm, bg=PANEL_BG, relief="flat", bd=0,
                            highlightthickness=1, highlightbackground=BORDER_CLR)
            card.grid(row=row, column=col, padx=3, pady=2, sticky="nsew")
            grid_frm.columnconfigure(col, weight=1)
            tk.Label(card, text=lbl, bg=PANEL_BG, fg=MUTED_CLR,
                     font=("Consolas", 8)).pack(anchor="w", padx=5, pady=(3,0))
            sv = tk.StringVar(value="\u2014")
            self._rt_vars[key] = sv
            tk.Label(card, textvariable=sv, bg=PANEL_BG, fg=clr,
                     font=("Consolas", 12, "bold")).pack(anchor="w", padx=5)
            if unit:
                tk.Label(card, text=unit, bg=PANEL_BG, fg=MUTED_CLR,
                         font=("Consolas", 7)).pack(anchor="e", padx=5, pady=(0,2))

        # Grafico live
        self._rt_fig = Figure(facecolor=DARK_BG, figsize=(5, 1.8))
        self._rt_ax  = self._rt_fig.add_subplot(111)
        self._style_axes(self._rt_ax, "Monitor Real-Time", "", "")
        self._rt_fig.tight_layout(pad=1.5)
        rt_c = FigureCanvasTkAgg(self._rt_fig, right)
        rt_c.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=(2,4))
        self._rt_canvas = rt_c

        # Status bar
        sb_frm = tk.Frame(right, bg=PANEL_BG)
        sb_frm.pack(fill="x", padx=6, pady=(0,4))
        self._pv_rt_sb = tk.StringVar(value="Fermo  --  premi Avvia per iniziare")
        tk.Label(sb_frm, textvariable=self._pv_rt_sb,
                 bg=PANEL_BG, fg=MUTED_CLR, font=("Consolas", 8),
                 anchor="w").pack(fill="x", padx=4, pady=2)

    # ── PLC: utilità log ──────────────────────────────────────
    def _plc_log_msg(self, msg, tag=None):
        """Inserisce un messaggio nel log DB Reader e (se presente) nel log Auto Export."""
        t = tag if tag else ()
        self._plc_log.insert("end", msg, t)
        self._plc_log.see("end")
        # Scrive anche nel log Auto Export se già creato
        try:
            self._ae_log.insert("end", msg, t)
            self._ae_log.see("end")
        except AttributeError:
            pass
        # update_idletasks rimosso: chiamarlo per ogni riga causa jank visibile.
        # Lo UI si aggiorna naturalmente tra i cicli Tk.

    def _plc_log_clear(self):
        self._plc_log.delete("1.0", "end")

    def _plc_set_result(self, key, val):
        if key in self._plc_result_vars:
            self._plc_result_vars[key].set(str(val))

    def _plc_dbname(self):
        """Genera il nome DB includendo il numero DB."""
        db_num = self._pv_plc_db.get()
        return f"WeldFind_DB{db_num}"

    def _plc_connect_and_read(self):
        """Connette al PLC, legge il DB, decodifica. Restituisce True se OK."""
        self._plc_log_clear()
        self._plc_decoded = None
        ip = self._pv_plc_ip.get()
        rack = int(self._pv_plc_rack.get())
        slot = int(self._pv_plc_slot.get())
        db_num = int(self._pv_plc_db.get())

        self._pv_plc_status.set("Connessione...")
        self._plc_log_msg(f"Connessione a {ip} (rack={rack}, slot={slot})...\n", "info")

        reader = PLCReader(ip, rack, slot)
        cpu, pdu = reader.connect()
        self._plc_log_msg(f"✓ Connesso — CPU: {cpu}, PDU: {pdu}\n", "ok")

        omap, db_size, ms = plc_build_offset_map(6, 2001)
        db_size = plc_resolve_db_size(reader.client, db_num, db_size)
        self._plc_log_msg(f"Lettura DB{db_num}: {db_size} byte ({db_size/1024:.1f} KB)...\n", "info")

        self._pv_plc_status.set("Lettura...")
        def progress(pct):
            self._pv_plc_status.set(f"Lettura... {pct}%")
            self.update_idletasks()

        t0 = time.time()
        raw = reader.read_db_raw(db_num, db_size, callback=progress)
        elapsed = time.time() - t0
        reader.disconnect()

        self._plc_log_msg(f"✓ Letto in {elapsed:.1f}s\n", "ok")

        decoded = plc_decode_db(raw, omap, ms)
        self._plc_decoded = decoded
        self._plc_decoded_max_samples = ms

        # Mostra risultati
        sc = decoded['scalars']
        n_samp = int(sc.get('iSamplesAcquired', 0))
        self._plc_set_result("Stato:",      "✓ Letto OK")
        self._plc_set_result("Campioni:",   str(n_samp))
        self._plc_set_result("LaserValue:", f"{sc.get('I_LaserValue', 0):.4f}")
        self._plc_set_result("Angle:",      f"{sc.get('I_CurrentAngle', 0):.3f}")
        self._plc_set_result("PeakValue:",  f"{sc.get('rPeakValue', 0):.4f}")
        self._plc_set_result("PeakDev:",    f"{sc.get('rPeakDeviation', 0):.4f}")
        pol = int(sc.get('I_PeakPolarity', 0))
        pol_name = {0: "Positivo", 1: "Negativo", 2: "Entrambi"}.get(pol, str(pol))
        self._plc_set_result("Polarity:",   pol_name)
        self._plc_set_result("DB Size:",    f"{db_size/1024:.1f} KB")

        self._plc_log_msg(f"\n  Campioni: {n_samp}\n")
        self._plc_log_msg(f"  Polarità: {pol_name}\n")
        self._plc_log_msg(f"  Peak: {sc.get('rPeakValue', 0):.4f} (dev: {sc.get('rPeakDeviation', 0):.4f})\n")
        det = int(sc.get('iDetectedAtSample', 0))
        ang = sc.get('rDetectedAtAngle', 0)
        self._plc_log_msg(f"  Detection @ sample {det}, angolo {ang:.3f}°\n")

        self._pv_plc_status.set(f"DB{db_num} — {n_samp} campioni")
        return True

    def _plc_test_connection(self):
        self._plc_log_clear()
        self._pv_plc_status.set("Test connessione...")
        ip = self._pv_plc_ip.get()
        rack = int(self._pv_plc_rack.get())
        slot = int(self._pv_plc_slot.get())
        self._plc_log_msg(f"Connessione a {ip} (rack={rack}, slot={slot})...\n", "info")
        try:
            reader = PLCReader(ip, rack, slot)
            cpu, pdu = reader.connect()
            self._plc_log_msg(f"✓ Connesso! CPU: {cpu}, PDU: {pdu}\n", "ok")
            reader.disconnect()
            self._plc_log_msg("✓ Disconnesso.\n", "ok")
            self._pv_plc_status.set("Connessione OK!")
            self._plc_set_result("Stato:", "✓ Connesso")
        except Exception as e:
            self._plc_log_msg(f"✗ ERRORE: {e}\n", "err")
            self._pv_plc_status.set("Connessione fallita!")
            self._plc_set_result("Stato:", "✗ Errore")

    def _plc_read_db(self):
        try:
            self._plc_connect_and_read()
            self._plc_log_msg("\n✓ Dati pronti.\n", "ok")
        except Exception as e:
            self._plc_log_msg(f"\n✗ ERRORE: {e}\n", "err")
            self._pv_plc_status.set("Errore!")
            self._plc_set_result("Stato:", "✗ Errore")

    def _plc_auto_save(self):
        """Connetti + Leggi + Salva file .db in un'unica azione."""
        try:
            self._plc_connect_and_read()
        except Exception as e:
            self._plc_log_msg(f"\n✗ ERRORE: {e}\n", "err")
            self._pv_plc_status.set("Errore!");  return

        db_name = self._plc_dbname()
        db_text = plc_generate_db_text(self._plc_decoded, db_name,
                                       self._plc_decoded_max_samples)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Salva file .db",
            defaultextension=".db",
            filetypes=[("DB files", "*.db"), ("Tutti i file", "*.*")],
            initialfile=f"{db_name}_{ts}.db"
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(db_text)
            self._plc_log_msg(f"\n✓ Salvato: {path}\n", "ok")
            self._pv_plc_status.set(f"Salvato: {os.path.basename(path)}")
        else:
            self._pv_plc_status.set("Salvataggio annullato")

    def _plc_auto_viewer(self):
        """Connetti + Leggi + Carica nel Viewer (non bloccante)."""
        def _read():
            self._plc_connect_and_read()
            db_name = self._plc_dbname()
            db_text = plc_generate_db_text(self._plc_decoded, db_name,
                                           self._plc_decoded_max_samples)
            ts    = datetime.datetime.now().strftime("%H:%M:%S")
            fname = f"PLC_{db_name}_{ts}.db"
            return parse_db_file_from_text(db_text, fname), fname

        def _done(res):
            data, fname = res
            self.db_data = data
            self.lbl_file.config(text=f"🔌 {fname}")
            self._plc_log_msg(f"\n✓ Caricato nel viewer: {fname}\n", "ok")
            self._pv_plc_status.set(f"Viewer: {fname}")
            self._update_results_panel(); self._preload_sim_params()
            self._recompute();            self._update_raw_tab()
            self.nb.select(0); self._sub_nb.select(0)

        def _err(e):
            self._plc_log_msg(f"\n✗ ERRORE: {e}\n", "err")
            self._pv_plc_status.set("Errore!")

        self._run_in_thread(_read, on_done=_done, on_error=_err,
                            status_var=self._pv_plc_status, status_msg="Lettura PLC...")

    # ══════════════════════════════════════════════════════════════
    #  AUTO EXPORT — Monitoraggio trigger via Snap7 GET/PUT
    # ══════════════════════════════════════════════════════════════

    def _autoexp_sql_toggle(self):
        s = "normal" if self._pv_autoexp_save_sql.get() else "disabled"
        self._ae_sql_entry.config(state=s); self._ae_sql_btn.config(state=s)

    def _autoexp_browse_sql(self):
        p = filedialog.asksaveasfilename(parent=self, title="File SQLite Auto Export",
            defaultextension=".sqlite",
            filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")],
            initialfile="weld_archive.sqlite")
        if p: self._pv_autoexp_sql_path.set(p)

    def _autoexp_sql_open(self) -> bool:
        path = self._pv_autoexp_sql_path.get().strip()
        if not path: return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception: pass
        try:
            self._autoexp_sql_con = weld_sqlite_init(path)
            self._pv_autoexp_sql_status.set(f"✓ {os.path.basename(path)}")
            return True
        except Exception as e:
            self._autoexp_sql_con = None
            self._pv_autoexp_sql_status.set(f"✗ {e}")
            return False

    def _autoexp_sql_close(self):
        if self._autoexp_sql_con:
            try: self._autoexp_sql_con.close()
            except Exception: pass
            self._autoexp_sql_con = None
        self._pv_autoexp_sql_status.set("")


    def _autoexp_browse_path(self):
        d = filedialog.askdirectory(parent=self, title="Cartella Buoni (saldatura trovata)",
                                     initialdir=self._pv_autoexp_path.get())
        if d:
            self._pv_autoexp_path.set(d)

    def _autoexp_browse_path_rej(self):
        d = filedialog.askdirectory(parent=self, title="Cartella Scarti (saldatura non trovata)",
                                     initialdir=self._pv_autoexp_path_rej.get())
        if d:
            self._pv_autoexp_path_rej.set(d)

    def _ae_viewer_select(self, idx):
        """Garantisce che al massimo un DB abbia il flag viewer attivo."""
        for i, row in enumerate(self._ae_db_rows):
            if i != idx:
                row["viewer"].set(False)

    def _ae_viewer_select(self, selected_idx):
        """Radio-like: assicura che un solo DB abbia viewer=True."""
        for i, row in enumerate(self._ae_db_rows):
            if i != selected_idx:
                row["viewer"].set(False)

    def _autoexp_start(self):
        """Avvia monitoraggio multi-DB (fino a 10 DB simultanei)."""
        if self._autoexp_running:
            return
        if not SNAP7_AVAILABLE:
            self._plc_log_msg("\n\u2717 python-snap7 non installato!\n", "err"); return
        if not self._pv_autoexp_on.get():
            self._plc_log_msg("\n\u26a0 Abilita il flag Auto Export prima di avviare.\n", "warn"); return

        ip   = self._pv_plc_ip.get().strip()
        rack = int(self._pv_plc_rack.get())
        slot = int(self._pv_plc_slot.get())

        # Cartelle output
        export_dir = self._pv_autoexp_path.get().strip()
        if not export_dir:
            self._plc_log_msg("\u2717 Specificare una cartella Buoni!\n", "err"); return
        reject_dir = self._pv_autoexp_path_rej.get().strip() or os.path.join(export_dir, "Scarti")
        self._pv_autoexp_path_rej.set(reject_dir)
        os.makedirs(export_dir, exist_ok=True)
        os.makedirs(reject_dir, exist_ok=True)

        # Determina trigger
        trigger_name = self._pv_autoexp_trigger.get()
        if "bStart_Prev" in trigger_name: trigger_var = "bStart_Prev"
        elif "O_Done"     in trigger_name: trigger_var = "O_Done"
        else:                              trigger_var = "iState"

        # Offset map comune (tutti i DB hanno la stessa struttura)
        omap, db_size, ms = plc_build_offset_map(6, 2001)
        trigger_info = next((x for x in omap if x[0] == trigger_var), None)
        if not trigger_info:
            self._plc_log_msg(f"\u2717 Trigger '{trigger_var}' non trovato!\n", "err"); return

        t_name, t_off, t_dtype, t_sz = trigger_info
        poll_start = max(0, t_off - 50)
        poll_size  = min(200, db_size - poll_start)
        trig_rel   = t_off - poll_start

        # Raccoglie DB abilitati
        active_rows = [(i, row) for i, row in enumerate(self._ae_db_rows)
                       if row["en"].get() and row["db"].get().strip().isdigit()]
        if not active_rows:
            self._plc_log_msg("\u2717 Nessun DB abilitato con numero valido!\n", "err"); return

        # Connessione unica
        self._plc_log_msg(f"\n{'\u2500'*40}\n", "info")
        self._plc_log_msg(f"Connessione a {ip}...\n", "info")
        try:
            self._autoexp_client = snap7.client.Client()
            self._autoexp_client.connect(ip, rack, slot)
            if not self._autoexp_client.get_connected():
                raise ConnectionError("Connessione fallita")
        except Exception as e:
            self._plc_log_msg(f"\u2717 {e}\n", "err")
            self._autoexp_client = None; return
        self._plc_log_msg("\u2713 Connesso!\n", "ok")

        # Verifica e prepara info per ogni DB
        self._ae_active_dbs = []
        for i, row in active_rows:
            db_num = int(row["db"].get().strip())
            try:
                db_size = plc_resolve_db_size(self._autoexp_client, db_num, db_size)
                test_raw = self._autoexp_client.db_read(db_num, poll_start, poll_size)
                t_dtype_r = t_dtype; t_sz_r = t_sz
                if t_dtype == 'bool': test_val = bool(test_raw[trig_rel] & (1 << t_sz))
                elif t_dtype == 'int': test_val = struct.unpack('>h', test_raw[trig_rel:trig_rel+2])[0]
                else: test_val = struct.unpack('>f', test_raw[trig_rel:trig_rel+4])[0]
                self._plc_log_msg(f"  DB{db_num}: \u2713 {trigger_var}={test_val}\n", "ok")
                self._ae_active_dbs.append({
                    "idx": i, "row": row, "db_num": db_num,
                    "omap": omap, "db_size": db_size, "ms": ms,
                    "poll_start": poll_start, "poll_size": poll_size,
                    "trig_rel": trig_rel, "trigger_info": trigger_info,
                    "trigger_var": trigger_var, "prev_trigger": test_val,
                })
                row["count"].set(f"\u2713 ready")
            except Exception as e:
                self._plc_log_msg(f"  DB{db_num}: \u2717 {e}\n", "err")
                row["count"].set(f"\u2717 err")

        if not self._ae_active_dbs:
            self._plc_log_msg("\u2717 Nessun DB raggiungibile!\n", "err")
            try: self._autoexp_client.disconnect()
            except Exception: pass
            self._autoexp_client = None; return

        # Stato comune
        self._autoexp_omap        = omap
        self._autoexp_db_size     = db_size
        self._autoexp_max_samples = ms
        self._autoexp_trigger_var = trigger_var
        self._autoexp_trigger_info = trigger_info
        self._autoexp_poll_start  = poll_start
        self._autoexp_poll_size   = poll_size
        self._autoexp_poll_trigger_rel = trig_rel

        self._autoexp_export_count = 0
        self._autoexp_reject_count = 0
        self._autoexp_error_count  = 0
        self._autoexp_poll_ms = max(50, int(self._pv_autoexp_poll.get() or 100))
        self._pv_autoexp_count.set("\u2713 0  \u2717 0")
        self._pv_autoexp_last.set("")

        if self._pv_autoexp_save_sql.get():
            if not self._autoexp_sql_open():
                if not messagebox.askyesno("SQLite", "Errore SQLite.\nContinuare solo con file .db?"):
                    return

        self._ae_prev_enabled = {}  # reset tracciamento stato DB
        self._autoexp_running = True
        self._btn_autoexp_start.config(state="disabled")
        self._btn_autoexp_stop.config(state="normal")
        n_db = len(self._ae_active_dbs)
        self._pv_autoexp_status.set(f"\u25cf Attivo — {n_db} DB")
        self._plc_log_msg(f"\u25b6 Monitoraggio avviato su {n_db} DB\n", "ok")
        self._update_conn_indicator("autoexp")
        self.app_log(f"Auto Export avviato: {n_db} DB, trigger={trigger_var}", "ok")
        self._autoexp_poll()

    def _autoexp_stop(self):
        """Ferma il monitoraggio e disconnetti."""
        self._autoexp_running = False
        if getattr(self, "_autoexp_timer_id", None):
            self.after_cancel(self._autoexp_timer_id)
            self._autoexp_timer_id = None
        if getattr(self, "_autoexp_client", None):
            try:
                self._autoexp_client.disconnect()
            except Exception:
                pass
            self._autoexp_client = None
        self._ae_active_dbs = []
        try:
            self._btn_autoexp_start.config(state="normal")
            self._btn_autoexp_stop.config(state="disabled")
            self._pv_autoexp_status.set("Fermo")
        except Exception:
            pass
        n = getattr(self, "_autoexp_export_count", 0)
        r = getattr(self, "_autoexp_reject_count", 0)
        self._plc_log_msg(f"■ Auto Export fermato. Buoni: {n} | Scarti: {r}\n\n", "warn")
        try: self._autoexp_sql_close()
        except Exception: pass
        self._update_conn_indicator(None)
        self.app_log(f"Auto Export fermato. Buoni: {n} | Scarti: {r}", "warn")


    def _autoexp_decode_trigger_rel(self, raw_chunk):
        """Decodifica valore trigger dal chunk letto a poll_start."""
        rel = self._autoexp_poll_trigger_rel
        _, _, dtype, sz = self._autoexp_trigger_info
        if dtype == 'bool':  return bool(raw_chunk[rel] & (1 << sz))
        elif dtype == 'int': return struct.unpack('>h', raw_chunk[rel:rel+2])[0]
        else:                return struct.unpack('>f', raw_chunk[rel:rel+4])[0]

    def _autoexp_poll(self):
        """Ciclo di polling multi-DB: controlla trigger per ogni DB attivo.
        La selezione dei DB viene riletta ad ogni ciclo (real-time).
        """
        if not self._autoexp_running or not self._autoexp_client:
            return
        try:
            ps = self._autoexp_poll_start
            pz = self._autoexp_poll_size
            self._autoexp_error_count = 0

            # *** real-time *** Aggiunge DB abilitati dopo l'avvio
            _known = {d["db_num"] for d in self._ae_active_dbs}
            for _row in self._ae_db_rows:
                if not _row["en"].get(): continue
                _s = _row["db"].get().strip()
                if not _s.isdigit(): continue
                _n = int(_s)
                if _n in _known: continue
                try:
                    _sz  = plc_resolve_db_size(self._autoexp_client, _n,
                                              self._autoexp_db_size)
                    _raw = self._autoexp_client.db_read(_n, ps, pz)
                    _tv  = self._autoexp_decode_trigger_rel(_raw)
                    self._ae_active_dbs.append({
                        "idx": self._ae_db_rows.index(_row),
                        "row": _row, "db_num": _n,
                        "omap": self._autoexp_omap,
                        "db_size": _sz, "ms": self._autoexp_max_samples,
                        "poll_start": ps, "poll_size": pz,
                        "trig_rel": self._autoexp_poll_trigger_rel,
                        "trigger_info": self._autoexp_trigger_info,
                        "trigger_var": self._autoexp_trigger_var,
                        "prev_trigger": _tv,
                    })
                    _known.add(_n)
                    _row["count"].set("\u2713 ready")
                    self._plc_log_msg(f"+ DB{_n} aggiunto\n", "ok")
                except Exception:
                    pass  # riprova al prossimo ciclo

            # *** real-time *** Itera solo DB con checkbox attiva; logga i cambi di stato
            if not hasattr(self, '_ae_prev_enabled'):
                self._ae_prev_enabled = {}
            for db_info in self._ae_active_dbs:
                db_num   = db_info["db_num"]
                _enabled = db_info["row"]["en"].get()
                _was     = self._ae_prev_enabled.get(db_num, True)
                if _enabled != _was:
                    self._ae_prev_enabled[db_num] = _enabled
                    if _enabled:
                        self._plc_log_msg(f"\u25b6 DB{db_num} riattivato\n", "ok")
                    else:
                        self._plc_log_msg(f"\u23f8 DB{db_num} sospeso\n", "warn")
                if not _enabled:
                    continue
                try:
                    raw = self._autoexp_client.db_read(db_num, ps, pz)
                    current_val = self._autoexp_decode_trigger_rel(raw)
                    prev        = db_info["prev_trigger"]
                    tvar        = db_info["trigger_var"]

                    if prev is not None:
                        if "bStart_Prev" in tvar:
                            triggered = bool(prev) and (not bool(current_val))
                        elif "O_Done" in tvar:
                            triggered = (not prev) and bool(current_val)
                        else:
                            triggered = (prev != 2) and (current_val == 2)
                    else:
                        triggered = False

                    db_info["prev_trigger"] = current_val
                    if triggered:
                        self._autoexp_on_trigger_db(db_info)

                except Exception as e:
                    self._plc_log_msg(f"\u2717 Errore poll DB{db_num}: {e}\n", "err")

            n_db = sum(1 for d in self._ae_active_dbs if d["row"]["en"].get())
            self._pv_autoexp_status.set(
                f"\u25cf Attivo — {n_db} DB  "
                f"\u2713{self._autoexp_export_count} \u2717{self._autoexp_reject_count}")

        except Exception as e:
            self._autoexp_error_count = getattr(self, '_autoexp_error_count', 0) + 1
            if self._autoexp_error_count <= 3 or self._autoexp_error_count % 20 == 0:
                self._plc_log_msg(f"\u2717 Errore polling #{self._autoexp_error_count}: {e}\n", "err")
            if self._autoexp_error_count >= 50:
                self._plc_log_msg("\u2717 Troppi errori, monitoraggio fermato.\n", "err")
                self._autoexp_stop(); return

        if self._autoexp_running:
            self._autoexp_timer_id = self.after(self._autoexp_poll_ms, self._autoexp_poll)

    def _autoexp_on_trigger_db(self, db_info):
        """Trigger scattato su un DB specifico: leggi, salva, aggiorna viewer."""
        row    = db_info["row"]
        db_num = db_info["db_num"]
        omap   = db_info["omap"]
        db_size= db_info["db_size"]
        ms     = db_info["ms"]
        self._plc_log_msg(f"\u2605 TRIGGER DB{db_num}! ", "ok")
        self._pv_autoexp_status.set(f"\u25cf Lettura DB{db_num}...")
        try:
            raw = bytearray(db_size)
            off = 0; chunk = 400
            while off < db_size:
                sz = min(chunk, db_size - off)
                raw[off:off+sz] = self._autoexp_client.db_read(db_num, off, sz)
                off += sz
            decoded = plc_decode_db(raw, omap, ms)
            sc = decoded['scalars']
            n_samp = int(sc.get('iSamplesAcquired', 0))

            clusters_valid    = int(sc.get('iClustersValid', 0))
            consecutive_count = int(sc.get('iConsecutiveCount', 0))
            min_consecutive   = int(sc.get('I_MinConsecutive', 1))
            weld_found = (clusters_valid >= 1 and consecutive_count >= min_consecutive)

            if weld_found:
                row["ok"] = row.get("ok", 0) + 1
                self._autoexp_export_count += 1
                seq = self._autoexp_export_count
                dest_dir = self._pv_autoexp_path.get().strip()
                prefix = "OK"
            else:
                row["rej"] = row.get("rej", 0) + 1
                self._autoexp_reject_count += 1
                seq = self._autoexp_reject_count
                dest_dir = self._pv_autoexp_path_rej.get().strip()
                prefix = "SCARTO"

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            db_name  = f"WeldFind_DB{db_num}"
            filename = f"{db_name}_{prefix}_{ts}_{seq:04d}.db"
            filepath = os.path.join(dest_dir, filename)

            db_text = plc_generate_db_text(decoded, db_name, ms)

            if self._pv_autoexp_save_file.get():
                os.makedirs(dest_dir, exist_ok=True)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(db_text)
            if self._pv_autoexp_save_sql.get() and self._autoexp_sql_con:
                try:
                    decoded["timestamp"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    rid = weld_sqlite_insert(self._autoexp_sql_con, db_num, decoded, filename, ms)
                    self._plc_log_msg(f"  \u2502 SQLite row #{rid}\n")
                except Exception as sql_e:
                    self._plc_log_msg(f"  \u2502 SQLite err: {sql_e}\n", "warn")

            ok_n  = row.get("ok", 0)
            rej_n = row.get("rej", 0)
            row["count"].set(f"\u2713{ok_n} \u2717{rej_n}")

            peak = sc.get('rPeakValue', 0); dev = sc.get('rPeakDeviation', 0)
            det_angle = sc.get('rDetectedAtAngle', 0)
            if weld_found:
                self._plc_log_msg(f"\u2713 BUONO  #{ok_n}: {filename}\n", "ok")
            else:
                self._plc_log_msg(f"\u2717 SCARTO #{rej_n}: {filename}\n", "warn")
            self._plc_log_msg(
                f"  {n_samp} camp. | clV={clusters_valid} cons={consecutive_count}/{min_consecutive}"
                f" | peak={peak:.3f} ang={det_angle:.1f}\u00b0\n")

            self._pv_autoexp_count.set(
                f"\u2713 {self._autoexp_export_count}  \u2717 {self._autoexp_reject_count}")
            self._pv_autoexp_last.set(f"{'\u2713' if weld_found else '\u2717'} DB{db_num} {ts[-6:]}")

            # Viewer: solo se questo DB ha viewer=True
            if row["viewer"].get() and not self._viewer_paused:
                data = parse_db_file_from_text(db_text, filename)
                self.db_data = data
                self.lbl_file.config(text=f"\U0001f504 {filename}")
                self._update_results_panel(); self._preload_sim_params()
                self._recompute(); self._update_raw_tab()

        except Exception as e:
            self._plc_log_msg(f"\u2717 Errore DB{db_num}: {e}\n", "err")


    # ══════════════════════════════════════════════════════════════
    #  MONITOR REAL-TIME — polling scalari via Snap7
    # ══════════════════════════════════════════════════════════════

    def _ae_log_msg(self, msg, tag=None):
        """Scrive nel log del tab Auto Export."""
        try:
            self._ae_log.insert("end", msg, tag if tag else ())
            self._ae_log.see("end");  self.update_idletasks()
        except Exception:
            pass

    def _rt_start(self):
        """Connetti al PLC e avvia il polling Real-Time."""
        if self._rt_running:
            return
        if not SNAP7_AVAILABLE:
            messagebox.showerror("Snap7 non disponibile",
                "Installare python-snap7:\n  pip install python-snap7")
            return
        ip   = self._pv_plc_ip.get().strip()
        rack = int(self._pv_plc_rack.get())
        slot = int(self._pv_plc_slot.get())
        db_num = int(self._pv_plc_db.get())

        self._pv_rt_status.set("Connessione...")
        self._pv_rt_badge.set("⬤  Connessione...")
        self._rt_badge_lbl.config(fg=WARN_CLR)
        self.update_idletasks()

        try:
            self._rt_client = snap7.client.Client()
            self._rt_client.connect(ip, rack, slot)
            if not self._rt_client.get_connected():
                raise ConnectionError("Connessione fallita")
        except Exception as e:
            messagebox.showerror("Errore connessione", str(e))
            self._rt_client = None
            self._pv_rt_status.set("Errore connessione")
            self._pv_rt_badge.set("⬤  Disconnesso")
            self._rt_badge_lbl.config(fg=MUTED_CLR)
            return

        # Costruisci offset map e calcola range scalari
        omap, db_size, ms = plc_build_offset_map(6, 2001)
        db_size = plc_resolve_db_size(self._rt_client, db_num, db_size)
        self._rt_omap    = omap
        self._rt_db_num  = db_num
        self._rt_db_size = db_size

        # Calcola il range minimo da leggere: tutti gli scalari
        # (salta gli array che sono enormi — ~32 KB — non servono per il monitor)
        scalar_offsets = [off for (name, off, dtype, sz) in omap if dtype != 'array_real']
        if scalar_offsets:
            rt_start = min(scalar_offsets)
            # Trova fine dell'ultimo scalare
            scalar_ends = []
            for name, off, dtype, sz in omap:
                if dtype == 'real':    scalar_ends.append(off + 4)
                elif dtype == 'lreal': scalar_ends.append(off + 8)
                elif dtype == 'int':   scalar_ends.append(off + 2)
                elif dtype == 'bool':  scalar_ends.append(off + 1)
            rt_end = max(scalar_ends)
            self._rt_poll_start = rt_start
            self._rt_poll_size  = min(rt_end - rt_start + 4, db_size - rt_start)
        else:
            self._rt_poll_start = 0
            self._rt_poll_size  = min(400, db_size)

        self._rt_running    = True
        self._rt_poll_count = 0
        self._rt_err_count  = 0   # inizializzato qui — niente hasattr in poll
        self._rt_buf_data   = []
        # Pre-filtro: lista (name, rel_offset, dtype, sz) solo scalari
        # calcolata una volta → _rt_poll non itera più l'intera omap ad ogni ciclo
        self._rt_scalar_entries = [
            (name, off - self._rt_poll_start, dtype, sz)
            for name, off, dtype, sz in omap
            if dtype != 'array_real'
            and (off - self._rt_poll_start) >= 0
        ]
        # Cache intervallo poll in ms — evita StringVar.get() + int() ogni ciclo
        self._rt_poll_ms = max(50, int(self._pv_rt_poll.get() or 200))
        self._btn_rt_start.config(state="disabled")
        self._btn_rt_stop.config(state="normal")
        self._pv_rt_status.set("● Monitoraggio attivo")
        self._pv_rt_badge.set("⬤  Connesso — in ascolto")
        self._rt_badge_lbl.config(fg=OK_CLR)
        self._update_conn_indicator("realtime")
        self.app_log(f"RT Monitor avviato: DB{db_num} @ {ip}", "ok")
        self._rt_poll()

    def _rt_stop(self):
        """Ferma il polling Real-Time."""
        self._rt_running = False
        if self._rt_timer_id:
            self.after_cancel(self._rt_timer_id)
            self._rt_timer_id = None
        if self._rt_client:
            try: self._rt_client.disconnect()
            except Exception: pass
            self._rt_client = None
        self._btn_rt_start.config(state="normal")
        self._btn_rt_stop.config(state="disabled")
        self._pv_rt_status.set("Fermo")
        self._pv_rt_badge.set("⬤  Disconnesso")
        self._rt_badge_lbl.config(fg=MUTED_CLR)
        self._pv_rt_sb.set(f"Fermato dopo {self._rt_poll_count} letture.")
        self._update_conn_indicator(None)
        self.app_log(f"RT Monitor fermato. Letture totali: {self._rt_poll_count}", "warn")

    def _rt_poll(self):
        """Singolo ciclo di polling: legge scalari e aggiorna la UI."""
        if not self._rt_running or not self._rt_client:
            return
        try:
            # Legge solo il blocco scalari (molto più veloce degli array)
            raw = self._rt_client.db_read(
                self._rt_db_num,
                self._rt_poll_start,
                self._rt_poll_size)

            # Decodifica scalari: usa _rt_scalar_entries pre-filtrata in _rt_start
            # (esclusi array, offset già relativi → nessun calcolo extra per ciclo)
            sc = {}
            raw_len = len(raw)
            for name, rel, dtype, sz in self._rt_scalar_entries:
                if rel + _RT_DTYPE_SIZE.get(dtype, 1) > raw_len:
                    continue
                try:
                    if dtype == 'real':    sc[name] = plc_decode_real(raw, rel)
                    elif dtype == 'lreal': sc[name] = plc_decode_lreal(raw, rel)
                    elif dtype == 'int':   sc[name] = plc_decode_int(raw, rel)
                    elif dtype == 'bool':  sc[name] = plc_decode_bool(raw, rel, sz)
                except Exception:
                    pass

            self._rt_poll_count += 1
            self._rt_update_display(sc)

        except Exception as e:
            self._pv_rt_sb.set(f"Errore lettura: {e}")
            self._rt_err_count += 1
            if self._rt_err_count >= 10:
                self._rt_stop()
                messagebox.showerror("RT Monitor", f"Troppi errori consecutivi.\n{e}")
                return
        else:
            self._rt_err_count = 0

        if self._rt_running:
            self._rt_timer_id = self.after(self._rt_poll_ms, self._rt_poll)

    def _rt_update_display(self, sc: dict):
        """Aggiorna card valori, grafico e badge con i dati letti."""
        # ── Aggiorna card ──────────────────────────────────────
        istate_map = {0: "0 Idle", 1: "1 Online", 2: "2 Done", 3: "3 Error"}
        for key, sv in self._rt_vars.items():
            val = sc.get(key)
            if val is None:
                sv.set("—")
                continue
            if isinstance(val, bool):
                sv.set("✓ Sì" if val else "✗ No")
            elif key == "iState":
                sv.set(istate_map.get(int(val), str(int(val))))
            elif key in ("iSamplesAcquired","iClustersValid","iConsecutiveCount"):
                sv.set(str(int(val)))
            elif isinstance(val, float):
                sv.set(f"{val:.4f}")
            else:
                sv.set(str(val))

        # ── Badge stato ──────────────────────────────────────
        istate = int(sc.get("iState", 0))
        o_err  = bool(sc.get("O_Error", False))
        o_done = bool(sc.get("O_Done", False))
        trovata = bool(_sc(sc, "IO_RicercaSaldatura.Trovata", default=False))

        if o_err:
            self._pv_rt_badge.set("⬤  ERRORE PLC")
            self._rt_badge_lbl.config(fg=WELD_CLR)
        elif trovata:
            ang = sc.get("rDetectedAtAngle", 0)
            self._pv_rt_badge.set(f"★  SALDATURA TROVATA  @  {ang:.2f}°")
            self._rt_badge_lbl.config(fg="#f9c74f")
        elif istate == 2:
            self._pv_rt_badge.set("⬤  DONE — ciclo completato")
            self._rt_badge_lbl.config(fg=ACCENT)
        elif istate == 1:
            n = int(sc.get("iSamplesAcquired", 0))
            self._pv_rt_badge.set(f"⬤  BUSY — {n} campioni acquisiti")
            self._rt_badge_lbl.config(fg=OK_CLR)
        else:
            self._pv_rt_badge.set("⬤  IDLE")
            self._rt_badge_lbl.config(fg=MUTED_CLR)

        self._pv_rt_count.set(f"Letture: {self._rt_poll_count}")

        # ── Grafico scorrevole ──────────────────────────────────
        trace_key = self._pv_rt_trace.get()
        trace_val = sc.get(trace_key)
        if trace_val is not None:
            try:
                self._rt_buf_data.append(float(trace_val))
                buf_max = max(50, int(self._pv_rt_buf.get() or 150))
                if len(self._rt_buf_data) > buf_max:
                    self._rt_buf_data = self._rt_buf_data[-buf_max:]

                self._rt_ax.cla()
                self._style_axes(self._rt_ax, f"{trace_key}  (live)", "", "")
                y = self._rt_buf_data
                x = list(range(len(y)))
                self._rt_ax.plot(x, y, color="#58a6ff", linewidth=1.2)
                self._rt_ax.fill_between(x, y, alpha=0.12, color="#58a6ff")
                if len(y) > 1:
                    self._rt_ax.set_xlim(0, len(y)-1)
                # Evidenzia se trovata
                if trovata:
                    self._rt_ax.axhline(y[-1], color="#f9c74f",
                                        linewidth=0.8, linestyle="--", alpha=0.6)
                self._rt_fig.tight_layout(pad=1.2)
                self._rt_canvas.draw_idle()
            except Exception:
                pass

        # ── Status bar ──────────────────────────────────────────
        ts = datetime.datetime.now().strftime("%H:%M:%S.") + \
             f"{datetime.datetime.now().microsecond//1000:03d}"
        poll_ms = self._pv_rt_poll.get()
        self._pv_rt_sb.set(
            f"DB{self._rt_db_num}  |  {self._pv_plc_ip.get()}  |  "
            f"Poll {poll_ms}ms  |  Ultimo: {ts}")

    # ══════════════════════════════════════════════════════════════
    #  SEPARATORE FILE — finestra autonoma
    # ══════════════════════════════════════════════════════════════

    def _open_file_sorter(self):
        """Apre la finestra Separatore File come Toplevel indipendente."""
        win = tk.Toplevel(self)
        win.title("Separatore File .db — WeldFind")
        win.geometry("680x520")
        win.configure(bg=DARK_BG)
        win.resizable(True, True)

        # ── Stato ──
        _running  = [False]
        _stop_req = [False]

        # ── Header ──────────────────────────────────────────────
        tk.Label(win, text="\U0001f4c2  Separatore File .db",
                 bg=DARK_BG, fg=ACCENT,
                 font=("Consolas", 13, "bold")).pack(pady=(10,4))
        tk.Label(win,
                 text="Apre ogni .db nella cartella sorgente, controlla se contiene "
                      "una saldatura trovata,\nsposta i file SENZA saldatura nella "
                      "cartella Scarti.",
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 9),
                 justify="left").pack(padx=14)

        # ── Paths ────────────────────────────────────────────────
        paths_f = ttk.LabelFrame(win, text="  Percorsi  ", padding=8)
        paths_f.pack(fill="x", padx=12, pady=8)

        pv_src = tk.StringVar(value=os.path.expanduser("~"))
        pv_dst = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "Scarti"))

        def _browse(pv, title):
            d = filedialog.askdirectory(parent=win, title=title, initialdir=pv.get())
            if d: pv.set(d)

        for label, pv, ttl in [
            ("\u2705 Sorgente (tutti i .db):", pv_src, "Cartella sorgente"),
            ("\u274c Scarti (no saldatura):", pv_dst, "Cartella scarti"),
        ]:
            r = ttk.Frame(paths_f); r.pack(fill="x", pady=3)
            tk.Label(r, text=label, bg=DARK_BG, fg=TEXT_CLR,
                     font=("Consolas", 9), width=28, anchor="w").pack(side="left")
            ttk.Entry(r, textvariable=pv, width=30).pack(
                side="left", fill="x", expand=True, padx=4)
            ttk.Button(r, text="\U0001f4c1", width=3,
                       command=lambda p=pv, t=ttl: _browse(p, t)).pack(side="left")

        # ── Opzioni ──────────────────────────────────────────────
        opt_f = ttk.Frame(win); opt_f.pack(fill="x", padx=12, pady=2)
        pv_dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_f, text="Dry run (non sposta, solo mostra)",
                        variable=pv_dry).pack(side="left")
        pv_copy = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_f, text="Copia invece di spostare",
                        variable=pv_copy).pack(side="left", padx=12)

        # ── Barra progresso + stats ───────────────────────────────
        prog_f = ttk.Frame(win); prog_f.pack(fill="x", padx=12, pady=(6,2))
        pv_prog = tk.StringVar(value="Pronto")
        tk.Label(prog_f, textvariable=pv_prog,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas", 8)).pack(fill="x")
        prog_bar = ttk.Progressbar(prog_f, mode="determinate", maximum=100)
        prog_bar.pack(fill="x", pady=2)

        stats_f = ttk.Frame(win); stats_f.pack(fill="x", padx=12)
        stats = {}
        for col, (key, lbl, clr) in enumerate([
            ("tot",   "Totale",   TEXT_CLR),
            ("buoni", "\u2713 Buoni", OK_CLR),
            ("scarti","\u2717 Scarti", WELD_CLR),
            ("err",   "Errori",   WARN_CLR),
        ]):
            stats_f.columnconfigure(col, weight=1)
            tk.Label(stats_f, text=lbl, bg=DARK_BG, fg=MUTED_CLR,
                     font=("Consolas", 8)).grid(row=0, column=col, sticky="ew")
            sv = tk.StringVar(value="0")
            tk.Label(stats_f, textvariable=sv, bg=PANEL_BG, fg=clr,
                     font=("Consolas", 14, "bold")).grid(
                row=1, column=col, sticky="ew", padx=4, pady=2)
            stats[key] = sv

        # ── Log ─────────────────────────────────────────────────
        log_f = ttk.LabelFrame(win, text="  Log  ", padding=4)
        log_f.pack(fill="both", expand=True, padx=12, pady=4)
        log_sb = ttk.Scrollbar(log_f); log_sb.pack(side="right", fill="y")
        log_txt = tk.Text(log_f, bg=DARK_BG, fg=TEXT_CLR,
                          font=("Consolas", 8), wrap="none",
                          yscrollcommand=log_sb.set, height=8)
        log_txt.pack(fill="both", expand=True)
        log_sb.config(command=log_txt.yview)
        log_txt.tag_config("ok",   foreground=OK_CLR)
        log_txt.tag_config("err",  foreground=WELD_CLR)
        log_txt.tag_config("warn", foreground=WARN_CLR)
        log_txt.tag_config("info", foreground=ACCENT)

        def _log(msg, tag=""):
            log_txt.insert("end", msg + "\n", tag if tag else ())
            log_txt.see("end")

        # ── Pulsanti ─────────────────────────────────────────────
        btn_f = ttk.Frame(win); btn_f.pack(fill="x", padx=12, pady=(0,10))

        btn_run  = tk.Button(btn_f, text="\u25b6  AVVIA",
            bg=OK_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            command=lambda: _start())
        btn_run.pack(side="left", padx=4)
        btn_stop = tk.Button(btn_f, text="\u25a0  Stop",
            bg=WELD_CLR, fg=DARK_BG, font=("Consolas", 10, "bold"),
            state="disabled", command=lambda: _stop())
        btn_stop.pack(side="left")
        ttk.Button(btn_f, text="\U0001f5d1  Pulisci log",
                   command=lambda: log_txt.delete("1.0","end")).pack(side="left", padx=8)

        def _stop():
            _stop_req[0] = True

        def _start():
            src_dir = pv_src.get().strip()
            dst_dir = pv_dst.get().strip()
            if not os.path.isdir(src_dir):
                messagebox.showerror("Errore", f"Cartella sorgente non valida:\n{src_dir}",
                                     parent=win); return
            if not pv_dry.get() and not os.path.isdir(dst_dir):
                try: os.makedirs(dst_dir, exist_ok=True)
                except Exception as e:
                    messagebox.showerror("Errore", f"Impossibile creare cartella scarti:\n{e}",
                                         parent=win); return

            files = sorted([f for f in os.listdir(src_dir) if f.lower().endswith(".db")])
            if not files:
                messagebox.showwarning("Nessun file", "Nessun .db trovato nella cartella.",
                                       parent=win); return

            n_tot = len(files)
            for sv in stats.values(): sv.set("0")
            stats["tot"].set(str(n_tot))
            prog_bar["maximum"] = n_tot
            prog_bar["value"]   = 0
            log_txt.delete("1.0", "end")
            _log(f"Avvio separazione: {n_tot} file in \'{src_dir}\'", "info")
            if pv_dry.get():
                _log("  [DRY RUN — nessuno spostamento effettivo]", "warn")

            _running[0]  = True
            _stop_req[0] = False
            btn_run.config(state="disabled")
            btn_stop.config(state="normal")

            n_buoni = 0; n_scarti = 0; n_err = 0

            def _process_next(idx):
                nonlocal n_buoni, n_scarti, n_err
                if idx >= n_tot or _stop_req[0]:
                    _finish(idx)
                    return

                fname = files[idx]
                fpath = os.path.join(src_dir, fname)
                try:
                    data = parse_db_file(fpath)
                    sc   = data["scalars"]
                    # Criterio: saldatura trovata se iClustersValid >= 1 e iConsecutiveCount >= MinConsec
                    clusters_valid = int(sc.get("iClustersValid",
                                                sc.get("iClusterCount", 0)))
                    consec = int(sc.get("iConsecutiveCount",
                                       sc.get("iConsecutivCount", 0)))
                    min_cons = int(sc.get("I_MinConsecutive", 1))
                    # Alternativa: legge direttamente Trovata se presente
                    trovata_sc = sc.get("IO_RicercaSaldatura.Trovata",
                                        sc.get("bWeldFound", None))
                    if trovata_sc is not None:
                        weld_found = bool(trovata_sc)
                    else:
                        weld_found = (clusters_valid >= 1 and consec >= min_cons)

                    if weld_found:
                        n_buoni += 1
                        _log(f"  \u2713 {fname}  (saldatura trovata)", "ok")
                    else:
                        n_scarti += 1
                        dst_path = os.path.join(dst_dir, fname)
                        if not pv_dry.get():
                            import shutil
                            if pv_copy.get():
                                shutil.copy2(fpath, dst_path)
                                _log(f"  \u2717 {fname}  → copiato in scarti", "warn")
                            else:
                                shutil.move(fpath, dst_path)
                                _log(f"  \u2717 {fname}  → spostato in scarti", "warn")
                        else:
                            _log(f"  \u2717 {fname}  [DRY: andrebbe in scarti]", "warn")

                except Exception as e:
                    n_err += 1
                    _log(f"  ! {fname}  — errore: {e}", "err")

                stats["buoni"].set(str(n_buoni))
                stats["scarti"].set(str(n_scarti))
                stats["err"].set(str(n_err))
                prog_bar["value"] = idx + 1
                pct = 100 * (idx+1) / n_tot
                pv_prog.set(f"{idx+1} / {n_tot}  ({pct:.0f}%)")
                win.after(0, _process_next, idx + 1)

            def _finish(idx):
                _running[0] = False
                btn_run.config(state="normal")
                btn_stop.config(state="disabled")
                if _stop_req[0]:
                    _log(f"\n\u25a0 Interrotto a {idx} / {n_tot}", "warn")
                else:
                    _log(f"\n\u2713 Completato: {n_buoni} buoni, {n_scarti} scarti, {n_err} errori", "ok")
                pv_prog.set(f"Completato — {n_buoni} buoni  {n_scarti} scarti  {n_err} errori")

            win.after(10, _process_next, 0)

        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy()))


    # ══════════════════════════════════════════════════════════════
    #  TAB OPC UA — CONFIGURAZIONE, BROWSE E LETTURA
    # ══════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    #  SIMULATORE — sub-tab Sorgente (file .db | SQLite)
    # ══════════════════════════════════════════════════════════════

    def _build_sim_source_tab(self, parent):
        """Sub-tab per selezione sorgente dati simulazione."""
        self._sim_src_sql_rows = []

        main = ttk.Frame(parent); main.pack(fill="both", expand=True, padx=8, pady=8)

        # Toggle sorgente
        tog_lf = ttk.LabelFrame(main, text="  Sorgente  ", padding=6)
        tog_lf.pack(fill="x", pady=(0,6))
        self._pv_sim_source = tk.StringVar(value="file")
        tog = ttk.Frame(tog_lf); tog.pack(fill="x")
        ttk.Radiobutton(tog, text="📄 File .db",
                        variable=self._pv_sim_source, value="file",
                        command=self._sim_src_on_toggle).pack(side="left", padx=6)
        ttk.Radiobutton(tog, text="🗄 SQLite",
                        variable=self._pv_sim_source, value="sqlite",
                        command=self._sim_src_on_toggle).pack(side="left", padx=6)

        # Frame FILE
        self._sim_src_file_frame = ttk.LabelFrame(main, text="  File .db  ", padding=6)
        self._sim_src_file_frame.pack(fill="x", pady=2)
        ttk.Button(self._sim_src_file_frame, text="📂  Apri file .db...",
                   style="Accent.TButton",
                   command=self._open_file).pack(fill="x", pady=2)
        self._pv_sim_src_file_info = tk.StringVar(value="Nessun file caricato")
        tk.Label(self._sim_src_file_frame, textvariable=self._pv_sim_src_file_info,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(anchor="w")

        # Frame SQLITE
        self._sim_src_sql_frame = ttk.LabelFrame(main, text="  SQLite  ", padding=6)

        # Path database
        r1 = ttk.Frame(self._sim_src_sql_frame); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Database:", style="Muted.TLabel", width=9).pack(side="left")
        self._pv_sim_src_sql_path = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "weld_archive.sqlite"))
        ttk.Entry(r1, textvariable=self._pv_sim_src_sql_path, width=24).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(r1, text="📁", width=3,
                   command=self._sim_src_browse_sql).pack(side="left")

        # Selezione DB number
        db_lf = ttk.LabelFrame(self._sim_src_sql_frame, text="  DB number disponibili  ", padding=4)
        db_lf.pack(fill="x", pady=4)
        db_row = ttk.Frame(db_lf); db_row.pack(fill="x")
        ttk.Button(db_row, text="🔄 Scansiona", width=12,
                   command=self._sim_src_scan_dbs).pack(side="left", padx=2)
        self._pv_sim_src_db_info = tk.StringVar(value="")
        tk.Label(db_row, textvariable=self._pv_sim_src_db_info,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="left", padx=4)
        db_list_f = ttk.Frame(db_lf); db_list_f.pack(fill="x", pady=2)
        db_sb = ttk.Scrollbar(db_list_f, orient="vertical"); db_sb.pack(side="right", fill="y")
        self._sim_src_db_listbox = tk.Listbox(db_list_f,
            bg=ENTRY_BG, fg=TEXT_CLR, selectmode="extended",
            font=("Consolas",9), height=4, yscrollcommand=db_sb.set,
            selectbackground=ACCENT, activestyle="none")
        self._sim_src_db_listbox.pack(fill="x")
        db_sb.config(command=self._sim_src_db_listbox.yview)

        # Filtri rapidi
        flt_lf = ttk.LabelFrame(self._sim_src_sql_frame, text="  Filtri rapidi  ", padding=4)
        flt_lf.pack(fill="x", pady=4)
        f1 = ttk.Frame(flt_lf); f1.pack(fill="x", pady=1)
        self._pv_sim_src_found = tk.StringVar(value="tutti")
        ttk.Label(f1, text="Saldatura:", style="Muted.TLabel", width=10).pack(side="left")
        for txt, val in [("Tutti","tutti"),("✓ Trovate","trovate"),("✗ Non trovate","non_trovate")]:
            ttk.Radiobutton(f1, text=txt, variable=self._pv_sim_src_found,
                            value=val).pack(side="left", padx=4)

        f2 = ttk.Frame(flt_lf); f2.pack(fill="x", pady=1)
        ttk.Label(f2, text="Data da:", style="Muted.TLabel", width=10).pack(side="left")
        self._pv_sim_src_from = tk.StringVar(value="")
        ttk.Entry(f2, textvariable=self._pv_sim_src_from, width=16).pack(side="left", padx=2)
        ttk.Label(f2, text="a:", style="Muted.TLabel").pack(side="left", padx=2)
        self._pv_sim_src_to = tk.StringVar(value="")
        ttk.Entry(f2, textvariable=self._pv_sim_src_to, width=16).pack(side="left", padx=2)
        ttk.Label(f2, text="(yyyy-mm-dd HH:MM)", style="Muted.TLabel",
                  font=("Consolas",7)).pack(side="left")

        f3 = ttk.Frame(flt_lf); f3.pack(fill="x", pady=1)
        ttk.Label(f3, text="WHERE lib.:", style="Muted.TLabel", width=10).pack(side="left")
        self._pv_sim_src_where = tk.StringVar(value="")
        ttk.Entry(f3, textvariable=self._pv_sim_src_where, width=30).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Label(f3, text="(opz.)", style="Muted.TLabel", font=("Consolas",7)).pack(side="left")

        # Pulsante applica filtro
        btn_r = ttk.Frame(self._sim_src_sql_frame); btn_r.pack(fill="x", pady=4)
        tk.Button(btn_r, text="🔍  Applica filtro",
                  bg=ACCENT, fg=DARK_BG, font=("Consolas",9,"bold"),
                  command=self._sim_src_apply_filter).pack(side="left", padx=(0,6))
        self._pv_sim_src_count = tk.StringVar(value="")
        tk.Label(btn_r, textvariable=self._pv_sim_src_count,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="left")

        # Listbox risultati
        res_lf = ttk.LabelFrame(self._sim_src_sql_frame, text="  Acquisizioni  ", padding=4)
        res_lf.pack(fill="both", expand=True, pady=2)
        res_top = ttk.Frame(res_lf); res_top.pack(fill="x")
        ttk.Button(res_top, text="☑ Tutti",   width=8,
                   command=lambda: self._sim_src_results_lb.select_set(0,"end")).pack(side="left",padx=2)
        ttk.Button(res_top, text="☐ Nessuno", width=8,
                   command=lambda: self._sim_src_results_lb.select_clear(0,"end")).pack(side="left")
        ttk.Button(res_top, text="📥 Carica selezionato",
                   command=self._sim_src_load_selected).pack(side="right", padx=4)
        res_sb = ttk.Scrollbar(res_lf); res_sb.pack(side="right", fill="y")
        self._sim_src_results_lb = tk.Listbox(res_lf,
            bg=ENTRY_BG, fg=TEXT_CLR, selectmode="browse",
            font=("Consolas",8), height=8, yscrollcommand=res_sb.set,
            selectbackground=ACCENT, activestyle="none")
        self._sim_src_results_lb.pack(fill="both", expand=True)
        res_sb.config(command=self._sim_src_results_lb.yview)
        self._sim_src_results_lb.bind("<Double-1>", lambda e: self._sim_src_load_selected())

    def _sim_src_on_toggle(self):
        if self._pv_sim_source.get() == "file":
            self._sim_src_sql_frame.pack_forget()
            self._sim_src_file_frame.pack(fill="x", pady=2)
        else:
            self._sim_src_file_frame.pack_forget()
            self._sim_src_sql_frame.pack(fill="both", expand=True, pady=2)

    def _sim_src_browse_sql(self):
        p = filedialog.askopenfilename(parent=self, title="Database SQLite",
            filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")])
        if p: self._pv_sim_src_sql_path.set(p)

    def _sim_src_scan_dbs(self):
        """Scansiona il file SQLite e mostra i DB number disponibili."""
        import sqlite3
        path = self._pv_sim_src_sql_path.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("File", "Seleziona un file SQLite.", parent=self); return
        try:
            con = sqlite3.connect(path)
            rows = con.execute(
                "SELECT db_number, COUNT(*), SUM(weld_found) FROM acquisitions "
                "GROUP BY db_number ORDER BY db_number").fetchall()
            con.close()
            self._sim_src_db_listbox.delete(0,"end")
            for db_num, tot, found in rows:
                pct = f"{100*found/tot:.0f}%" if tot else "—"
                self._sim_src_db_listbox.insert("end",
                    f"DB{db_num}   ({tot} acq, {found} trovate, {pct})")
            self._sim_src_db_listbox.select_set(0,"end")
            self._pv_sim_src_db_info.set(f"{len(rows)} DB trovati")
            self._sim_src_db_nums = [r[0] for r in rows]
        except Exception as e:
            messagebox.showerror("SQLite", str(e), parent=self)

    def _sim_src_apply_filter(self):
        """Applica il filtro al database SQLite e mostra i risultati."""
        import sqlite3
        path = self._pv_sim_src_sql_path.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("File", "Seleziona un file SQLite.", parent=self); return

        # Costruisce WHERE
        conditions = []
        sel = self._sim_src_db_listbox.curselection()
        if sel and hasattr(self, '_sim_src_db_nums'):
            db_nums = [self._sim_src_db_nums[i] for i in sel]
            conditions.append(f"db_number IN ({','.join(str(d) for d in db_nums)})")

        found_val = self._pv_sim_src_found.get()
        if found_val == "trovate":     conditions.append("weld_found = 1")
        elif found_val == "non_trovate": conditions.append("weld_found = 0")

        dt_from = self._pv_sim_src_from.get().strip()
        dt_to   = self._pv_sim_src_to.get().strip()
        if dt_from: conditions.append(f"timestamp >= '{dt_from}'")
        if dt_to:   conditions.append(f"timestamp <= '{dt_to}'")

        where_extra = self._pv_sim_src_where.get().strip()
        if where_extra: conditions.append(f"({where_extra})")

        where = " AND ".join(conditions) if conditions else "1=1"
        query = (f"SELECT id, timestamp, db_number, weld_found, filename "
                 f"FROM acquisitions WHERE {where} ORDER BY timestamp DESC LIMIT 1000")

        def _run():
            con = sqlite3.connect(path)
            rows = con.execute(query).fetchall()
            con.close()
            return rows

        def _done(rows):
            self._sim_src_sql_rows = rows
            self._sim_src_results_lb.delete(0,"end")
            n_found = sum(1 for r in rows if r[3])
            for r in rows:
                icon = "✓" if r[3] else "✗"
                ts = r[1][:16] if r[1] else "?"
                self._sim_src_results_lb.insert("end",
                    f"{icon} DB{r[2]}  {ts}  {r[4] or f'row{r[0]}'}")
            self._pv_sim_src_count.set(
                f"{len(rows)} risultati  |  ✓{n_found}  ✗{len(rows)-n_found}")

        self._run_in_thread(_run, on_done=_done)

    def _sim_src_load_selected(self):
        """Carica l'acquisizione selezionata nel viewer."""
        import sqlite3
        sel = self._sim_src_results_lb.curselection()
        if not sel:
            messagebox.showwarning("Selezione","Seleziona una riga.", parent=self); return
        idx = sel[0]
        if idx >= len(self._sim_src_sql_rows):
            return
        row_id = self._sim_src_sql_rows[idx][0]
        path = self._pv_sim_src_sql_path.get().strip()

        def _load():
            con = sqlite3.connect(path)
            row = con.execute("SELECT * FROM acquisitions WHERE id=?", (row_id,)).fetchone()
            con.close()
            if not row: raise ValueError(f"Riga {row_id} non trovata")
            return weld_sqlite_load_row(row)

        def _done(data):
            self.db_data = data
            fname = data.get("filename", f"SQLite_row{row_id}")
            self.lbl_file.config(text=f"🗄 {fname}")
            n = int(data["scalars"].get("iSamplesAcquired",0))
            self.app_log(f"SQLite: {fname} ({n} camp.)", "ok")
            self._pv_sim_src_file_info.set(f"✓ {fname}")
            self._update_results_panel(); self._preload_sim_params(); self._recompute()
            # Vai ai grafici
            try: self._sim_outer_nb.select(1)
            except Exception: pass

        self._run_in_thread(_load, on_done=_done)

    # ══════════════════════════════════════════════════════════════
    #  STATISTICHE — helpers sorgente SQLite
    # ══════════════════════════════════════════════════════════════

    def _stat_on_source_change(self):
        if self._pv_stat_source.get() == "files":
            self._stat_sqlite_frame.pack_forget()
            self._stat_files_frame.pack(fill="x")
        else:
            self._stat_files_frame.pack_forget()
            self._stat_sqlite_frame.pack(fill="x")
            self._stat_load_from_sqlite()

    def _stat_load_from_sqlite(self):
        """Carica righe dal SQLite con filtri — usa SELECT * per compatibilità con weld_sqlite_load_row."""
        import sqlite3 as _sq3
        path = self._pv_stat_sql_path.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("SQLite", f"File non trovato:\n{path}", parent=self)
            return

        # Costruisce WHERE
        conditions = []
        found_val = self._pv_stat_sql_found.get()
        if found_val == "trovate":      conditions.append("weld_found = 1")
        elif found_val == "non_trovate": conditions.append("weld_found = 0")
        def _norm_date(s):
            """Normalizza data: DD/MM/YYYY o DD-MM-YYYY -> YYYY-MM-DD."""
            s = s.strip()
            if not s: return ''
            import re as _re
            m = _re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', s)
            if m: return f"{m.group(3)}-{m.group(2):>02s}-{m.group(1):>02s}"
            return s  # gia YYYY-MM-DD o altro
        dt_from = _norm_date(self._pv_stat_sql_from.get())
        dt_to   = _norm_date(self._pv_stat_sql_to.get())
        # 'A' include tutto il giorno: aggiungi ' 23:59:59' se solo data
        if dt_to and len(dt_to) == 10: dt_to += ' 23:59:59'
        if dt_from: conditions.append(f"timestamp >= '{dt_from}'")
        if dt_to:   conditions.append(f"timestamp <= '{dt_to}'")
        db_str = self._pv_stat_sql_db.get().strip()
        if db_str:
            dbs = [d.strip() for d in db_str.replace(",", " ").split() if d.strip().isdigit()]
            if dbs: conditions.append(f"db_number IN ({','.join(dbs)})")
        where = " AND ".join(conditions) if conditions else "1=1"

        def _query():
            con = _sq3.connect(path)
            # SELECT * per avere tutte le 21 colonne richieste da weld_sqlite_load_row
            rows = con.execute(
                f"SELECT * FROM acquisitions WHERE {where} ORDER BY timestamp DESC LIMIT 2000"
            ).fetchall()
            con.close()
            return rows

        def _done(rows):
            self._stat_sql_rows = rows   # righe complete (21 col) per weld_sqlite_load_row
            self._stat_listbox.delete(0, "end")
            self._stat_files = []
            for r in rows:
                row_id = r[0]; ts = r[1][:16] if r[1] else "?"
                db_num = r[2]; found = "\u2713" if r[4] else "\u2717"
                fname  = r[3] or f"row{row_id}"
                self._stat_listbox.insert("end", f"{found} DB{db_num}  {ts}  {fname}")
                self._stat_files.append(f"__sqlite_row_{row_id}")
            self._stat_listbox.select_set(0, "end")
            n  = len(rows)
            nf = sum(1 for r in rows if r[4])
            self._pv_stat_nfiles.set(f"{n} righe")
            self._pv_stat_sql_info.set(f"\u2713 {n} righe  |  \u2713{nf}  \u2717{n-nf}")

        def _err(e):
            messagebox.showerror("SQLite", str(e), parent=self)

        self._run_in_thread(_query, on_done=_done, on_error=_err)

    def _stat_sql_browse(self):
        p = filedialog.askopenfilename(parent=self, title="Database SQLite",
            filetypes=[("SQLite", "*.sqlite *.db3"), ("Tutti", "*.*")])
        if p: self._pv_stat_sql_path.set(p)


    # ══════════════════════════════════════════════════════════════
    #  SQLITE IMPORT TAB
    # ══════════════════════════════════════════════════════════════

    def _build_sqlite_import_tab(self, parent):
        self._sqlimp_running = False; self._sqlimp_stop_flag = False
        main = ttk.PanedWindow(parent, orient="horizontal"); main.pack(fill="both", expand=True)
        left  = ttk.Frame(main, width=400); main.add(left,  weight=0); left.pack_propagate(False)
        right = ttk.Frame(main);            main.add(right, weight=1)
        # Sorgente
        src_lf = ttk.LabelFrame(left, text="  Cartella sorgente .db  ", padding=6)
        src_lf.pack(fill="x", padx=6, pady=(8,4))
        r1 = ttk.Frame(src_lf); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Cartella:", style="Muted.TLabel", width=8).pack(side="left")
        self._pv_sqlimp_src = tk.StringVar(value=os.path.expanduser("~"))
        ttk.Entry(r1, textvariable=self._pv_sqlimp_src, width=20).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(r1, text="📁", width=3, command=self._sqlimp_browse_src).pack(side="left")
        self._pv_sqlimp_recurse = tk.BooleanVar(value=True)
        ttk.Checkbutton(src_lf, text="Includi sottocartelle", variable=self._pv_sqlimp_recurse).pack(anchor="w")
        ttk.Button(src_lf, text="🔍  Scansiona", command=self._sqlimp_scan,
                   style="Accent.TButton").pack(fill="x", pady=(4,0))
        # Lista file
        list_lf = ttk.LabelFrame(left, text="  File trovati  ", padding=4)
        list_lf.pack(fill="both", expand=True, padx=6, pady=4)
        lt = ttk.Frame(list_lf); lt.pack(fill="x")
        ttk.Button(lt, text="☑ Tutti",   width=8, command=lambda: self._sqlimp_select_all(True)).pack(side="left", padx=2)
        ttk.Button(lt, text="☐ Nessuno", width=8, command=lambda: self._sqlimp_select_all(False)).pack(side="left")
        self._pv_sqlimp_nfiles = tk.StringVar(value="0 file")
        tk.Label(lt, textvariable=self._pv_sqlimp_nfiles, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="right", padx=4)
        lsb = ttk.Scrollbar(list_lf); lsb.pack(side="right", fill="y")
        self._sqlimp_listbox = tk.Listbox(list_lf, bg=ENTRY_BG, fg=TEXT_CLR,
            selectmode="extended", font=("Consolas",8), yscrollcommand=lsb.set,
            selectbackground=ACCENT, activestyle="none", height=10)
        self._sqlimp_listbox.pack(fill="both", expand=True)
        lsb.config(command=self._sqlimp_listbox.yview)
        self._sqlimp_listbox.bind("<<ListboxSelect>>", self._sqlimp_on_select)
        self._sqlimp_files = []
        # Destinazione
        dst_lf = ttk.LabelFrame(left, text="  Database SQLite destinazione  ", padding=6)
        dst_lf.pack(fill="x", padx=6, pady=4)
        r2 = ttk.Frame(dst_lf); r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="File:", style="Muted.TLabel", width=5).pack(side="left")
        self._pv_sqlimp_dst = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "WeldExport", "weld_archive.sqlite"))
        ttk.Entry(r2, textvariable=self._pv_sqlimp_dst, width=20).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(r2, text="📁", width=3, command=self._sqlimp_browse_dst).pack(side="left")
        self._pv_sqlimp_db_info = tk.StringVar(value="")
        tk.Label(dst_lf, textvariable=self._pv_sqlimp_db_info, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(anchor="w")
        # Opzioni
        opt_lf = ttk.LabelFrame(left, text="  Opzioni  ", padding=4)
        opt_lf.pack(fill="x", padx=6, pady=2)
        self._pv_sqlimp_skip_dup    = tk.BooleanVar(value=True)
        self._pv_sqlimp_only_found  = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_lf, text="Salta duplicati (filename + db_number)", variable=self._pv_sqlimp_skip_dup).pack(anchor="w")
        ttk.Checkbutton(opt_lf, text="Importa solo saldatura trovata", variable=self._pv_sqlimp_only_found).pack(anchor="w")
        # Pulsanti
        btn_f = ttk.Frame(left); btn_f.pack(fill="x", padx=6, pady=6)
        self._sqlimp_run_btn  = tk.Button(btn_f, text="▶  IMPORTA SELEZIONATI",
            bg=STAT_CLR, fg=DARK_BG, font=("Consolas",9,"bold"), command=self._sqlimp_run)
        self._sqlimp_run_btn.pack(fill="x", pady=(0,3))
        self._sqlimp_stop_btn = tk.Button(btn_f, text="■  Stop",
            bg=WELD_CLR, fg=DARK_BG, font=("Consolas",9,"bold"), state="disabled", command=self._sqlimp_stop)
        self._sqlimp_stop_btn.pack(fill="x")
        # Destra: progress + stat + tabella + log
        prog_f = ttk.Frame(right); prog_f.pack(fill="x", padx=6, pady=(8,2))
        self._pv_sqlimp_prog_txt = tk.StringVar(value="Pronto")
        tk.Label(prog_f, textvariable=self._pv_sqlimp_prog_txt, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(fill="x")
        self._sqlimp_prog_bar = ttk.Progressbar(prog_f, mode="determinate", maximum=100)
        self._sqlimp_prog_bar.pack(fill="x", pady=2)
        stat_f = ttk.Frame(right); stat_f.pack(fill="x", padx=6, pady=2)
        self._sqlimp_stat_vars = {}
        for col,(key,lbl,clr) in enumerate([("tot","Totale",TEXT_CLR),("imp","✓ Importati",OK_CLR),
                ("skip","⏭ Saltati",MUTED_CLR),("err","✗ Errori",WELD_CLR),("found","★ Trovati",STAT_CLR)]):
            stat_f.columnconfigure(col, weight=1)
            tk.Label(stat_f, text=lbl, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).grid(row=0,column=col,sticky="ew")
            sv = tk.StringVar(value="0")
            tk.Label(stat_f, textvariable=sv, bg=PANEL_BG, fg=clr, font=("Consolas",13,"bold")).grid(row=1,column=col,sticky="ew",padx=3,pady=2)
            self._sqlimp_stat_vars[key] = sv
        db_lf = ttk.LabelFrame(right, text="  Riepilogo per DB number  ", padding=4)
        db_lf.pack(fill="x", padx=6, pady=2)
        db_sh = ttk.Scrollbar(db_lf, orient="horizontal"); db_sh.pack(side="bottom", fill="x")
        db_sv2 = ttk.Scrollbar(db_lf); db_sv2.pack(side="right", fill="y")
        self._sqlimp_db_tree = ttk.Treeview(db_lf, columns=("db","tot","trovati","pct"),
            show="headings", height=4, xscrollcommand=db_sh.set, yscrollcommand=db_sv2.set)
        db_sh.config(command=self._sqlimp_db_tree.xview); db_sv2.config(command=self._sqlimp_db_tree.yview)
        for cid,lbl,w in [("db","DB",90),("tot","Totale",70),("trovati","★ Trovati",70),("pct","% trovati",80)]:
            self._sqlimp_db_tree.heading(cid, text=lbl); self._sqlimp_db_tree.column(cid, width=w, anchor="center")
        self._sqlimp_db_tree.pack(fill="x")
        log_lf = ttk.LabelFrame(right, text="  Log  ", padding=4)
        log_lf.pack(fill="both", expand=True, padx=6, pady=(2,6))
        log_sb2 = ttk.Scrollbar(log_lf); log_sb2.pack(side="right", fill="y")
        self._sqlimp_log = tk.Text(log_lf, bg=DARK_BG, fg=TEXT_CLR, font=("Consolas",8), wrap="none", yscrollcommand=log_sb2.set)
        self._sqlimp_log.pack(fill="both", expand=True)
        log_sb2.config(command=self._sqlimp_log.yview)
        self._sqlimp_log.tag_config("ok", foreground=OK_CLR); self._sqlimp_log.tag_config("err", foreground=WELD_CLR)
        self._sqlimp_log.tag_config("warn", foreground=WARN_CLR); self._sqlimp_log.tag_config("info", foreground=ACCENT)

    def _sqlimp_browse_src(self):
        d = filedialog.askdirectory(parent=self, title="Cartella sorgente .db", initialdir=self._pv_sqlimp_src.get())
        if d: self._pv_sqlimp_src.set(d)
    def _sqlimp_browse_dst(self):
        p = filedialog.asksaveasfilename(parent=self, title="Database SQLite", defaultextension=".sqlite",
            filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")], initialfile="weld_archive.sqlite")
        if p: self._pv_sqlimp_dst.set(p); self._sqlimp_refresh_db_info()
    def _sqlimp_refresh_db_info(self):
        path = self._pv_sqlimp_dst.get().strip()
        if not path or not os.path.exists(path): self._pv_sqlimp_db_info.set(""); return
        try:
            import sqlite3; con=sqlite3.connect(path)
            n=con.execute("SELECT COUNT(*) FROM acquisitions").fetchone()[0]
            dbs=con.execute("SELECT db_number,COUNT(*) FROM acquisitions GROUP BY db_number").fetchall()
            con.close()
            parts=", ".join(f"DB{d}={c}" for d,c in sorted(dbs)[:5])
            self._pv_sqlimp_db_info.set(f"✓ {n} righe: {parts}")
        except Exception: self._pv_sqlimp_db_info.set("(nuovo file)")
    def _sqlimp_select_all(self, val):
        if val: self._sqlimp_listbox.select_set(0,"end")
        else:   self._sqlimp_listbox.select_clear(0,"end")
        self._sqlimp_on_select()
    def _sqlimp_on_select(self, _evt=None):
        n=len(self._sqlimp_listbox.curselection()); self._pv_sqlimp_nfiles.set(f"{n}/{len(self._sqlimp_files)} sel.")
    def _sqlimp_log_msg(self, msg, tag=""):
        self._sqlimp_log.insert("end", msg, tag if tag else ()); self._sqlimp_log.see("end")
    def _sqlimp_scan(self):
        src=self._pv_sqlimp_src.get().strip()
        if not os.path.isdir(src): messagebox.showwarning("Cartella",src,parent=self); return
        files=[]
        if self._pv_sqlimp_recurse.get():
            for root,_,fnames in os.walk(src):
                for f in sorted(fnames):
                    if f.lower().endswith(".db"): files.append(os.path.join(root,f))
        else:
            files=sorted([os.path.join(src,f) for f in os.listdir(src) if f.lower().endswith(".db")])
        self._sqlimp_files=files; self._sqlimp_listbox.delete(0,"end")
        for fp in files: self._sqlimp_listbox.insert("end", os.path.relpath(fp,src))
        self._sqlimp_listbox.select_set(0,"end"); self._pv_sqlimp_nfiles.set(f"{len(files)}/{len(files)} sel.")
        self._sqlimp_log_msg(f"Trovati {len(files)} file .db\n","info"); self._sqlimp_refresh_db_info()
    def _sqlimp_stop(self): self._sqlimp_stop_flag=True
    def _sqlimp_run(self):
        import re as _re, datetime as _dt
        sel=self._sqlimp_listbox.curselection()
        if not sel: messagebox.showwarning("Nessun file","Seleziona file.",parent=self); return
        dst=self._pv_sqlimp_dst.get().strip()
        if not dst: messagebox.showwarning("Dest","Seleziona SQLite.",parent=self); return
        files=[self._sqlimp_files[i] for i in sel]
        skip=self._pv_sqlimp_skip_dup.get(); only=self._pv_sqlimp_only_found.get()
        self._sqlimp_log.delete("1.0","end")
        for sv in self._sqlimp_stat_vars.values(): sv.set("0")
        self._sqlimp_stat_vars["tot"].set(str(len(files)))
        for row in self._sqlimp_db_tree.get_children(): self._sqlimp_db_tree.delete(row)
        self._sqlimp_prog_bar["value"]=0; self._sqlimp_run_btn.config(state="disabled")
        self._sqlimp_stop_btn.config(state="normal"); self._sqlimp_running=True; self._sqlimp_stop_flag=False
        n_imp=[0]; n_skip=[0]; n_err=[0]; n_found=[0]; db_stats={}
        try: os.makedirs(os.path.dirname(dst), exist_ok=True)
        except Exception: pass
        try: con=weld_sqlite_init(dst)
        except Exception as e:
            messagebox.showerror("SQLite",str(e),parent=self)
            self._sqlimp_run_btn.config(state="normal"); self._sqlimp_stop_btn.config(state="disabled"); return
        existing=set()
        if skip:
            try: existing={(r[0],r[1]) for r in con.execute("SELECT filename,db_number FROM acquisitions").fetchall()}
            except Exception: pass
        def _proc(idx):
            if idx>=len(files) or self._sqlimp_stop_flag: _fin(idx); return
            fp=files[idx]; fname=os.path.basename(fp)
            try:
                data=parse_db_file(fp); sc=data["scalars"]; ar=data["arrays"]
                db_num=int(sc.get("I_DbNumber",sc.get("iDbNumber",0)))
                if db_num==0:
                    # Cerca DB+cifre, poi numero 4-6 cifre nel nome
                    m=_re.search(r"DB(\d{2,6})",fname,_re.IGNORECASE)
                    if not m: m=_re.search(r"_(\d{4,6})[_.]",fname)
                    if not m: m=_re.search(r"(\d{4,6})(?:\D|$)",os.path.splitext(fname)[0])
                    db_num=int(m.group(1)) if m else 0
                trovata=sc.get("IO_RicercaSaldatura.Trovata",sc.get("bWeldFound",None))
                if trovata is not None:
                    try:    wf=float(trovata)!=0
                    except: wf=bool(trovata)
                else: wf=(int(float(sc.get("iClustersValid",0)))>=1 and int(float(sc.get("iConsecutiveCount",0)))>=int(float(sc.get("I_MinConsecutive",1))))
                if only and not wf: n_skip[0]+=1; self._sqlimp_log_msg(f"  \u23ed {fname}\n")
                elif skip and (fname,db_num) in existing: n_skip[0]+=1; self._sqlimp_log_msg(f"  \u23ed {fname} (dup)\n")
                else:
                    # Timestamp: dal nome file > data modifica > ora corrente
                    import re as _re2
                    _ts = None
                    _m = _re2.search(r'_(\d{8})_(\d{6})', fname)
                    if _m:
                        try: _ts = _dt.datetime.strptime(_m.group(1)+_m.group(2),"%Y%m%d%H%M%S").isoformat(sep=" ",timespec="seconds")
                        except Exception: pass
                    if not _ts:
                        try: _ts = _dt.datetime.fromtimestamp(os.path.getmtime(fp)).isoformat(sep=" ",timespec="seconds")
                        except Exception: pass
                    _ts = _ts or _dt.datetime.now().isoformat(sep=" ",timespec="seconds")
                    decoded={"scalars":sc,"arrays":ar,"timestamp":_ts}
                    rid=weld_sqlite_insert(con,db_num,decoded,fname); existing.add((fname,db_num))
                    n_imp[0]+=1
                    if wf: n_found[0]+=1
                    db_stats.setdefault(db_num,[0,0]); db_stats[db_num][0]+=1
                    if wf: db_stats[db_num][1]+=1
                    self._sqlimp_log_msg(f"  \u2713 DB{db_num} {fname} {'TROVATA' if wf else 'no'} #row{rid}\n","ok" if wf else "")
            except Exception as e: n_err[0]+=1; self._sqlimp_log_msg(f"  \u2717 {fname} {e}\n","err")
            for k,v in [("imp",n_imp[0]),("skip",n_skip[0]),("err",n_err[0]),("found",n_found[0])]:
                self._sqlimp_stat_vars[k].set(str(v))
            pct=100*(idx+1)/len(files); self._sqlimp_prog_bar["value"]=pct
            self._pv_sqlimp_prog_txt.set(f"{idx+1}/{len(files)} ({pct:.0f}%)")
            self.after(0, _proc, idx+1)
        def _fin(idx):
            con.close(); self._sqlimp_running=False
            self._sqlimp_run_btn.config(state="normal"); self._sqlimp_stop_btn.config(state="disabled")
            self._sqlimp_log_msg(f"\n\u2713 {n_imp[0]} importati {n_skip[0]} saltati {n_err[0]} errori\n","ok")
            self._sqlimp_prog_bar["value"]=100
            for row in self._sqlimp_db_tree.get_children(): self._sqlimp_db_tree.delete(row)
            for db_num,(tot,found) in sorted(db_stats.items()):
                self._sqlimp_db_tree.insert("","end",values=(f"DB{db_num}",tot,found,f"{100*found/tot:.0f}%" if tot else "—"))
            self._sqlimp_refresh_db_info()
        self._sqlimp_log_msg(f"Importazione {len(files)} file → '{dst}'\n","info")
        self.after(0,_proc,0)

    # ══════════════════════════════════════════════════════════════
    #  SQLITE QUERY TAB
    # ══════════════════════════════════════════════════════════════

    _SQL_FIELDS = [
        ("timestamp","Data/Ora","text"),("db_number","DB Number","int"),
        ("weld_found","Saldatura trovata","bool"),("det_angle","Angolo (°)","float"),
        ("peak_dev","PeakDev mm","float"),("peak_sigmas","SNR σ","float"),
        ("n_samples","N campioni","int"),("sigma_factor","SigmaFactor","float"),
        ("min_abs_dev","MinAbsDev mm","float"),("hyst_sigmas","HystSigmas","float"),
        ("window_deg","Window °","float"),("min_consec","MinConsec","int"),
        ("polarity","Polarità","int"),("filename","Filename","text"),
    ]
    _SQL_OPS = {"text":["=","!=","LIKE","NOT LIKE","IS NULL","IS NOT NULL"],
                "int": ["=","!=",">",">=","<","<=","BETWEEN","IN"],
                "float":["=","!=",">",">=","<","<=","BETWEEN"],
                "bool": ["= 1 (trovata)","= 0 (non trovata)"]}
    _SQL_PRESETS = [
        ("Tutte","1=1"),("Solo trovate","weld_found = 1"),("Solo non trovate","weld_found = 0"),
        ("Oggi","date(timestamp) = date('now')"),("Ultima ora","timestamp >= datetime('now','-1 hour')"),
        ("Ultime 24h","timestamp >= datetime('now','-1 day')"),
        ("Questa settimana","timestamp >= datetime('now','-7 days')"),
        ("SNR > 3 (robuste)","weld_found=1 AND peak_sigmas > 3"),
        ("Non trovate oggi","weld_found=0 AND date(timestamp)=date('now')"),
    ]

    def _build_sqlite_query_tab(self, parent):
        self._sqlqry_filter_rows=[]; self._sqlqry_results=[]; self._sqlqry_active_where="1=1"
        # Layout: top=filtri (fisso), bot=tabella risultati (espande)
        top = ttk.Frame(parent); top.pack(fill="x", side="top")
        bot = ttk.Frame(parent); bot.pack(fill="both", expand=True, side="top")
        # DB file
        db_bar = ttk.Frame(top); db_bar.pack(fill="x", padx=6, pady=(6,2))
        ttk.Label(db_bar, text="Database:", style="Muted.TLabel").pack(side="left")
        self._pv_sqlqry_path = tk.StringVar(value=os.path.join(os.path.expanduser("~"),"WeldExport","weld_archive.sqlite"))
        ttk.Entry(db_bar, textvariable=self._pv_sqlqry_path, width=38).pack(fill="x", padx=4)
        ttk.Button(db_bar, text="📁", width=3, command=self._sqlqry_browse).pack(side="left")
        self._pv_sqlqry_db_info = tk.StringVar(value="")
        tk.Label(db_bar, textvariable=self._pv_sqlqry_db_info, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="left", padx=6)
        # Filter notebook
        fnb = ttk.Notebook(top); fnb.pack(fill="x", padx=6, pady=2)
        bld_f = ttk.Frame(fnb); sql_f = ttk.Frame(fnb); pre_f = ttk.Frame(fnb)
        fnb.add(bld_f, text="  🧱  Builder  "); fnb.add(sql_f, text="  📝  SQL  "); fnb.add(pre_f, text="  ⚡  Preset  ")
        # Builder
        bld_top = ttk.Frame(bld_f); bld_top.pack(fill="x", padx=4, pady=(4,2))
        ttk.Button(bld_top, text="+ Aggiungi condizione", command=self._sqlqry_add_row).pack(side="left", padx=4)
        self._pv_sqlqry_logic = tk.StringVar(value="AND")
        ttk.Label(bld_top, text="Logica:", style="Muted.TLabel").pack(side="left", padx=(12,2))
        for txt in ("AND","OR"):
            ttk.Radiobutton(bld_top, text=txt, variable=self._pv_sqlqry_logic,
                            value=txt, command=self._sqlqry_update_preview).pack(side="left", padx=3)
        ttk.Button(bld_top, text="🗑 Pulisci", command=self._sqlqry_clear_rows).pack(side="right", padx=4)
        rows_c = tk.Canvas(bld_f, bg=DARK_BG, highlightthickness=0, height=110)
        rows_sb3 = ttk.Scrollbar(bld_f, orient="vertical", command=rows_c.yview)
        self._sqlqry_rows_frame = ttk.Frame(rows_c)
        self._sqlqry_rows_frame.bind("<Configure>", lambda e: rows_c.configure(scrollregion=rows_c.bbox("all")))
        rows_c.create_window((0,0), window=self._sqlqry_rows_frame, anchor="nw")
        rows_c.configure(yscrollcommand=rows_sb3.set); rows_sb3.pack(side="right", fill="y"); rows_c.pack(side="left", fill="x", expand=True)
        # SQL libero
        ttk.Label(sql_f, text="Clausola WHERE (senza WHERE):", style="Muted.TLabel").pack(anchor="w", padx=6, pady=(4,2))
        self._sqlqry_sql_text = tk.Text(sql_f, bg=ENTRY_BG, fg=TEXT_CLR, font=("Consolas",9), height=4, wrap="word", insertbackground=TEXT_CLR)
        self._sqlqry_sql_text.pack(fill="x", padx=6, pady=(0,4)); self._sqlqry_sql_text.insert("end","1=1")
        self._sqlqry_sql_text.bind("<KeyRelease>", lambda e: self._sqlqry_update_preview())
        ex_r = ttk.Frame(sql_f); ex_r.pack(fill="x", padx=6)
        tk.Label(ex_r, text="es: weld_found=0 AND db_number=11630 AND timestamp BETWEEN '2026-03-26 10:00' AND '2026-03-26 11:00'",
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",7), wraplength=500, justify="left").pack(side="left")
        # Preset
        pr_lf = ttk.Frame(pre_f); pr_lf.pack(fill="x", padx=6, pady=4)
        for name,where in self._SQL_PRESETS:
            r = ttk.Frame(pr_lf); r.pack(fill="x", pady=1)
            ttk.Button(r, text=name, width=24, command=lambda w=where: self._sqlqry_apply_preset(w)).pack(side="left")
            tk.Label(r, text=where[:60], bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",7)).pack(side="left", padx=4)
        # Preview + ordine + limite
        prev_f = ttk.Frame(top); prev_f.pack(fill="x", padx=6, pady=(2,2))
        ttk.Label(prev_f, text="Query:", style="Muted.TLabel", width=7).pack(side="left")
        self._pv_sqlqry_preview = tk.StringVar(value="SELECT ... WHERE 1=1")
        tk.Label(prev_f, textvariable=self._pv_sqlqry_preview, bg=PANEL_BG, fg=ACCENT,
                 font=("Consolas",8), anchor="w", relief="flat").pack(fill="x", padx=4)
        opt_f = ttk.Frame(top); opt_f.pack(fill="x", padx=6, pady=2)
        ttk.Label(opt_f, text="Ordina per:", style="Muted.TLabel").pack(side="left")
        self._pv_sqlqry_order = tk.StringVar(value="timestamp DESC")
        ttk.Combobox(opt_f, textvariable=self._pv_sqlqry_order, state="readonly", width=22,
            values=["timestamp DESC","timestamp ASC","db_number ASC","weld_found DESC","peak_sigmas DESC"]).pack(side="left", padx=4)
        ttk.Label(opt_f, text="Limite:", style="Muted.TLabel").pack(side="left", padx=(10,2))
        self._pv_sqlqry_limit = tk.StringVar(value="500")
        ttk.Entry(opt_f, textvariable=self._pv_sqlqry_limit, width=6).pack(side="left")
        ttk.Label(opt_f, text="righe", style="Muted.TLabel").pack(side="left", padx=2)
        # Pulsanti
        btn_f2 = ttk.Frame(top); btn_f2.pack(fill="x", padx=6, pady=(2,4))
        tk.Button(btn_f2, text="▶  ESEGUI QUERY", bg=ACCENT, fg=DARK_BG, font=("Consolas",9,"bold"),
                  command=self._sqlqry_run).pack(side="left", padx=(0,6))
        ttk.Button(btn_f2, text="💾  Export CSV", command=self._sqlqry_export_csv).pack(side="left")
        ttk.Button(btn_f2, text="📊  Carica nel Sim", style="Accent.TButton",
                   command=self._sqlqry_load_selected).pack(side="left", padx=6)
        tk.Button(btn_f2, text="🗑  Elimina sel.",
            bg="#5a1a1a", fg="#ff8888", font=("Consolas",9,"bold"),
            activebackground="#7a2a2a", activeforeground="#ffaaaa",
            command=self._sqlqry_delete_selected).pack(side="left", padx=4)
        self._pv_sqlqry_count = tk.StringVar(value="")
        tk.Label(btn_f2, textvariable=self._pv_sqlqry_count, bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="right")
        # Tabella risultati
        res_lf2 = ttk.LabelFrame(bot, text="  Risultati  ", padding=4); res_lf2.pack(fill="both", expand=True, padx=6, pady=4)
        res_sh2 = ttk.Scrollbar(res_lf2, orient="horizontal"); res_sh2.pack(side="bottom", fill="x")
        res_sv2 = ttk.Scrollbar(res_lf2); res_sv2.pack(side="right", fill="y")
        cols=["id","timestamp","db","trovata","angolo","peak_dev","snr","n_samp","sigma","min_abs","hyst","win","min_c","file"]
        self._sqlqry_tree = ttk.Treeview(res_lf2, columns=cols, show="headings",
            xscrollcommand=res_sh2.set, yscrollcommand=res_sv2.set)
        res_sh2.config(command=self._sqlqry_tree.xview); res_sv2.config(command=self._sqlqry_tree.yview)
        col_w={"id":40,"timestamp":140,"db":60,"trovata":65,"angolo":70,"peak_dev":80,"snr":60,"n_samp":70,
               "sigma":80,"min_abs":80,"hyst":60,"win":60,"min_c":55,"file":180}
        col_l={"id":"ID","timestamp":"Data/Ora","db":"DB","trovata":"Trovata","angolo":"Angolo°",
               "peak_dev":"PeakDev","snr":"SNR","n_samp":"Campio.","sigma":"SigmaF","min_abs":"MinAbs",
               "hyst":"Hyst","win":"Win°","min_c":"MinC","file":"File"}
        for c in cols:
            self._sqlqry_tree.heading(c, text=col_l.get(c,c), command=lambda _c=c: self._sqlqry_sort(_c))
            self._sqlqry_tree.column(c, width=col_w.get(c,70), anchor="center")
        self._sqlqry_tree.pack(fill="both", expand=True)
        self._sqlqry_tree.tag_configure("found", background="#0f2d0f", foreground=OK_CLR)
        self._sqlqry_tree.tag_configure("nofnd", background=ENTRY_BG, foreground=TEXT_CLR)
        self._sqlqry_tree.bind("<Double-1>", self._sqlqry_on_double_click)
        self._sqlqry_add_row()

    def _sqlqry_add_row(self):
        rf = ttk.Frame(self._sqlqry_rows_frame); rf.pack(fill="x", pady=1, padx=2)
        fv = tk.StringVar(value=self._SQL_FIELDS[0][1])
        fc = ttk.Combobox(rf, textvariable=fv, width=14, state="readonly",
                          values=[f[1] for f in self._SQL_FIELDS]); fc.current(0); fc.pack(side="left", padx=2)
        ov = tk.StringVar(value="=")
        oc = ttk.Combobox(rf, textvariable=ov, width=14, state="readonly", values=self._SQL_OPS["text"]); oc.pack(side="left", padx=2)
        v1 = tk.StringVar(value=""); e1 = ttk.Entry(rf, textvariable=v1, width=18); e1.pack(side="left", padx=2)
        v2l = ttk.Label(rf, text="e", style="Muted.TLabel"); v2 = tk.StringVar(); e2 = ttk.Entry(rf, textvariable=v2, width=12)
        def _rm(): rf.destroy(); self._sqlqry_filter_rows=[r for r in self._sqlqry_filter_rows if r["frame"].winfo_exists()]; self._sqlqry_update_preview()
        ttk.Button(rf, text="✕", width=2, command=_rm).pack(side="left", padx=2)
        def _fc(*_):
            dn=fv.get(); ft="text"
            for f in self._SQL_FIELDS:
                if f[1]==dn: ft=f[2]; break
            oc.config(values=self._SQL_OPS[ft]); oc.current(0); _oc()
        def _oc(*_):
            op=ov.get()
            if "BETWEEN" in op: v2l.pack(side="left",padx=2); e2.pack(side="left",padx=2)
            else: v2l.pack_forget(); e2.pack_forget()
            self._sqlqry_update_preview()
        fc.bind("<<ComboboxSelected>>", _fc); oc.bind("<<ComboboxSelected>>", _oc)
        e1.bind("<KeyRelease>", lambda e: self._sqlqry_update_preview())
        e2.bind("<KeyRelease>", lambda e: self._sqlqry_update_preview())
        row={"frame":rf,"field":fv,"op":ov,"val":v1,"val2":v2}
        self._sqlqry_filter_rows.append(row); self._sqlqry_update_preview()

    def _sqlqry_clear_rows(self):
        for r in self._sqlqry_filter_rows:
            try: r["frame"].destroy()
            except Exception: pass
        self._sqlqry_filter_rows=[]; self._sqlqry_add_row()

    def _sqlqry_build_where(self):
        parts=[]
        for r in self._sqlqry_filter_rows:
            try:
                if not r["frame"].winfo_exists(): continue
            except Exception: continue
            dn=r["field"].get(); col=dn; ft="text"
            for f in self._SQL_FIELDS:
                if f[1]==dn: col=f[0]; ft=f[2]; break
            op=r["op"].get(); val=r["val"].get().strip(); val2=r["val2"].get().strip()
            if "= 1" in op: parts.append(f"{col} = 1"); continue
            if "= 0" in op: parts.append(f"{col} = 0"); continue
            if "IS NULL" in op and "NOT" not in op: parts.append(f"{col} IS NULL"); continue
            if "IS NOT NULL" in op: parts.append(f"{col} IS NOT NULL"); continue
            if not val: continue
            if "BETWEEN" in op and val2:
                parts.append(f"{col} BETWEEN '{val}' AND '{val2}'" if ft=="text" else f"{col} BETWEEN {val} AND {val2}")
            elif "IN" in op: parts.append(f"{col} IN ({','.join(v.strip() for v in val.split(','))})")
            elif "LIKE" in op: parts.append(f"{col} {op} '%{val}%'")
            elif ft=="text": parts.append(f"{col} {op} '{val}'")
            else: parts.append(f"{col} {op} {val}")
        return f" {self._pv_sqlqry_logic.get()} ".join(parts) if parts else "1=1"

    def _sqlqry_update_preview(self, *_):
        where=self._sqlqry_build_where(); self._sqlqry_active_where=where
        lim=self._pv_sqlqry_limit.get() or "500"; order=self._pv_sqlqry_order.get()
        p=f"SELECT ... FROM acquisitions WHERE {where} ORDER BY {order} LIMIT {lim}"
        self._pv_sqlqry_preview.set(p[:120]+("..." if len(p)>120 else ""))

    def _sqlqry_apply_preset(self, where):
        try: self._sqlqry_sql_text.delete("1.0","end"); self._sqlqry_sql_text.insert("end",where)
        except Exception: pass
        self._sqlqry_active_where=where
        lim=self._pv_sqlqry_limit.get() or "500"; order=self._pv_sqlqry_order.get()
        self._pv_sqlqry_preview.set(f"SELECT ... WHERE {where} ORDER BY {order} LIMIT {lim}")

    def _sqlqry_browse(self):
        p=filedialog.askopenfilename(parent=self, title="Database SQLite",
            filetypes=[("SQLite","*.sqlite *.db3"),("Tutti","*.*")])
        if p: self._pv_sqlqry_path.set(p); self._sqlqry_refresh_info()

    def _sqlqry_refresh_info(self):
        path=self._pv_sqlqry_path.get().strip()
        if not path or not os.path.exists(path): self._pv_sqlqry_db_info.set(""); return
        try:
            import sqlite3; con=sqlite3.connect(path)
            n=con.execute("SELECT COUNT(*) FROM acquisitions").fetchone()[0]
            dbs=con.execute("SELECT db_number,COUNT(*) FROM acquisitions GROUP BY db_number").fetchall()
            con.close(); parts=" | ".join(f"DB{d}:{c}" for d,c in sorted(dbs)[:6])
            self._pv_sqlqry_db_info.set(f"  {n} righe — {parts}")
        except Exception as e: self._pv_sqlqry_db_info.set(f"  Errore: {e}")

    def _sqlqry_delete_selected(self):
        """Elimina le righe selezionate dal SQLite con doppia conferma."""
        sel = self._sqlqry_tree.selection()
        if not sel:
            messagebox.showwarning("Elimina", "Seleziona almeno una riga.")
            return
        ids = []
        for iid in sel:
            vals = self._sqlqry_tree.item(iid, "values")
            if vals:
                try: ids.append(int(vals[0]))
                except (ValueError, IndexError): pass
        if not ids:
            messagebox.showwarning("Elimina", "Impossibile recuperare gli ID delle righe.")
            return
        n = len(ids)
        if not messagebox.askyesno("Elimina righe",
                f"Stai per eliminare {n} riga{'e' if n>1 else ''} dal database SQLite.\nContinuare?",
                icon="warning"):
            return
        if not messagebox.askyesno("Conferma definitiva",
                f"ATTENZIONE: operazione irreversibile.\nEliminare definitivamente {n} riga{'e' if n>1 else ''}?",
                icon="warning", default="no"):
            return
        db_path = self._pv_sqlqry_path.get().strip()
        if not db_path or not os.path.isfile(db_path):
            messagebox.showerror("Elimina", "Database non trovato.")
            return
        try:
            import sqlite3 as _sq3
            con = _sq3.connect(db_path)
            placeholders = ",".join("?" for _ in ids)
            deleted = con.execute(
                f"DELETE FROM acquisitions WHERE id IN ({placeholders})", ids).rowcount
            con.commit(); con.close()
            for iid in sel:
                self._sqlqry_tree.delete(iid)
            self._pv_sqlqry_count.set(
                f"{len(self._sqlqry_tree.get_children())} righe  (−{deleted} eliminate)")
            self.app_log(f"Eliminate {deleted} righe SQLite id={ids}", "warn")
        except Exception as e:
            messagebox.showerror("Elimina", f"Errore: {e}")


    def _sqlqry_run(self):
        import sqlite3
        path=self._pv_sqlqry_path.get().strip()
        if not path: messagebox.showwarning("Database","Seleziona un file SQLite.",parent=self); return
        if not os.path.exists(path): messagebox.showwarning("Database",f"File non trovato:\n{path}",parent=self); return
        sql_raw=""
        try: sql_raw=self._sqlqry_sql_text.get("1.0","end").strip()
        except Exception: pass
        where=sql_raw if (sql_raw and sql_raw!="1=1") else self._sqlqry_build_where()
        lim=self._pv_sqlqry_limit.get() or "500"; order=self._pv_sqlqry_order.get()
        query=(f"SELECT id,timestamp,db_number,weld_found,det_angle,peak_dev,"
               f"peak_sigmas,n_samples,sigma_factor,min_abs_dev,hyst_sigmas,"
               f"window_deg,min_consec,filename FROM acquisitions WHERE {where} ORDER BY {order} LIMIT {lim}")
        def _run():
            con=sqlite3.connect(path); rows=con.execute(query).fetchall(); con.close(); return rows
        def _done(rows):
            self._sqlqry_results=rows
            for row in self._sqlqry_tree.get_children(): self._sqlqry_tree.delete(row)
            for r in rows:
                vals=[r[0],r[1],f"DB{r[2]}","✓ Sì" if r[3] else "✗ No",
                      f"{r[4]:.2f}" if r[4] else "—",f"{r[5]:.3f}" if r[5] else "—",
                      f"{r[6]:.2f}" if r[6] else "—",r[7],
                      f"{r[8]:.2f}" if r[8] else "—",f"{r[9]:.2f}" if r[9] else "—",
                      f"{r[10]:.2f}" if r[10] else "—",f"{r[11]:.1f}" if r[11] else "—",r[12],r[13] or ""]
                self._sqlqry_tree.insert("","end",values=tuple(vals),tags=("found" if r[3] else "nofnd",))
            n=len(rows); nf=sum(1 for r in rows if r[3])
            self._pv_sqlqry_count.set(f"{n} righe | ✓{nf} ✗{n-nf}")
            self._sqlqry_refresh_info()
            # Aggiorna contatore statistiche se usa sorgente SQLite
            try:
                if self._pv_stat_source.get() == "sqlite":
                    self._stat_load_from_sqlite()
            except Exception: pass
        def _err(e): messagebox.showerror("Errore query",str(e),parent=self)
        self._run_in_thread(_run, on_done=_done, on_error=_err)

    def _sqlqry_sort(self, col):
        items=[(self._sqlqry_tree.set(k,col),k) for k in self._sqlqry_tree.get_children("")]
        try: items.sort(key=lambda x: float(x[0].replace("DB","").replace("✓ Sì","1").replace("✗ No","0").replace("—","0") or 0))
        except (ValueError,TypeError): items.sort(key=lambda x: x[0])
        for idx,(_,k) in enumerate(items): self._sqlqry_tree.move(k,"",idx)

    def _sqlqry_export_csv(self):
        if not self._sqlqry_results: messagebox.showwarning("Nessun dato","Esegui prima una query.",parent=self); return
        path=filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
            filetypes=[("CSV","*.csv"),("Tutti","*.*")], initialfile="weld_query.csv")
        if not path: return
        import csv
        with open(path,"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f)
            w.writerow(["id","timestamp","db_number","weld_found","det_angle","peak_dev",
                        "peak_sigmas","n_samples","sigma_factor","min_abs_dev","hyst_sigmas",
                        "window_deg","min_consec","filename"])
            w.writerows(self._sqlqry_results)
        messagebox.showinfo("Export",f"Salvato: {path}",parent=self)

    def _sqlqry_on_double_click(self, event): self._sqlqry_load_selected()

    def _sqlqry_load_selected(self):
        import sqlite3
        sel=self._sqlqry_tree.selection()
        if not sel: messagebox.showwarning("Selezione","Seleziona una riga.",parent=self); return
        row_id=self._sqlqry_tree.item(sel[0])["values"][0]
        path=self._pv_sqlqry_path.get().strip()
        def _load():
            con=sqlite3.connect(path)
            row=con.execute("SELECT * FROM acquisitions WHERE id=?",(row_id,)).fetchone()
            con.close()
            if not row: raise ValueError(f"Riga {row_id} non trovata")
            return weld_sqlite_load_row(row)
        def _done(data):
            self.db_data=data; fname=data.get("filename",f"SQLite_row{row_id}")
            self.lbl_file.config(text=f"🗄 {fname}")
            self._update_results_panel(); self._preload_sim_params(); self._recompute(); self._update_raw_tab()
            self.nb.select(1); self.app_log(f"Caricato da SQLite: {fname}","ok")
            try: self.after(50, self._run_simulation)  # *** v4.7 *** auto-run
            except Exception: pass
        self._run_in_thread(_load, on_done=_done)



    def _draw_signal_tab(self):
        d = self.comp_data;  sc = self.db_data["scalars"];  a = d["angles"]
        self.ax1a.cla();  self.ax1b.cla()
        self._style_axes(self.ax1a, "Segnale laser + baseline adattiva", "Angolo (°)", "Valore")
        self._style_axes(self.ax1b, "Delta (segnale − baseline)", "Angolo (°)", "Δ")
        self.ax1a.plot(a, d["samples"],  color=ACCENT,   linewidth=1.2, label="Laser", zorder=3)
        self.ax1a.plot(a, d["baseline"], color=WELD_CLR, linewidth=1.5, linestyle="--",
                       label="Baseline (ricalc.)", zorder=4)
        if "baseline_db" in d:
            self.ax1a.plot(a, d["baseline_db"], color=OK_CLR, linewidth=1, linestyle=":", alpha=0.7, label="Baseline DB")
        if "mean_db" in d:
            m = d["mean_db"];  vm = m != 0
            if vm.any():
                self.ax1a.plot(a[vm], m[vm], color=OK_CLR, linewidth=1.5, label="arMean PLC", alpha=0.9)
        # *** v4.3.83 *** Soglie in base alla polarita del DB
        _pol4 = int(float(sc.get("I_PeakPolarity", 0)))
        _show_pos4 = _pol4 in (0, 2)
        _show_neg4 = _pol4 in (1, 2)
        if "thresh_hi_db" in d:
            th = d["thresh_hi_db"];  tl = d.get("thresh_lo_db", th);  vm = th != 0
            if vm.any() and _show_pos4:
                self.ax1a.plot(a[vm], th[vm], color="#FFD700", linewidth=1.5, linestyle="--", label="ThreshHigh PLC (+)", alpha=0.85)
                self.ax1a.plot(a[vm], tl[vm], color="#FFD700", linewidth=0.8, linestyle=":", alpha=0.6)
        if "thresh_hi_neg_db" in d:
            thn = d["thresh_hi_neg_db"];  tln = d.get("thresh_lo_neg_db", thn);  vmn = thn != 0
            if vmn.any() and _show_neg4:
                self.ax1a.plot(a[vmn], thn[vmn], color="#4fc3f7", linewidth=1.5, linestyle="--",
                               label="ThreshHigh PLC (\xe2\x88\x92)", alpha=0.85)
                self.ax1a.plot(a[vmn], tln[vmn], color="#4fc3f7", linewidth=0.8, linestyle=":", alpha=0.6)
        if _show_pos4:
            self.ax1a.plot(a, d["thresh_hi"], color=WARN_CLR, linewidth=0.8, linestyle="-.",
                           label="Soglia Python (+)", alpha=0.6)
            self.ax1a.fill_between(a, d["thresh_lo"], d["thresh_hi"], alpha=0.04, color=WARN_CLR)
        if _show_neg4 and "thresh_hi_neg" in d:
            self.ax1a.plot(a, d["thresh_hi_neg"], color="#4fc3f7", linewidth=0.8, linestyle="-.",
                           label="Soglia Python (\xe2\x88\x92)", alpha=0.6)
            self.ax1a.fill_between(a, d["thresh_hi_neg"], d["thresh_lo_neg"], alpha=0.04, color="#4fc3f7")
        if not _show_pos4 and not _show_neg4:
            self.ax1a.plot(a, d["thresh_hi"], color=WARN_CLR, linewidth=0.8, linestyle="-.", label="Soglia Python", alpha=0.6)
            self.ax1a.fill_between(a, d["thresh_lo"], d["thresh_hi"], alpha=0.04, color=WARN_CLR)

        wc   = _sc(sc, "IO_RicercaSaldatura.OutPosizioneAsse", "rWeldAngleCenter")
        ws   = sc.get("rWeldAngleStart"); we = sc.get("rWeldAngleEnd")
        da   = sc.get("rDetectedAtAngle")
        trov = _sc(sc, "IO_RicercaSaldatura.Trovata", "bWeldFound", default=False)
        # *** v4.7 *** Indicatore sup. piatta da scalari DB
        _flat_en = bool(float(sc.get('I_FlatWaitEnable', 0)))
        _flat_fd = bool(float(sc.get('bFlatSurfaceFound', 0)))
        if _flat_en and len(a) > 0 and len(d['samples']) > 0:
            _clr = '#3fb950' if _flat_fd else '#ff6b6b'
            _txt = '✓ Sup.piatta' if _flat_fd else '✗ Sup.piatta non trovata'
            _ypos = float(np.nanmin(d['samples'])) + (float(np.nanmax(d['samples'])) - float(np.nanmin(d['samples']))) * 0.06
            self.ax1a.text(float(a[0]), _ypos, _txt, color=_clr, fontsize=8, va='bottom', fontstyle='italic')
        if da:
            self.ax1a.axvline(float(da), color=DET_CLR, linewidth=2.5, linestyle="-",
                               alpha=0.9, label=f"★ Detection {float(da):.1f}°")
        if wc and trov:
            self.ax1a.axvline(float(wc), color=WELD_CLR, linewidth=1.5, linestyle="--",
                               alpha=0.7, label=f"OutPosizioneAsse {float(wc):.1f}°")
            if ws:
                self.ax1a.axvspan(float(ws), float(we), alpha=0.12, color=WELD_CLR, label="Zona saldatura")
        self.ax1a.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        if _pol4 == 1 and "thresh_hi_neg" in d:
            td = d["baseline"] - d["thresh_hi_neg"]  # distanza positiva dalla soglia neg
            _dlt = -d["delta"]
            self.ax1b.fill_between(a, d["delta"], 0, where=_dlt >= td, color=WELD_CLR, alpha=0.5, label="Sotto soglia (\xe2\x88\x92)")
            self.ax1b.fill_between(a, d["delta"], 0, where=_dlt < td,  color=ACCENT, alpha=0.3)
            self.ax1b.plot(a, -td, color="#4fc3f7", linewidth=1.2, linestyle="-.", label="Soglia \xce\x94 (\xe2\x88\x92)")
        else:
            td = d["thresh_hi"] - d["baseline"]
            self.ax1b.fill_between(a, d["delta"], 0, where=d["delta"] >= td, color=WELD_CLR, alpha=0.5, label="Sopra soglia")
            self.ax1b.fill_between(a, d["delta"], 0, where=d["delta"] < td,  color=ACCENT, alpha=0.3)
            self.ax1b.plot(a, td, color=WARN_CLR, linewidth=1.2, linestyle="-.", label="Soglia Δ adattiva")
        self.ax1b.axhline(0, color=MUTED_CLR, linewidth=0.8)
        if da:
            self.ax1b.axvline(float(da), color=DET_CLR, linewidth=2, alpha=0.8)
        self.ax1b.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig1.tight_layout(pad=2.5);  self.canvas1.draw_idle()

    # ── DRAW: TAB 2 (POLAR) ───────────────────────────────────
    def _draw_polar_tab(self):
        d = self.comp_data;  sc = self.db_data["scalars"]
        self.ax2.cla();  self._style_polar(self.ax2)
        ar = np.radians(d["angles"]);  s = d["samples"];  bl = d["baseline"]
        th_hi = d["thresh_hi"]

        # Profilo normalizzato: baseline = 1.0, deviazione scalata
        bl_med = np.nanmedian(bl)
        if bl_med < 1e-6:
            bl_med = 1.0
        # Scala: il profilo reale centrato sulla baseline, con deviazioni amplificate
        dev = s - bl
        dev_max = max(np.nanmax(np.abs(dev)), 1e-6)
        # Raggio: 1.0 = baseline, deviazioni scalate a ±0.6 del raggio
        rn = 1.0 + 0.6 * dev / dev_max
        rn_bl = np.ones_like(ar) * 1.0
        rn_th = 1.0 + 0.6 * (th_hi - bl) / dev_max

        # Profilo laser
        self.ax2.plot(ar, rn, color=ACCENT, linewidth=1.3, alpha=0.9, label="Profilo laser")
        self.ax2.fill_between(ar, rn_bl, rn, where=rn > rn_bl,
                               alpha=0.15, color=WELD_CLR, interpolate=True)
        self.ax2.fill_between(ar, rn, rn_bl, where=rn <= rn_bl,
                               alpha=0.08, color=ACCENT, interpolate=True)
        # Cerchio baseline
        self.ax2.plot(ar, rn_bl, color=OK_CLR, linewidth=1.0, linestyle="--", alpha=0.6, label="Baseline")
        # Cerchio soglia
        self.ax2.plot(ar, rn_th, color=WARN_CLR, linewidth=0.8, linestyle=":", alpha=0.5, label="Soglia")

        wc   = _sc(sc, "IO_RicercaSaldatura.OutPosizioneAsse", "rWeldAngleCenter")
        ws   = sc.get("rWeldAngleStart"); we = sc.get("rWeldAngleEnd")
        da   = sc.get("rDetectedAtAngle")
        trov = _sc(sc, "IO_RicercaSaldatura.Trovata", "bWeldFound", default=False)
        if da:
            self.ax2.axvline(np.radians(float(da)), color=DET_CLR, linewidth=2.5, alpha=0.9,
                              label=f"★ Detection {float(da):.1f}°")
        if wc and trov:
            if ws:
                arc = np.radians(np.linspace(float(ws), float(we), 60))
                r_max = np.nanmax(rn) * 1.05
                self.ax2.fill_between(arc, 0, np.full_like(arc, r_max),
                                       alpha=0.20, color=WELD_CLR, label="Zona saldatura")
            self.ax2.set_title(
                f"Profilo rilevato  |  OutPosizioneAsse {float(wc):.1f}°" +
                (f"  |  ★ detection {float(da):.1f}°" if da else ""),
                color=ACCENT, fontsize=10, pad=14)
        else:
            self.ax2.set_title("Vista polare — profilo laser", color=ACCENT, fontsize=10, pad=14)
        self.ax2.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR,
                         loc="upper right", bbox_to_anchor=(1.15, 1.0))
        self.ax2.set_xticks(np.radians([0,90,180,270]))
        self.ax2.set_xticklabels(["0°","90°","180°","270°"], color=MUTED_CLR, fontsize=9)
        self.fig2.tight_layout(pad=2.5);  self.canvas2.draw_idle()

    # ── DRAW: TAB 3 (HISTOGRAM) ───────────────────────────────
    def _draw_hist_tab(self):
        d = self.comp_data
        self.ax3a.cla();  self.ax3b.cla()
        self._style_axes(self.ax3a, "Distribuzione segnale", "Valore", "Conteggio")
        self._style_axes(self.ax3b, "Distribuzione delta",   "Δ",      "Conteggio")
        sc = d["samples"][~np.isnan(d["samples"])]
        dc = d["delta"][~np.isnan(d["delta"])]
        self.ax3a.hist(sc, bins=40, color=ACCENT, alpha=0.8, edgecolor=BORDER_CLR)
        mu = d["global_mean"];  sg = d["global_std"]
        self.ax3a.axvline(mu,    color=OK_CLR,   linewidth=1.5, label=f"μ={mu:.2f}")
        self.ax3a.axvline(mu+sg, color=WARN_CLR, linewidth=1, linestyle="--", label=f"σ={sg:.2f}")
        self.ax3a.axvline(mu-sg, color=WARN_CLR, linewidth=1, linestyle="--")
        self.ax3a.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.ax3b.hist(dc, bins=40, color=WELD_CLR, alpha=0.7, edgecolor=BORDER_CLR)
        tm = float(np.median(d["thresh_hi"] - d["baseline"]))
        self.ax3b.axvline(tm, color=WARN_CLR, linewidth=1.5, linestyle="-.", label=f"Soglia med.={tm:.2f}")
        self.ax3b.axvline(0, color=MUTED_CLR, linewidth=0.8)
        self.ax3b.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.fig3.tight_layout(pad=2.5);  self.canvas3.draw_idle()

    # ── DRAW: TAB 4 (GREZZI) ──────────────────────────────────
    def _draw_raw_graphs_tab(self):
        if not self.db_data:
            return
        arrays = self.db_data["arrays"];  sc = self.db_data["scalars"]
        n = self._resolve_n_acq()
        rl = np.array(arrays.get("arSamples",[])[:n], dtype=float)
        ra = np.array(arrays.get("arAngles", [])[:n], dtype=float)  # v4.3.2 angoli grezzi
        if len(rl) == 0:
            return
        idx = np.arange(len(rl))
        for ax in (self.ax4a, self.ax4b, self.ax4c):
            ax.cla()
        self._style_axes(self.ax4a, f"Laser grezzo ({len(rl)} campioni)", "Indice", "Valore")
        self._style_axes(self.ax4b, "Angolo encoder grezzo", "Indice", "Angolo (°)")
        self._style_axes(self.ax4c, "Laser vs Angolo (scatter)", "Angolo (°)", "Valore")
        self.ax4a.plot(idx, rl, color=ACCENT, linewidth=1.0, alpha=0.9)
        self.ax4a.fill_between(idx, rl, np.nanmin(rl), alpha=0.12, color=ACCENT)
        da  = sc.get("rDetectedAtAngle")
        wc  = _sc(sc, "IO_RicercaSaldatura.OutPosizioneAsse", "rWeldAngleCenter")
        ws  = sc.get("rWeldAngleStart"); we = sc.get("rWeldAngleEnd")
        if da and len(ra) > 0:
            ix = int(np.argmin(np.abs(ra - float(da))))
            self.ax4a.axvline(ix, color=DET_CLR, linewidth=2, linestyle="-",
                              label=f"★ Detection idx={ix}")
        if wc and ws and len(ra) > 0:
            ix_s = int(np.argmin(np.abs(ra - float(ws))))
            ix_e = int(np.argmin(np.abs(ra - float(we))))
            self.ax4a.axvspan(ix_s, ix_e, alpha=0.18, color=WELD_CLR, label="Zona saldatura")
        self.ax4a.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        self.ax4b.plot(idx, ra, color=OK_CLR, linewidth=1.0, alpha=0.9)
        self.ax4b.fill_between(idx, ra, 0, alpha=0.10, color=OK_CLR)
        self.ax4b.set_ylim(np.nanmin(ra) - 10, np.nanmax(ra) + 15)  # *** v4.3.1 *** adaptive
        sc2 = self.ax4c.scatter(ra, rl, c=idx, cmap="plasma", s=4, alpha=0.8, linewidths=0, zorder=3)
        self.ax4c.plot(ra, rl, color=ACCENT, linewidth=0.5, alpha=0.3)
        cb = self.fig4.colorbar(sc2, ax=self.ax4c, pad=0.01)
        cb.set_label("Indice campione", color=MUTED_CLR, fontsize=8)
        cb.ax.yaxis.set_tick_params(color=MUTED_CLR, labelsize=7)
        [lbl.set_color(MUTED_CLR) for lbl in cb.ax.yaxis.get_ticklabels()]
        cb.outline.set_edgecolor(BORDER_CLR)
        self.ax4c.set_xlim(np.nanmin(ra) - 5, np.nanmax(ra) + 10)  # *** v4.3.1 *** adaptive
        self.fig4.tight_layout(pad=2.8);  self.canvas4.draw_idle()

    # ── DRAW: TAB 5 ───────────────────────────────────────────
    def _draw_samples_angles_tab(self):
        if not self.db_data:
            return
        arrays = self.db_data["arrays"];  sc = self.db_data["scalars"]
        n = self._resolve_n_acq()
        rl = np.array(arrays.get("arSamples",[])[:n], dtype=float)
        ra = np.array(arrays.get("arAngles", [])[:n], dtype=float)  # v4.3.2 angoli grezzi
        if len(rl) == 0:
            return
        idx = np.arange(len(rl))
        wc  = _sc(sc, "IO_RicercaSaldatura.OutPosizioneAsse", "rWeldAngleCenter")
        ws  = sc.get("rWeldAngleStart"); we = sc.get("rWeldAngleEnd")
        pv  = sc.get("rPeakValue")
        self.ax5_main.cla();  self.ax5_top.cla();  self.ax5_right.cla()
        self._style_axes(self.ax5_main, "", "arAngles (°)", "arSamples")
        self._style_axes(self.ax5_top,  f"arSamples ↔ arAngles  n={len(rl)}", "", "N")
        self._style_axes(self.ax5_right,"", "N", "")
        sp = self.ax5_main.scatter(ra, rl, c=idx, cmap="plasma", s=6, alpha=0.85, linewidths=0, zorder=4)
        self.ax5_main.plot(ra, rl, color=ACCENT, linewidth=0.6, alpha=0.25)
        mu = np.nanmean(rl);  sg = np.nanstd(rl)
        self.ax5_main.axhspan(mu-sg, mu+sg, alpha=0.08, color=OK_CLR)
        self.ax5_main.axhline(mu, color=OK_CLR, linewidth=0.9, linestyle="--", alpha=0.7)
        if ws:
            self.ax5_main.axvspan(float(ws), float(we), alpha=0.18, color=WELD_CLR)
        da = sc.get("rDetectedAtAngle")
        if da:
            self.ax5_main.axvline(float(da), color=DET_CLR, linewidth=2, label=f"★ {float(da):.1f}°")
            self.ax5_main.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
        cb = self.fig5.colorbar(sp, ax=self.ax5_right, location="right", pad=0.04, shrink=0.85)
        cb.set_label("Indice", color=MUTED_CLR, fontsize=8)
        cb.ax.yaxis.set_tick_params(color=MUTED_CLR, labelsize=7)
        [lbl.set_color(MUTED_CLR) for lbl in cb.ax.yaxis.get_ticklabels()]
        cb.outline.set_edgecolor(BORDER_CLR)
        ha, ea = np.histogram(ra, bins=72, range=(float(np.nanmin(ra)), float(np.nanmax(ra)) + 5))  # *** v4.3.1 *** adaptive
        ca = (ea[:-1]+ea[1:])/2
        self.ax5_top.fill_between(ca, ha, 0, alpha=0.5, color=ACCENT, step="mid")
        self.ax5_top.plot(ca, ha, color=ACCENT, linewidth=1.0)
        if ws:
            self.ax5_top.axvspan(float(ws), float(we), alpha=0.25, color=WELD_CLR)
        self.ax5_top.tick_params(labelbottom=False)
        hs, es = np.histogram(rl, bins=40)
        cs = (es[:-1]+es[1:])/2
        self.ax5_right.fill_betweenx(cs, hs, 0, alpha=0.5, color=OK_CLR, step="mid")
        self.ax5_right.plot(hs, cs, color=OK_CLR, linewidth=1.0)
        if pv:
            self.ax5_right.axhline(float(pv), color=WELD_CLR, linewidth=1.2, linestyle="-", alpha=0.9)
        self.ax5_right.tick_params(labelleft=False)
        self.fig5.subplots_adjust(left=0.08, right=0.96, top=0.92, bottom=0.08);  self.canvas5.draw_idle()

    # ── RAW TAB ───────────────────────────────────────────────
    def _update_raw_tab(self):
        self.txt_raw.config(state="normal")
        self.txt_raw.delete("1.0", "end")
        if self.db_data:
            self.txt_raw.insert("end", self.db_data["raw_text"])
        self.txt_raw.config(state="disabled")

    # ── EXPORT ────────────────────────────────────────────────
    def _export_png(self):
        if not self.comp_data:
            messagebox.showwarning("Nessun dato", "Caricare prima un file .db");  return
        path = filedialog.asksaveasfilename(defaultextension=".png",
            filetypes=[("PNG","*.png")], initialfile="weld_analysis.png")
        if path:
            tab  = self.nb.index("current")
            figs = [self.fig1, self.fig2, self.fig3, self.fig4, self.fig5]
            figs[min(tab, 4)].savefig(path, dpi=150, facecolor=DARK_BG, bbox_inches="tight")
            messagebox.showinfo("Esportato", f"Salvato:\n{path}")

    def _export_csv(self):
        if not self.comp_data:
            messagebox.showwarning("Nessun dato", "Caricare prima un file .db");  return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
            filetypes=[("CSV","*.csv")], initialfile="weld_samples.csv")
        if not path:
            return
        d = self.comp_data
        with open(path, "w", encoding="utf-8") as f:
            f.write("index,angle_deg,laser,baseline,thresh_hi,thresh_lo,delta,above\n")
            for i, (a, s, b, th, tl, dv) in enumerate(
                    zip(d["angles"], d["samples"], d["baseline"],
                        d["thresh_hi"], d["thresh_lo"], d["delta"])):
                f.write(f"{i},{a:.3f},{s:.4f},{b:.4f},{th:.4f},{tl:.4f},{dv:.4f},{'1' if s>=th else '0'}\n")
        messagebox.showinfo("Esportato", f"CSV:\n{path}")

    # ══════════════════════════════════════════════════════════════
    #  TAB STATISTICHE — SWEEP MULTI-FILE
    #  Come il Parameter Sweep ma su N file contemporaneamente.
    #  Per ogni valore del parametro: simula tutti i file, aggrega i
    #  risultati (% trovati, SNR medio, SNR minimo) e li mostra su
    #  grafici unici. Propone il valore ottimale.
    # ══════════════════════════════════════════════════════════════

    def _build_stat_tab(self, parent):
        """Tab Statistiche: grid-search parallelo multi-file, non bloccante."""
        STAT_CLR  = "#f9c74f"
        STAT_CLR2 = "#4fc3f7"

        # ── Stato interno sweep parallelo ──────────────────────
        self._stat_futures   = []
        self._stat_executor  = None
        self._stat_poll_id   = None
        self._stat_total     = 0
        self._stat_done      = 0
        self._stat_results   = {}   # (fi, ci) → (found, snr, peak_dev, det_angle)
        self._stat_combos    = []
        self._stat_paths     = []
        self._stat_files     = []
        self._stat_last_best = {}

        # ── Layout ─────────────────────────────────────────────
        main = ttk.PanedWindow(parent, orient="horizontal")
        main.pack(fill="both", expand=True)
        left_outer = ttk.Frame(main, width=330)
        main.add(left_outer, weight=0)
        left_outer.pack_propagate(False)
        lcanv = tk.Canvas(left_outer, bg=DARK_BG, highlightthickness=0)
        lscrl = ttk.Scrollbar(left_outer, orient="vertical", command=lcanv.yview)
        lfrm  = ttk.Frame(lcanv)
        lfrm.bind("<Configure>", lambda e: lcanv.configure(scrollregion=lcanv.bbox("all")))
        lwin = lcanv.create_window((0,0), window=lfrm, anchor="nw")
        lcanv.configure(yscrollcommand=lscrl.set)
        lcanv.bind("<Configure>", lambda e: lcanv.itemconfigure(lwin, width=e.width))
        lcanv.bind("<MouseWheel>", lambda e: lcanv.yview_scroll(int(-1*(e.delta/120)),"units"))
        lscrl.pack(side="right", fill="y")
        lcanv.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(main); main.add(right, weight=1)

        # ── File sorgente ───────────────────────────────────────
        src_lf = ttk.LabelFrame(lfrm, text="  Sorgente dati  ", padding=6)
        src_lf.pack(fill="x", padx=4, pady=(6,2))
        self._pv_stat_source = tk.StringVar(value="files")
        tog = ttk.Frame(src_lf); tog.pack(fill="x", pady=(0,4))
        ttk.Radiobutton(tog, text="📂 File .db", variable=self._pv_stat_source,
            value="files", command=self._stat_on_source_change).pack(side="left",padx=4)
        ttk.Radiobutton(tog, text="🔍 SQLite Query", variable=self._pv_stat_source,
            value="sqlite", command=self._stat_on_source_change).pack(side="left",padx=4)
        self._stat_files_frame = ttk.Frame(src_lf)
        self._stat_files_frame.pack(fill="x")
        r1 = ttk.Frame(self._stat_files_frame); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Cartella:").pack(side="left")
        self._pv_stat_dir = tk.StringVar(value=os.path.expanduser("~"))
        ttk.Entry(r1, textvariable=self._pv_stat_dir, width=16).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(r1, text="📁", width=3,
                   command=self._stat_browse_dir).pack(side="left")
        ttk.Button(self._stat_files_frame, text="🔄  Aggiorna lista .db",
                   command=self._stat_load_list, style="Accent.TButton").pack(fill="x", pady=(4,2))
        self._stat_sqlite_frame = ttk.Frame(src_lf)
        # Path file SQLite
        sql_r1 = ttk.Frame(self._stat_sqlite_frame); sql_r1.pack(fill="x", pady=2)
        ttk.Label(sql_r1, text="File:", style="Muted.TLabel", width=5).pack(side="left")
        self._pv_stat_sql_path = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "WeldExport", "weld_archive.sqlite"))
        ttk.Entry(sql_r1, textvariable=self._pv_stat_sql_path, width=16).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(sql_r1, text="📁", width=3,
                   command=self._stat_sql_browse).pack(side="left")
        # Filtri rapidi
        sql_r2 = ttk.Frame(self._stat_sqlite_frame); sql_r2.pack(fill="x", pady=1)
        ttk.Label(sql_r2, text="Saldatura:", style="Muted.TLabel", width=9).pack(side="left")
        self._pv_stat_sql_found = tk.StringVar(value="tutti")
        for txt, val in [("Tutti","tutti"), ("✓","trovate"), ("✗","non_trovate")]:
            ttk.Radiobutton(sql_r2, text=txt, variable=self._pv_stat_sql_found,
                            value=val).pack(side="left", padx=3)
        sql_r3 = ttk.Frame(self._stat_sqlite_frame); sql_r3.pack(fill="x", pady=1)
        ttk.Label(sql_r3, text="Da:", style="Muted.TLabel", width=5).pack(side="left")
        self._pv_stat_sql_from = tk.StringVar(value="")
        ttk.Entry(sql_r3, textvariable=self._pv_stat_sql_from, width=14).pack(side="left", padx=2)
        ttk.Label(sql_r3, text="A:", style="Muted.TLabel").pack(side="left")
        self._pv_stat_sql_to = tk.StringVar(value="")
        ttk.Entry(sql_r3, textvariable=self._pv_stat_sql_to, width=14).pack(side="left", padx=2)
        ttk.Label(sql_r3, text="YYYY-MM-DD o GG/MM/AAAA", style="Muted.TLabel",
                  font=("Consolas",7)).pack(side="left", padx=(4,0))
        sql_r4 = ttk.Frame(self._stat_sqlite_frame); sql_r4.pack(fill="x", pady=1)
        ttk.Label(sql_r4, text="DB:", style="Muted.TLabel", width=5).pack(side="left")
        self._pv_stat_sql_db = tk.StringVar(value="")
        ttk.Entry(sql_r4, textvariable=self._pv_stat_sql_db, width=44).pack(side="left", padx=2)
        ttk.Label(sql_r4, text="es. 28160,28010", style="Muted.TLabel",
                  font=("Consolas",7)).pack(side="left")
        # Pulsante applica
        ttk.Button(self._stat_sqlite_frame, text="🔍  Carica da SQLite",
                   style="Accent.TButton",
                   command=self._stat_load_from_sqlite).pack(fill="x", pady=(4,2))
        self._pv_stat_sql_info = tk.StringVar(value="Seleziona file e premi Carica")
        tk.Label(self._stat_sqlite_frame, textvariable=self._pv_stat_sql_info,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(anchor="w")
        # stat source starts hidden (files_frame visible by default)

        list_lf = ttk.LabelFrame(lfrm, text="  File selezionati  ", padding=4)
        list_lf.pack(fill="both", expand=True, padx=4, pady=2)
        lt = ttk.Frame(list_lf); lt.pack(fill="x")
        ttk.Button(lt, text="\u2611 Tutti",   width=8,
                   command=lambda: self._stat_select_all(True)).pack(side="left", padx=2)
        ttk.Button(lt, text="\u2610 Nessuno", width=8,
                   command=lambda: self._stat_select_all(False)).pack(side="left")
        self._pv_stat_nfiles = tk.StringVar(value="0 file")
        tk.Label(lt, textvariable=self._pv_stat_nfiles,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(side="right", padx=4)
        lsb = ttk.Scrollbar(list_lf); lsb.pack(side="right", fill="y")
        self._stat_listbox = tk.Listbox(list_lf,
            bg=ENTRY_BG, fg=TEXT_CLR, selectmode="extended",
            font=("Consolas",8), yscrollcommand=lsb.set,
            selectbackground=ACCENT, activestyle="none", height=8,
            exportselection=False)
        self._stat_listbox.pack(fill="both", expand=True)
        lsb.config(command=self._stat_listbox.yview)
        self._stat_listbox.bind("<<ListboxSelect>>", self._stat_on_select)

        # ── Grid-search parametri ───────────────────────────────
        hdr_lf = ttk.LabelFrame(lfrm, text="  Grid Search — parametri in sweep  ", padding=4)
        hdr_lf.pack(fill="x", padx=4, pady=2)

        # header colonne
        hdr_r = ttk.Frame(hdr_lf); hdr_r.pack(fill="x", pady=(0,2))
        for col_txt, col_w in [("\u2611", 22), ("Parametro", 110),
                                ("Da", 46), ("A", 46), ("Passi", 36)]:
            ttk.Label(hdr_r, text=col_txt, style="Muted.TLabel",
                      width=max(1, col_w//8), anchor="center").pack(side="left")

        # Definizione parametri sweepabili
        SWEEP_DEFS = [
            ("sigma_factor",           "SigmaFactor",    "3.0",  "1.0","3.0", "6",  False),
            ("min_abs_dev",            "MinAbsDev [mm]", "0.5",  "0.4","0.6", "6",  False),
            ("hyst_sigmas",            "HystSigmas",     "0.5",  "0.0","1.0", "5",  False),
            ("window_deg",             "Window [\u00b0]","2.0",  "2.0","5.0", "6",  False),
            ("min_consecutive",        "MinConsec",       "4",    "4",  "6",   "6",  True ),
            ("flat_wait_enable",       "FlatWait En",     "0",    "0",  "1",   "2",  True ),
            ("flat_wait_samples",      "FlatWait Samp",   "5",    "3",  "15",  "4",  True ),
            ("flat_wait_toll",         "FlatWait Toll[mm]","0.25","0.1","0.5", "4",  False),
        ]
        self._stat_swp = {}

        def _make_stat_row(key, label, vdef, da, a, steps, is_int):
            r = ttk.Frame(hdr_lf); r.pack(fill="x", pady=1)
            chk_v = tk.BooleanVar(value=True)
            frm_v = tk.StringVar(value=da)
            to_v  = tk.StringVar(value=a)
            stp_v = tk.StringVar(value=steps)
            fix_v = tk.StringVar(value=vdef)

            def _toggle(*_):
                st = "normal" if chk_v.get() else "disabled"
                for w in (e_frm, e_to, e_stp): w.config(state=st)
                e_fix.config(state="disabled" if chk_v.get() else "normal")

            tk.Checkbutton(r, variable=chk_v, command=_toggle,
                bg=DARK_BG, selectcolor="#1f6feb",
                activebackground=DARK_BG).pack(side="left", padx=2)
            ttk.Label(r, text=label, style="Muted.TLabel",
                      width=14, anchor="w").pack(side="left")
            e_fix = ttk.Entry(r, textvariable=fix_v, width=5, state="disabled")
            e_fix.pack(side="left", padx=1)
            e_frm = ttk.Entry(r, textvariable=frm_v, width=5); e_frm.pack(side="left", padx=1)
            e_to  = ttk.Entry(r, textvariable=to_v,  width=5); e_to.pack(side="left", padx=1)
            e_stp = ttk.Entry(r, textvariable=stp_v, width=4); e_stp.pack(side="left", padx=1)
            self._stat_swp[key] = {"chk": chk_v, "fix": fix_v,
                                    "frm": frm_v, "to": to_v,
                                    "stp": stp_v, "int": is_int}

        for args in SWEEP_DEFS:
            _make_stat_row(*args)

        # Parametri fissi
        fix_lf = ttk.LabelFrame(lfrm, text="  Fissi  ", padding=4)
        fix_lf.pack(fill="x", padx=4, pady=2)
        self._stat_fix = {}

        # MaxConsec e StopOnWeld — campi numerici
        for lbl, key, dflt in [
            ("MaxConsec",  "max_consecutive", "60"),
            ("StopOnWeld", "stop_on_weld",    "1"),
        ]:
            rr = ttk.Frame(fix_lf); rr.pack(fill="x", pady=1)
            ttk.Label(rr, text=f"{lbl}:", style="Muted.TLabel",
                      width=16).pack(side="left")
            v = tk.StringVar(value=dflt)
            ttk.Entry(rr, textvariable=v, width=7).pack(side="left")
            self._stat_fix[key] = v

        # Polarità — 3 radio button
        pol_row = ttk.Frame(fix_lf); pol_row.pack(fill="x", pady=2)
        ttk.Label(pol_row, text="Polarità:", style="Muted.TLabel",
                  width=16).pack(side="left")
        self._stat_polarity_var = tk.IntVar(value=0)
        for txt, val in [("⊕ Positivo", 0), ("⊖ Negativo", 1), ("± Entrambi", 2)]:
            ttk.Radiobutton(pol_row, text=txt,
                            variable=self._stat_polarity_var,
                            value=val).pack(side="left", padx=4)
        # Wrapper StringVar per compatibilità con _stat_run che legge self._stat_fix
        class _IntVarAsStrVar:
            def __init__(self, ivar): self._v = ivar
            def get(self): return str(self._v.get())
        self._stat_fix["peak_polarity"] = _IntVarAsStrVar(self._stat_polarity_var)

        # Stima combinazioni
        self._pv_stat_est = tk.StringVar(value="")
        tk.Label(lfrm, textvariable=self._pv_stat_est,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(padx=4, anchor="w")
        for k in self._stat_swp:
            for sv in (self._stat_swp[k]["frm"], self._stat_swp[k]["to"],
                       self._stat_swp[k]["stp"], self._stat_swp[k]["chk"]):
                sv.trace_add("write", self._stat_update_estimate)

        # Workers
        n_cpu = os.cpu_count() or 4
        wk_lf = ttk.LabelFrame(lfrm, text="  Parallelismo  ", padding=4)
        wk_lf.pack(fill="x", padx=4, pady=2)
        wk_r = ttk.Frame(wk_lf); wk_r.pack(fill="x")
        ttk.Label(wk_r, text="Worker (proc):", style="Muted.TLabel").pack(side="left")
        self._pv_stat_workers = tk.StringVar(value=str(max(1, n_cpu // 2)))
        ttk.Entry(wk_r, textvariable=self._pv_stat_workers, width=4).pack(side="left", padx=4)
        ttk.Label(wk_r, text=f"(CPU: {n_cpu})", style="Muted.TLabel").pack(side="left")

        # Pulsanti
        btn_f = ttk.Frame(lfrm); btn_f.pack(fill="x", padx=4, pady=6)
        self._btn_stat_run = tk.Button(btn_f, text="\u25b6  AVVIA GRID SEARCH",
            bg=STAT_CLR, fg=DARK_BG, font=("Consolas",9,"bold"),
            command=self._stat_run)
        self._btn_stat_run.pack(fill="x", pady=(0,3))
        self._btn_stat_stop = tk.Button(btn_f, text="\u25a0  Stop",
            bg=WELD_CLR, fg=DARK_BG, font=("Consolas",9,"bold"),
            state="disabled", command=self._stat_stop)
        self._btn_stat_stop.pack(fill="x")

        # ════════ DESTRA ════════════════════════════════════════

        # Barra progresso
        prog_f = ttk.Frame(right); prog_f.pack(fill="x", padx=6, pady=(6,2))
        self._pv_stat_prog_txt = tk.StringVar(value="Pronto")
        tk.Label(prog_f, textvariable=self._pv_stat_prog_txt,
                 bg=DARK_BG, fg=MUTED_CLR, font=("Consolas",8)).pack(fill="x")
        self._stat_prog_bar = ttk.Progressbar(prog_f, mode="determinate", maximum=100)
        self._stat_prog_bar.pack(fill="x", pady=2)
        self._stat_prog_bar["value"] = 0

        # Box best set
        best_lf = ttk.LabelFrame(right, text="  \U0001f3c6  Parametri ottimali  ", padding=6)
        best_lf.pack(fill="x", padx=6, pady=2)
        self._pv_stat_best = tk.StringVar(value="\u2014  avvia il grid search")
        tk.Label(best_lf, textvariable=self._pv_stat_best,
                 bg=PANEL_BG, fg=STAT_CLR,
                 font=("Consolas",9,"bold"),
                 justify="left", anchor="w").pack(fill="x")

        # Griglia parametri ottimali — mostra i valori dei param in sweep + % trovati
        # Le card vengono popolate dinamicamente in _stat_update_results
        pg = ttk.Frame(best_lf); pg.pack(fill="x", pady=(4,0))
        self._stat_prop_vars = {}
        self._stat_prop_frame = pg   # salvato per aggiornamento dinamico colonne

        # Card fissa: % trovati (sempre visibile)
        pg.columnconfigure(0, weight=2)
        ttk.Label(pg, text="% trovati", style="Muted.TLabel",
                  font=("Consolas",8)).grid(row=0, column=0, padx=6, sticky="ew")
        v_pct = tk.StringVar(value="\u2014")
        tk.Label(pg, textvariable=v_pct, bg=PANEL_BG, fg=STAT_CLR,
                 font=("Consolas",16,"bold"), width=7, anchor="center"
                 ).grid(row=1, column=0, padx=4, pady=2, sticky="ew")
        self._stat_prop_vars["pct"] = v_pct

        # Card per ogni param sweep (colonne 1..N) — create ora, aggiornate al run
        PARAM_LABELS = {
            "sigma_factor":           "SigmaFactor",
            "min_abs_dev":            "MinAbsDev",
            "hyst_sigmas":            "HystSigmas",
            "window_deg":             "Window\u00b0",
            "min_consecutive":        "MinConsec",
            "flat_wait_enable":       "FlatEn",
            "flat_wait_samples":      "FlatSamp",
            "flat_wait_toll":         "FlatToll",
        }
        PARAM_COLORS = {
            "sigma_factor":           "#58a6ff",
            "min_abs_dev":            "#3fb950",
            "hyst_sigmas":            "#bc8cff",
            "window_deg":             "#f0883e",
            "min_consecutive":        "#4fc3f7",
            "flat_wait_enable":       "#3fb980",
            "flat_wait_samples":      "#3fb9a0",
            "flat_wait_toll":         "#3fb9c0",
        }
        for col, key in enumerate(PARAM_LABELS, start=1):
            pg.columnconfigure(col, weight=1)
            ttk.Label(pg, text=PARAM_LABELS[key], style="Muted.TLabel",
                      font=("Consolas",8)).grid(row=0, column=col, padx=4, sticky="ew")
            v = tk.StringVar(value="\u2014")
            tk.Label(pg, textvariable=v, bg=PANEL_BG, fg=PARAM_COLORS[key],
                     font=("Consolas",13,"bold"), width=7, anchor="center"
                     ).grid(row=1, column=col, padx=3, pady=2, sticky="ew")
            self._stat_prop_vars[key] = v

        bf = ttk.Frame(best_lf); bf.pack(fill="x", pady=(6,0))
        ttk.Button(bf, text="\U0001f4cb Applica al Simulatore",
                   style="Accent.TButton",
                   command=self._stat_apply_to_sim).pack(side="left", padx=4)
        ttk.Button(bf, text="\U0001f4cb Copia",
                   command=self._stat_copy_params).pack(side="left")
        ttk.Button(bf, text="\U0001f4c2  Separatore File",
                   command=self._open_file_sorter).pack(side="left", padx=4)

        # Barra discordanze PLC vs Sim
        disc_f = ttk.LabelFrame(right, text="  \u26a0  Discordanze PLC \u2194 Sim  ", padding=4)
        disc_f.pack(fill="x", padx=6, pady=2)
        self._pv_stat_disc = tk.StringVar(value="\u2014  esegui il grid search")
        _dl = tk.Label(disc_f, textvariable=self._pv_stat_disc,
                 bg=PANEL_BG, fg=WARN_CLR, font=("Consolas",9,"bold"),
                 anchor="w", justify="left")
        _dl.pack(fill="x", padx=4)
        self._stat_disc_label = _dl

        # Tabella top risultati
        tbl_lf = ttk.LabelFrame(right, text="  Risultati (% trovati ↓, SNR med ↓)  ", padding=4)
        tbl_lf.pack(fill="x", padx=6, pady=2)
        tbl_sh = ttk.Scrollbar(tbl_lf, orient="horizontal"); tbl_sh.pack(side="bottom", fill="x")
        tbl_sv = ttk.Scrollbar(tbl_lf);                       tbl_sv.pack(side="right",  fill="y")
        self._stat_tree = ttk.Treeview(tbl_lf, show="headings", height=10,
                                        xscrollcommand=tbl_sh.set, yscrollcommand=tbl_sv.set)
        tbl_sh.config(command=self._stat_tree.xview)
        tbl_sv.config(command=self._stat_tree.yview)
        self._stat_tree.pack(fill="x")
        self._stat_tree.tag_configure("best", background="#0f2d0f", foreground=OK_CLR,
                                       font=("Consolas", 9, "bold"))
        self._stat_tree.tag_configure("good", background=ENTRY_BG, foreground=TEXT_CLR)
        self._stat_tree.tag_configure("poor", background=ENTRY_BG, foreground=MUTED_CLR)

        # Dividi right in: grafico (alto) + tabella discordanze (basso)
        # Grafico parametri (nascosto, mantenuto per _stat_update_results)
        _stat_fig_frame = ttk.Frame(right)
        self._stat_fig = Figure(facecolor=DARK_BG)
        self._stat_ax1 = self._stat_fig.add_subplot(111)
        self._style_axes(self._stat_ax1, "", "", "")
        self._stat_canvas = FigureCanvasTkAgg(self._stat_fig, _stat_fig_frame)

        # Tabella discordanze occupa tutto lo spazio
        disc_lf = ttk.LabelFrame(right,
            text="  ⚠  Discordanze PLC ↔ Sim  (doppio click → carica nel Simulatore)  ",
            padding=4)
        disc_lf.pack(fill="both", expand=True, padx=4, pady=(2,4))
        _sd_sbv = ttk.Scrollbar(disc_lf); _sd_sbv.pack(side="right", fill="y")
        _sd_sbh = ttk.Scrollbar(disc_lf, orient="horizontal"); _sd_sbh.pack(side="bottom", fill="x")
        _sd_cols = ("file","plc","sim","plc_ang","sim_ang","delta_ang","plc_cons")
        self._stat_disc_tree = ttk.Treeview(disc_lf, columns=_sd_cols, show="headings",
                                             yscrollcommand=_sd_sbv.set,
                                             xscrollcommand=_sd_sbh.set)
        _sd_sbv.config(command=self._stat_disc_tree.yview)
        _sd_sbh.config(command=self._stat_disc_tree.xview)
        for _c, _l, _w in [
            ("file","File",220),("plc","PLC",50),("sim","Sim",50),
            ("plc_ang","Ang PLC",75),("sim_ang","Ang Sim",75),
            ("delta_ang","Δ ang",65),("plc_cons","Cons PLC",70),
        ]:
            self._stat_disc_tree.heading(_c, text=_l)
            self._stat_disc_tree.column(_c, width=_w, anchor="center" if _c != "file" else "w")
        self._stat_disc_tree.tag_configure("disc",  background="#2a1a1a", foreground=WARN_CLR)
        self._stat_disc_tree.tag_configure("match", foreground=MUTED_CLR)
        self._stat_disc_tree.pack(fill="both", expand=True)
        self._stat_disc_tree.bind("<Button-3>",
            lambda e: self._disc_context_menu(e, self._stat_disc_tree,
                getattr(self,"_disc_rows",[])))

    # ── helpers stat ─────────────────────────────────────────

    def _stat_browse_dir(self):
        d = filedialog.askdirectory(parent=self,
            title="Cartella con file .db",
            initialdir=self._pv_stat_dir.get())
        if d:
            self._pv_stat_dir.set(d)
            self._stat_load_list()

    def _stat_load_list(self):
        d = self._pv_stat_dir.get().strip()
        if not os.path.isdir(d):
            messagebox.showwarning("Cartella non valida", f"\'{d}\' non è una cartella.")
            return
        files = sorted([os.path.join(d, f) for f in os.listdir(d)
                        if f.lower().endswith(".db")])
        self._stat_files = files
        self._stat_listbox.delete(0, "end")
        for fp in files:
            self._stat_listbox.insert("end", os.path.basename(fp))
        self._stat_listbox.select_set(0, "end")
        self._pv_stat_nfiles.set(f"{len(files)} file")
        self._stat_update_estimate()

    def _stat_select_all(self, val: bool):
        if val: self._stat_listbox.select_set(0, "end")
        else:   self._stat_listbox.select_clear(0, "end")
        self._stat_on_select()

    def _stat_on_select(self, _evt=None):
        n = len(self._stat_listbox.curselection())
        self._pv_stat_nfiles.set(f"{n} / {len(self._stat_files)} selezionati")
        self._stat_update_estimate()

    def _stat_update_estimate(self, *_):
        """Mostra stima numero simulazioni prima di avviare."""
        try:
            n_files = len(self._stat_listbox.curselection()) or len(self._stat_files)
            n_combos = 1
            for key, d in self._stat_swp.items():
                if d["chk"].get():
                    ns = max(2, int(d["stp"].get() or 2))
                    n_combos *= ns
            total = n_combos * n_files
            workers = int(self._pv_stat_workers.get() or 1)
            # Stima ~0.3ms per simulazione per core
            est_s = total * 0.0003 / max(workers, 1)
            self._pv_stat_est.set(
                f"~{n_combos:,} combinazioni × {n_files} file = {total:,} simulazioni "
                f"(est. {est_s:.0f}s con {workers} worker)")
        except Exception:
            pass

    def _stat_build_grid(self):
        """Costruisce il prodotto cartesiano dei parametri in sweep."""
        keys, arrays = [], []
        for key, d in self._stat_swp.items():
            if d["chk"].get():
                va = float(d["frm"].get()); vb = float(d["to"].get())
                ns = max(2, int(d["stp"].get()))
                arr = np.linspace(va, vb, ns)
                if d["int"]:
                    arr = np.unique(np.round(arr).astype(int)).astype(float)
                keys.append(key)
                arrays.append(arr)
        combos = list(itertools.product(*arrays))

        # *** Se flat_wait_enable e' fisso=0 (non in sweep e valore fisso 0),
        # rimuovi flat_wait_samples e flat_wait_toll dal grid:
        # non ha senso farne sweep se flat e' disabilitato.
        fw_en_key = "flat_wait_enable"
        fw_en_in_sweep = fw_en_key in keys
        fw_en_fixed_val = 0
        if not fw_en_in_sweep and fw_en_key in self._stat_swp:
            try: fw_en_fixed_val = int(float(self._stat_swp[fw_en_key]["fix"].get()))
            except: pass
        if not fw_en_in_sweep and fw_en_fixed_val == 0:
            # Rimuovi flat_wait_samples e flat_wait_toll dal grid se presenti
            for rm_key in ("flat_wait_samples", "flat_wait_toll"):
                if rm_key in keys:
                    idx = keys.index(rm_key)
                    keys = [k for k in keys if k != rm_key]
                    # Rimuovi colonna idx dalle combo e deduplica
                    combos = list(dict.fromkeys(
                        tuple(v for i,v in enumerate(c) if i != idx)
                        for c in combos
                    ))

        return keys, combos

    # ── worker top-level (definito sotto come _stat_sim_worker) ──

    def _stat_run(self):
        """Avvia grid-search parallelo — architettura initializer-cached.

        Ogni worker carica i file UNA SOLA VOLTA (initializer).
        Ogni task trasmette solo (fi, combo_tuple) = ~55 bytes IPC.
        Speedup atteso: 44x rispetto a parse per ogni task.
        """
        import threading

        sel = self._stat_listbox.curselection()
        if not sel:
            messagebox.showwarning("Nessun file", "Seleziona almeno un elemento.")
            return

        use_sqlite = self._pv_stat_source.get() == "sqlite"
        paths = [self._stat_files[i] for i in sel]

        try:
            keys, combos = self._stat_build_grid()
        except ValueError as e:
            messagebox.showerror("Parametri sweep", str(e)); return

        if not combos:
            messagebox.showwarning("Nessun sweep", "Abilita almeno un parametro.")
            return

        try:
            fixed = {
                "max_consecutive": int(self._stat_fix["max_consecutive"].get() or 60),
                "peak_polarity":   int(self._stat_fix["peak_polarity"].get()   or 0),
                "stop_on_weld":    bool(int(self._stat_fix["stop_on_weld"].get() or 1)),
            }
            # *** v4.3.82 BUGFIX *** Aggiunge i valori FISSI dei parametri sweep non abilitati.
            # Senza questo, quando sweep e' disabilitato il worker usa i default Python
            # (sig=3.0, mabs=1.5, hyst=0.5, win=10.0, mc=3) invece dei valori impostati.
            for _k, _d in self._stat_swp.items():
                if not _d["chk"].get():  # sweep disabilitato -> usa valore fisso
                    try:
                        _raw = _d["fix"].get()
                        fixed[_k] = int(float(_raw)) if _d["int"] else float(_raw)
                    except (ValueError, TypeError):
                        pass
            # *** v4.6 *** aggiunge parametri v4.6 come fissi
            # v4.6: usa sweep se abilitato, altrimenti valore fisso dal simulatore
            _sk = getattr(self, '_stat_sweep_keys', set())
            if 'adapt_baseline_enable' not in _sk:
                fixed['adapt_baseline_enable'] = bool(getattr(self,'_sv46_adapt_en',None) and self._sv46_adapt_en.get())
            if 'adapt_baseline_offset' not in _sk:
                fixed['adapt_baseline_offset'] = float(self._sv['adapt_offset'].get()) if 'adapt_offset' in self._sv else 3.0
            if 'flat_wait_enable' not in _sk:
                fixed['flat_wait_enable']  = bool(getattr(self,'_sv46_flat_en',None) and self._sv46_flat_en.get())
            if 'flat_wait_samples' not in _sk:
                fixed['flat_wait_samples'] = int(float(self._sv['flat_samples'].get())) if 'flat_samples' in self._sv else 5
            if 'flat_wait_toll' not in _sk:
                fixed['flat_wait_toll']    = float(self._sv['flat_toll'].get()) if 'flat_toll' in self._sv else 0.5
            fixed['detection_start_deg'] = float(self._sv['det_start'].get()) if 'det_start' in self._sv else 0.0  # *** v4.9 ***
        except ValueError as e:
            messagebox.showerror("Parametri fissi", str(e)); return

        n_workers = max(1, int(self._pv_stat_workers.get() or max(1, __import__("os").cpu_count() - 1)))
        total     = len(combos) * len(paths)

        # Reset UI
        self._stat_tree.config(columns=())
        for row in self._stat_tree.get_children():
            self._stat_tree.delete(row)
        self._stat_ax1.cla(); self._stat_canvas.draw_idle()
        self._stat_prog_bar["value"]   = 0
        self._stat_prog_bar["maximum"] = 100
        self._pv_stat_best.set("\u23f3  Pre-caricamento file...")
        self._pv_stat_prog_txt.set(f"Caricamento dati...")
        for v in self._stat_prop_vars.values(): v.set("\u2026")
        self.update_idletasks()

        # ── Pre-carica file payload (file .db o SQLite) ──────────────
        try:
            file_payloads = []
            plc_scalars   = []
            if use_sqlite:
                # Sorgente SQLite: estrai BLOB dalla RAM
                sql_rows = getattr(self, "_stat_sql_rows", [])
                sel_rows = [sql_rows[i] for i in sel if i < len(sql_rows)]
                paths = [f"SQLite_row{r[0]}" for r in sel_rows]
                self._pv_stat_prog_txt.set(f"Estrazione {len(sel_rows)} righe SQLite...")
                self.update_idletasks()
                for r in sel_rows:
                    d   = weld_sqlite_load_row(r)
                    sc  = d["scalars"]; ar = d["arrays"]
                    n   = int(sc.get("iSamplesAcquired",0)) or len(ar.get("arSamples",[]))
                    raw_s = ar.get("arSamples",[])[:n] if n >= 3 else []
                    raw_a = ar.get("arAngles", [])[:n] if n >= 3 else []
                    fkw   = dict(min_angle_delta=float(sc.get("I_MinAngleDelta",0.5)),
                                 min_laser_delta=float(sc.get("I_MinLaserDelta",0.1)),
                                 max_samples=5000)
                    fkw.update(fixed)
                    _thi = ar.get("arThreshHigh",  [])[:n]
                    _tlo = ar.get("arThreshLow",   [])[:n]
                    _thn = ar.get("arThreshHighNeg",[])[:n]
                    _tln = ar.get("arThreshLowNeg", [])[:n]
                    _has_th = len(_thi) > 0 and any(v != 0 for v in _thi[:min(100,len(_thi))])
                    file_payloads.append((raw_s, raw_a, fkw,
                        _thi, _tlo, _thn, _tln, _has_th))
                    _min_cons = int(sc.get("I_MinConsecutive", 1))
                    _plc_found = (int(sc.get("iClustersValid",0)) >= 1 and
                                  int(sc.get("iConsecutiveCount",0)) >= _min_cons)
                    plc_scalars.append({
                        "fp": f"SQLite#{r[0]}",
                        "row": r,
                        "ts": str(r[1])[:16] if r[1] else "",
                        "db_num": r[2],
                        "weld_found": _plc_found,
                        "det_angle":  float(sc.get("rDetectedAtAngle", 0.0)),
                        "consec":     int(sc.get("iConsecutiveCount", 0)),
                        "min_cons":   _min_cons,
                        "peak":       float(sc.get("rPeakValue", 0.0)),
                    })
            else:
                # Sorgente file .db
                self._pv_stat_prog_txt.set(f"Parsing {len(paths)} file .db...")
                self.update_idletasks()
                for fp in paths:
                    data  = parse_db_file(fp)
                    sc    = data["scalars"]; ar = data["arrays"]
                    n     = int(sc.get("iSamplesAcquired",0)) or len(ar.get("arSamples",[]))
                    raw_s = ar.get("arSamples",[])[:n] if n >= 3 else []
                    raw_a = ar.get("arAngles", [])[:n] if n >= 3 else []
                    fkw   = dict(min_angle_delta=float(sc.get("I_MinAngleDelta",0.5)),
                                 min_laser_delta=float(sc.get("I_MinLaserDelta",0.1)),
                                 max_samples=5000)
                    fkw.update(fixed)
                    _ar2 = data["arrays"]
                    _thi = _ar2.get("arThreshHigh",  [])[:n]
                    _tlo = _ar2.get("arThreshLow",   [])[:n]
                    _thn = _ar2.get("arThreshHighNeg",[])[:n]
                    _tln = _ar2.get("arThreshLowNeg", [])[:n]
                    _has_th = len(_thi) > 0 and any(v != 0 for v in _thi[:min(100,len(_thi))])
                    file_payloads.append((raw_s, raw_a, fkw,
                        _thi, _tlo, _thn, _tln, _has_th))
                    _min_cons = int(sc.get("I_MinConsecutive", 1))
                    _plc_found = (int(sc.get("iClustersValid",0)) >= 1 and
                                  int(sc.get("iConsecutiveCount",0)) >= _min_cons)
                    plc_scalars.append({
                        "fp": fp,
                        "row": None,
                        "ts": "",
                        "db_num": int(sc.get("I_DB_Number", 0)) or 0,
                        "weld_found": _plc_found,
                        "det_angle":  float(sc.get("rDetectedAtAngle", 0.0)),
                        "consec":     int(sc.get("iConsecutiveCount", 0)),
                        "min_cons":   _min_cons,
                        "peak":       float(sc.get("rPeakValue", 0.0)),
                    })
        except Exception as e:
            messagebox.showerror("Errore caricamento", str(e))
            self._pv_stat_best.set("\u2014  errore")
            return

        keys_tuple = tuple(keys)

        # Stato condiviso
        self._stat_paths       = paths
        self._stat_combos      = combos
        self._stat_plc_scalars = plc_scalars
        self._stat_keys       = keys
        self._stat_sweep_keys = set(keys)  # per check in _stat_update_results
        self._stat_total      = total
        self._stat_done       = 0
        self._stat_results    = {}
        self._stat_result_q   = _queue.Queue()
        self._stat_stop_event = threading.Event()
        self._stat_executor   = None
        self._stat_futures    = []

        self._pv_stat_best.set("\u23f3  In corso...")
        self._pv_stat_prog_txt.set(f"Avvio {n_workers} worker... (0 / {total:,})")
        self._btn_stat_run.config(state="disabled")
        self._btn_stat_stop.config(state="normal")
        self._stat_running = True

        # Generatore lazy — nessuna lista da 2M in RAM
        # Task = (fi, ci, combo_tuple) — solo ~55 bytes IPC invece di path+dict
        def _task_gen():
            for fi in range(len(paths)):
                for ci, combo in enumerate(combos):
                    yield fi, ci, tuple(combo)

        # Thread feeder con semaforo a finestra
        def _feeder():
            import concurrent.futures as cf
            window      = n_workers * 16
            sem         = threading.Semaphore(window)
            stop_ev     = self._stat_stop_event
            result_q    = self._stat_result_q
            submitted   = [0]
            completed   = [0]
            submit_done = threading.Event()

            try:
                executor = cf.ProcessPoolExecutor(
                    max_workers  = n_workers,
                    initializer  = _worker_init,
                    initargs     = (file_payloads,))
                self._stat_executor = executor

                for fi, ci, combo in _task_gen():
                    if stop_ev.is_set():
                        break
                    sem.acquire()
                    if stop_ev.is_set():
                        sem.release(); break
                    try:
                        fut = executor.submit(
                            _stat_sim_worker_v2, fi, combo, keys_tuple)
                        submitted[0] += 1
                    except RuntimeError:
                        sem.release(); break

                    def _done(f, _fi=fi, _ci=ci,
                               _sem=sem, _q=result_q,
                               _sub=submitted, _cmp=completed,
                               _sd=submit_done):
                        try:
                            res = f.result()
                        except Exception:
                            res = (False, 0.0, 0.0, 0.0)
                        _q.put((_fi, _ci, res))
                        _cmp[0] += 1
                        _sem.release()
                        if _sd.is_set() and _cmp[0] >= _sub[0]:
                            _q.put(("__done__", None, None))

                    fut.add_done_callback(_done)

            except Exception as e:
                result_q.put(("__error__", str(e), None))
            finally:
                submit_done.set()
                if completed[0] >= submitted[0]:
                    result_q.put(("__done__", None, None))
                try: executor.shutdown(wait=False)
                except Exception: pass

        self._stat_feeder_thread = threading.Thread(
            target=_feeder, daemon=True, name="StatFeeder")
        self._stat_feeder_thread.start()
        self._stat_poll_queue()

    def _stat_poll_queue(self):
        """Drena la queue dei risultati e aggiorna UI — schedulato via after()."""
        if not self._stat_running:
            return

        result_q = self._stat_result_q
        batch = 0

        while batch < 2000:
            try:
                fi, ci, res = result_q.get_nowait()
            except _queue.Empty:
                break

            if fi == "__done__":
                self.after(200, self._stat_drain_and_finish)
                return
            if fi == "__error__":
                self.app_log(f"Errore grid-search: {ci}", "err")
                break

            self._stat_results[(fi, ci)] = res
            self._stat_done += 1
            batch += 1
            self._stat_last_ci = ci  # traccia ultima combo ricevuta

        if batch > 0:
            pct = 100 * self._stat_done / max(self._stat_total, 1)
            self._stat_prog_bar["value"] = pct
            # *** Mostra parametri della combo corrente in lavorazione
            _ci = getattr(self, '_stat_last_ci', 0)
            _combos = getattr(self, '_stat_combos', [])
            _keys   = getattr(self, '_stat_keys',   [])
            if _keys and _combos and _ci < len(_combos):
                _swp = getattr(self, '_stat_swp', {})
                _parts = []
                for _k, _v in zip(_keys, _combos[_ci]):
                    _is_int = _swp.get(_k, {}).get('int', False)
                    _parts.append(f"{_k}={'%d'%int(_v) if _is_int else '%.3g'%_v}")
                _combo_str = '  '.join(_parts)
                self._pv_stat_prog_txt.set(
                    f"{self._stat_done:,} / {self._stat_total:,}  ({pct:.0f}%)  —  {_combo_str}")
            else:
                self._pv_stat_prog_txt.set(
                    f"{self._stat_done:,} / {self._stat_total:,}  ({pct:.0f}%)")
            prev_pct = 100 * (self._stat_done - batch) / max(self._stat_total, 1)
            if int(pct / 2) > int(prev_pct / 2):
                try:
                    self._stat_update_results()
                except Exception as e:
                    # Non bloccare mai il polling — loggare silenziosamente
                    try: self.app_log(f"[stat] update_results error: {e}", "warn")
                    except Exception: pass

        # Controlla anche per conteggio reale (fallback se __done__ manca)
        if self._stat_done >= self._stat_total and self._stat_total > 0:
            self._stat_finish()
            return

        self._stat_poll_id = self.after(150, self._stat_poll_queue)

    def _disc_load_row(self, ps_row):
        """Carica dati da un dict plc_scalars nel viewer e naviga al simulatore."""
        fp = ps_row.get("fp","")
        try:
            if fp.startswith("SQLite#"):
                row = ps_row.get("row")
                if row is None:
                    messagebox.showwarning("Carica", "Riga SQLite non disponibile.")
                    return
                data = weld_sqlite_load_row(row)
                self.db_data = data
                fname = ps_row.get("ts","") or fp
                self.lbl_file.config(text=f"\u2504 {fname}")
            else:
                data = parse_db_file(fp)
                self.db_data = data
                import os as _os
                self.lbl_file.config(text=f"\u2504 {_os.path.basename(fp)}")
            self._update_results_panel()
            self._preload_sim_params()        # 1. parametri originali DB
            self._apply_stat_best_to_sim()    # 2. sovrascrive con best combo grid search
            self._recompute()
            self._update_raw_tab()
            self._run_simulation()            # 3. simula con parametri corretti
            try: self.nb.select(1)
            except Exception: pass
            try: self._sim_outer_nb.select(1)
            except Exception: pass
            try: self.sim_nb.select(0)
            except Exception: pass
        except Exception as e:
            messagebox.showerror("Carica", str(e))

    def _disc_context_menu(self, event, tree, disc_rows):
        """Menu contestuale tasto destro per entrambe le tabelle discordanze."""
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        idx = int(iid)
        if idx >= len(disc_rows):
            return
        row = disc_rows[idx]
        # Cerca il dict plc_scalars corrispondente
        plc_sc = getattr(self, "_stat_plc_scalars", [])
        ps = None
        for p in plc_sc:
            if p.get("fp") == row.get("fp"):
                ps = p; break
        if ps is None:
            # fallback: costruisci ps minimale da row
            ps = {"fp": row.get("fp",""), "row": None, "ts": ""}
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Carica nel Simulatore",
                         command=lambda: self._disc_load_row(ps))
        menu.add_separator()
        menu.add_command(label=f"File: {row.get('fname','')[:60]}", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


    def _stat_go_discordanze(self):
        """Porta il focus sul simulatore, sub-tab Discordanze e popola tabella+grafico."""
        disc_rows = getattr(self, "_disc_rows", [])
        if not disc_rows:
            return
        # Vai al tab Simulatore (index 1 nel notebook principale)
        try:
            self.nb.select(1)
        except Exception:
            pass
        # Poi al sub-tab Grafici (index 1 del sim_outer_nb)
        try:
            self._sim_outer_nb.select(1)
        except Exception:
            pass
        # Poi al sub-tab Discordanze (ultimo del sim_nb)
        try:
            tabs = self.sim_nb.tabs()
            for t in tabs:
                if "Discordanze" in self.sim_nb.tab(t, "text"):
                    self.sim_nb.select(t)
                    break
        except Exception:
            pass
        self._stat_fill_disc_tab(disc_rows)

    def _stat_fill_disc_tab(self, disc_rows):
        """Popola tabella e grafico nel sub-tab Discordanze."""
        tree = self._disc_tree
        for item in tree.get_children():
            tree.delete(item)
        for i, r in enumerate(disc_rows):
            if not r["is_disc"]:
                continue
            plc_s = "✓" if r["plc_found"] else "✗"
            sim_s = "✓" if r["sim_found"] else "✗"
            da    = f"{r['delta_ang']:.2f}°" if r["delta_ang"] is not None else "—"
            pa    = f"{r['plc_ang']:.2f}°"   if r["plc_found"]   else "—"
            sa    = f"{r['sim_ang']:.2f}°"   if r["sim_found"]   else "—"
            tree.insert("", "end", iid=str(i), tags=("disc",), values=(
                r["fname"], plc_s, sim_s, pa, sa, da,
                str(r["plc_cons"]) if r["plc_found"] else "—",
                str(r["sim_cons"]) if r["sim_found"] else "—",
            ))
        self._disc_fill_chart(disc_rows)
        # Popola anche la tabella nel tab Statistiche
        try:
            self._stat_disc_fill(disc_rows)
        except Exception:
            pass

    def _disc_fill_chart(self, disc_rows):
        """Grafico: PLC vs Sim angolo e consecutivi."""
        ax1 = self._disc_ax1; ax2 = self._disc_ax2
        ax1.cla(); ax2.cla()

        xs = list(range(len(disc_rows)))
        plc_angs = [r["plc_ang"] if r["plc_found"] else None for r in disc_rows]
        sim_angs = [r["sim_ang"] if r["sim_found"] else None for r in disc_rows]
        plc_cons = [r["plc_cons"] if r["plc_found"] else None for r in disc_rows]
        disc_mask = [r["is_disc"] for r in disc_rows]

        # Angolo plot
        xp = [i for i,v in enumerate(plc_angs) if v is not None]
        yp = [v for v in plc_angs if v is not None]
        xs_ = [i for i,v in enumerate(sim_angs) if v is not None]
        ys_ = [v for v in sim_angs if v is not None]
        if xp: ax1.scatter(xp, yp, color=OK_CLR,   s=40, label="PLC",  zorder=3)
        if xs_: ax1.scatter(xs_, ys_, color=CIAN_CLR, s=25, label="Sim", marker="x", zorder=4)
        disc_xs = [i for i,d in enumerate(disc_mask) if d]
        if disc_xs:
            all_y = [v for v in yp + ys_ if v is not None]
            y0 = min(all_y) - 1 if all_y else 0
            y1 = max(all_y) + 1 if all_y else 1
            ax1.vlines(disc_xs, y0, y1, colors=WARN_CLR,
                       linewidth=1.0, alpha=0.5, linestyle="--")
        self._style_axes(ax1, "Angolo rilevato: PLC vs Sim", "File #", "Angolo (°)")
        if xp or xs_:
            ax1.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        # Consecutivi plot
        if xp:
            ycons = [v if v is not None else 0 for v in plc_cons]
            ax2.bar(xp, ycons, color=OK_CLR, alpha=0.6, label="PLC", width=0.4)
        disc_ys = [1 if d else 0 for d in disc_mask]
        if disc_xs:
            cons_vals = [plc_cons[i] or 0 for i in range(len(disc_rows)) if disc_rows[i]["is_disc"]]
            ystar = max(cons_vals) + 1 if cons_vals else 1
            ax2.scatter(disc_xs, [ystar]*len(disc_xs),
                        color=WARN_CLR, s=60, marker="*", zorder=5, label="⚠")
        self._style_axes(ax2, "Consecutivi PLC al rilevamento", "File #", "Consecutivi")
        if xp:
            ax2.legend(fontsize=8, facecolor=PANEL_BG, edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        self._disc_fig.tight_layout(pad=2.0)
        self._disc_canvas.draw_idle()

    def _disc_on_select(self, event=None):
        """Doppio click riga discordanze simulatore: carica il file."""
        iid = self._disc_tree.identify_row(event.y) if event else ""
        if not iid:
            sel = self._disc_tree.selection()
            if not sel: return
            iid = sel[0]
        idx = int(iid)
        disc_rows = getattr(self, "_disc_rows", [])
        if idx >= len(disc_rows):
            return
        row = disc_rows[idx]
        fp  = row.get("fp", "")
        if not fp or fp.startswith("SQLite#"):
            return
        try:
            data = parse_db_file(fp)
            self.db_data = data
            import os
            self.lbl_file.config(text=f"┄ {os.path.basename(fp)}")
            self._update_results_panel()
            self._preload_sim_params()
            self._apply_stat_best_to_sim()
            self._recompute()
            self._update_raw_tab()
            self._run_simulation()
            # Vai al tab Segnale & Detection
            try: self._sim_outer_nb.select(1)
            except Exception: pass
            try: self.sim_nb.select(0)
            except Exception: pass
        except Exception as e:
            self.app_log(f"Discordanze load error: {e}", "err")
            messagebox.showerror("Disc load", str(e))


    def _stat_disc_fill(self, disc_rows):
        """Popola la tabella discordanze nel tab Statistiche."""
        tree = self._stat_disc_tree
        for item in tree.get_children():
            tree.delete(item)
        for i, r in enumerate(disc_rows):
            if not r["is_disc"]:
                continue
            plc_s = "✓" if r["plc_found"] else "✗"
            sim_s = "✓" if r["sim_found"] else "✗"
            da    = f"{r['delta_ang']:.2f}°" if r["delta_ang"] is not None else "—"
            pa    = f"{r['plc_ang']:.2f}°"   if r["plc_found"] else "—"
            sa    = f"{r['sim_ang']:.2f}°"   if r["sim_found"] else "—"
            tree.insert("", "end", iid=str(i), tags=("disc",), values=(
                r["fname"], plc_s, sim_s, pa, sa, da,
                str(r["plc_cons"]) if r["plc_found"] else "—",
            ))

    def _stat_disc_dblclick(self, event=None):
        """Doppio click sulla tabella stat discordanze: carica file e apre simulatore."""
        iid = self._stat_disc_tree.identify_row(event.y) if event else ""
        if not iid:
            sel = self._stat_disc_tree.selection()
            if not sel: return
            iid = sel[0]
        idx = int(iid)
        disc_rows = getattr(self, "_disc_rows", [])
        if idx >= len(disc_rows):
            return
        row = disc_rows[idx]
        fp  = row.get("fp", "")
        if not fp or fp.startswith("SQLite#"):
            return
        try:
            data = parse_db_file(fp)
            self.db_data = data
            import os
            self.lbl_file.config(text=f"┄ {os.path.basename(fp)}")
            self._update_results_panel()
            self._preload_sim_params()
            self._apply_stat_best_to_sim()
            self._recompute()
            self._update_raw_tab()
            self._run_simulation()
            # Naviga al tab Simulatore
            self.nb.select(1)
            # Sub-tab Grafici
            try: self._sim_outer_nb.select(1)
            except Exception: pass
            # Tab Segnale & Detection
            try: self.sim_nb.select(0)
            except Exception: pass
        except Exception as e:
            self.app_log(f"Disc dblclick error: {e}", "err")
            messagebox.showerror("Disc dblclick", str(e))


    def _stat_drain_and_finish(self):
        """Svuota la coda residua dopo il segnale __done__ e finalizza."""
        result_q = self._stat_result_q
        while True:
            try:
                fi, ci, res = result_q.get_nowait()
            except _queue.Empty:
                break
            if fi in ("__done__", "__error__"):
                break
            self._stat_results[(fi, ci)] = res
            self._stat_done += 1

        pct = 100 * self._stat_done / max(self._stat_total, 1)
        self._stat_prog_bar["value"] = pct
        self._pv_stat_prog_txt.set(
            f"{self._stat_done:,} / {self._stat_total:,}  ({pct:.0f}%)")
        self._stat_finish()

    def _stat_update_results(self):
        """Calcola best set e aggiorna card + tabella + grafico."""
        n_files  = len(self._stat_paths)
        n_combos = len(self._stat_combos)
        if n_combos == 0 or n_files == 0:
            return

        pcts      = np.zeros(n_combos)
        snr_meds  = np.zeros(n_combos)
        snr_mins  = np.full(n_combos, np.inf)
        sig_facts = np.zeros(n_combos)  # sigma_factor per combo
        min_abss  = np.zeros(n_combos)  # min_abs_dev per combo
        # Indici delle chiavi di scoring nelle combo
        _keys = self._stat_keys
        _sf_i = _keys.index('sigma_factor')   if 'sigma_factor' in _keys else -1
        _ma_i = _keys.index('min_abs_dev')     if 'min_abs_dev'  in _keys else -1
        # Valori fissi (se non in sweep)
        def _fix(key, default):
            try: return float(self._stat_swp.get(key, {}).get('fix').get())
            except Exception: return default
        _sf_fix = _fix('sigma_factor', 3.0) if _sf_i < 0 else 0.0
        _ma_fix = _fix('min_abs_dev',  1.5) if _ma_i < 0 else 0.0
        best_score = (-1.0,) * 4; best_ci = 0
        best_pct = -1.0; best_snr_med = 0.0; best_snr_min = 0.0

        for ci in range(n_combos):
            found_l, snr_l = [], []
            for fi in range(n_files):
                r = self._stat_results.get((fi, ci))
                if r is None: continue
                found, snr, _, det_angle = r
                found_l.append(found)
                if found: snr_l.append(snr)
            if not found_l: continue
            pct = 100.0 * sum(found_l) / len(found_l)
            pcts[ci]     = pct
            snr_meds[ci] = float(np.median(snr_l)) if snr_l else 0.0
            snr_mins[ci] = float(np.min(snr_l))    if snr_l else 0.0
            cmb = self._stat_combos[ci]
            sig_facts[ci] = float(cmb[_sf_i]) if _sf_i >= 0 else _sf_fix
            min_abss[ci]  = float(cmb[_ma_i]) if _ma_i >= 0 else _ma_fix
            # Criteri: pct desc, snr_med desc, sigma_factor desc, min_abs_dev desc
            score = (pct, snr_meds[ci], sig_facts[ci], min_abss[ci])
            if score > best_score:
                best_score = score; best_ci = ci
                best_pct = pct
                best_snr_med = snr_meds[ci]; best_snr_min = snr_mins[ci]

        if best_pct < 0: return

        combo  = self._stat_combos[best_ci]
        params = dict(zip(self._stat_keys, combo))

        # *** v4.3.76 *** Aggiunge parametri NON in sweep (chk=False) con il valore fisso.
        # Cosi _stat_prop_vars li mostra e _stat_apply_to_sim li applica tutti.
        for _k, _d in self._stat_swp.items():
            if not _d["chk"].get() and _k not in params:
                try:
                    _raw = _d["fix"].get()
                    params[_k] = int(_raw) if _d["int"] else float(_raw)
                except (ValueError, TypeError):
                    pass

        PARAM_LABELS = {
            "sigma_factor":           "SigmaFactor",
            "min_abs_dev":            "MinAbsDev",
            "hyst_sigmas":            "HystSigmas",
            "window_deg":             "Window\u00b0",
            "min_consecutive":        "MinConsec",
            "flat_wait_enable":       "FlatEn",
            "flat_wait_samples":      "FlatSamp",
            "flat_wait_toll":         "FlatToll",
        }
        self._stat_prop_vars["pct"].set(f"{best_pct:.0f}%")
        for key in PARAM_LABELS:
            sv = self._stat_prop_vars.get(key)
            if sv is None: continue
            if key in params:
                v = params[key]
                is_int = self._stat_swp.get(key, {}).get("int", False)
                val_str = str(int(v)) if is_int else f"{v:.3g}"
                # *** v4.3.76 *** indica se il valore viene dallo sweep o e' fisso
                in_sweep = self._stat_swp.get(key, {}).get("chk") and \
                           self._stat_swp[key]["chk"].get()
                sv.set(val_str if in_sweep else f"{val_str} (fisso)")
            else:
                sv.set("\u2014")

        parts = []
        for k, v in params.items():
            is_int = self._stat_swp.get(k, {}).get("int", False)
            parts.append(f"{PARAM_LABELS.get(k,k)}={'%d'%int(v) if is_int else '%.3g'%v}")
        self._pv_stat_best.set("  ".join(parts))
        self._stat_last_best = {"params": params, "pct": best_pct,
                                 "snr_med": best_snr_med, "snr_min": best_snr_min}

        # Ordinati: % trovati desc, SNR med desc, sigma_factor desc, min_abs_dev desc
        ranked = sorted(
            [(ci, pcts[ci], snr_meds[ci], snr_mins[ci], sig_facts[ci], min_abss[ci])
             for ci in range(n_combos) if pcts[ci] > 0],
            key=lambda x: (x[1], x[2], x[4], x[5]), reverse=True)

        cols = self._stat_keys + ["% trovati", "SNR med.", "SNR min.", "SigmaF", "MinAbs"]
        self._stat_tree.config(columns=cols)
        for c in cols:
            w = 65 if c in ("% trovati","SNR med.","SNR min.","SigmaF","MinAbs") else 80
            self._stat_tree.heading(c, text=c)
            self._stat_tree.column(c, width=w, anchor="center")
        for row in self._stat_tree.get_children():
            self._stat_tree.delete(row)
        for ri, (ci, pct, snrm, snrn, sf, ma) in enumerate(ranked):
            cmb  = self._stat_combos[ci]
            vals = []
            for k, v in zip(self._stat_keys, cmb):
                vals.append(str(int(v)) if self._stat_swp[k]["int"] else f"{v:.3g}")
            vals += [f"{pct:.0f}%", f"{snrm:.2f}", f"{snrn:.2f}",
                     f"{sf:.2g}", f"{ma:.3g}"]
            tag = "best" if ri == 0 else ("good" if pct >= 80 else "poor")
            self._stat_tree.insert("", "end", values=tuple(vals), tags=(tag,))

        # Grafico adattivo
        self._stat_ax1.cla()
        n_sweep = len(self._stat_keys)

        if n_sweep == 0:
            self._style_axes(self._stat_ax1, "Nessun parametro in sweep","","")

        elif n_sweep == 1:
            key  = self._stat_keys[0]
            from collections import defaultdict
            agg  = defaultdict(list)
            for ci_v, combo_v in enumerate(self._stat_combos):
                agg[round(combo_v[0], 6)].append(pcts[ci_v])
            xs = sorted(agg.keys())
            ys = [float(np.mean(agg[x])) for x in xs]
            pt_colors = [OK_CLR if y>=100 else (WARN_CLR if y>0 else WELD_CLR) for y in ys]
            self._stat_ax1.plot(xs, ys, color=MUTED_CLR, linewidth=1, alpha=0.5, zorder=1)
            self._stat_ax1.scatter(xs, ys, c=pt_colors, s=60, zorder=3)
            if params.get(key) is not None:
                self._stat_ax1.scatter([params[key]], [best_pct],
                    color=STAT_CLR, s=200, marker="*", zorder=5,
                    label=f"best={params[key]:.3g}")
            self._stat_ax1.axhline(100, color=MUTED_CLR, linewidth=0.8,
                                    linestyle="--", alpha=0.6)
            self._stat_ax1.set_ylim(-5, 108)
            self._style_axes(self._stat_ax1,
                f"% trovati vs {PARAM_LABELS.get(key,key)}",
                PARAM_LABELS.get(key, key), "% trovati")
            self._stat_ax1.legend(fontsize=8, facecolor=PANEL_BG,
                                   edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)

        elif n_sweep == 2:
            k0, k1 = self._stat_keys[0], self._stat_keys[1]
            v0s = sorted(set(round(c[0],6) for c in self._stat_combos))
            v1s = sorted(set(round(c[1],6) for c in self._stat_combos))
            grid = np.zeros((len(v1s), len(v0s)))
            for ci_v, combo_v in enumerate(self._stat_combos):
                j = v0s.index(round(combo_v[0],6))
                i = v1s.index(round(combo_v[1],6))
                grid[i,j] = pcts[ci_v]
            ext = [v0s[0], v0s[-1], v1s[0], v1s[-1]]
            im  = self._stat_ax1.imshow(grid, aspect="auto", origin="lower",
                    extent=ext, cmap="RdYlGn", vmin=0, vmax=100,
                    interpolation="nearest")
            try: self._stat_fig.colorbar(im, ax=self._stat_ax1, label="% trovati")
            except Exception: pass
            if params.get(k0) is not None and params.get(k1) is not None:
                self._stat_ax1.scatter([params[k0]], [params[k1]],
                    color=STAT_CLR, s=200, marker="*", zorder=5, label="best")
                self._stat_ax1.legend(fontsize=8, facecolor=PANEL_BG,
                                       edgecolor=BORDER_CLR, labelcolor=TEXT_CLR)
            self._style_axes(self._stat_ax1,
                f"% trovati — {PARAM_LABELS.get(k0,k0)} vs {PARAM_LABELS.get(k1,k1)}",
                PARAM_LABELS.get(k0,k0), PARAM_LABELS.get(k1,k1))

        else:
            top40 = sorted(
                [(ci, pcts[ci], snr_meds[ci])
                 for ci in range(n_combos) if pcts[ci] > 0],
                key=lambda x: (x[1], x[2]), reverse=True)[:40]
            if top40:
                xs_r  = list(range(1, len(top40)+1))
                ys_p  = [t[1] for t in top40]
                sz    = [max(20, t[2]*25) for t in top40]
                sc    = self._stat_ax1.scatter(xs_r, ys_p, c=ys_p,
                    cmap="RdYlGn", s=sz, vmin=0, vmax=100, alpha=0.85, zorder=3)
                try: self._stat_fig.colorbar(sc, ax=self._stat_ax1, label="% trovati")
                except Exception: pass
                self._stat_ax1.axhline(100, color=MUTED_CLR,
                    linewidth=0.8, linestyle="--", alpha=0.5)
                self._stat_ax1.set_ylim(-5, 108)
                self._style_axes(self._stat_ax1,
                    f"% trovati (bolla=SNR) — top {len(top40)} combinazioni",
                    "rank", "% trovati")

        self._stat_fig.tight_layout(pad=2.0)
        self._stat_canvas.draw_idle()

        # ── Discordanze PLC vs Sim ────────────────────────────────────────
        plc_sc = getattr(self, "_stat_plc_scalars", [])
        if plc_sc and best_pct >= 0:
            best_ci_val = best_ci
            disc_rows = []
            for fi in range(len(plc_sc)):
                ps = plc_sc[fi]
                r  = self._stat_results.get((fi, best_ci_val))
                if r is None:
                    continue
                found_sim, snr_sim, _, sim_ang = r
                plc_found = ps["weld_found"]
                plc_ang   = ps["det_angle"]
                plc_cons  = ps["consec"]
                sim_cons  = 0   # non disponibile nel worker (aggiungeremo sotto se serve)
                fp        = ps["fp"]
                if fp.startswith("SQLite#"):
                    _ts  = ps.get("ts", "")
                    _db  = ps.get("db_num", "")
                    fname = f"DB{_db}  {_ts}" if _db else fp
                else:
                    _db  = ps.get("db_num", 0)
                    _bn  = fp.split("\\")[-1].split("/")[-1]
                    fname = f"DB{_db}  {_bn}" if _db else _bn

                ang_delta = abs(plc_ang - sim_ang) if (plc_found and found_sim) else None
                is_disc = (bool(plc_found) != bool(found_sim)) or \
                          (ang_delta is not None and ang_delta > 1.0)

                disc_rows.append({
                    "fname": fname, "fp": fp,
                    "plc_found": plc_found, "sim_found": found_sim,
                    "plc_ang": plc_ang, "sim_ang": sim_ang,
                    "delta_ang": ang_delta,
                    "plc_cons": plc_cons, "sim_cons": sim_cons,
                    "is_disc": is_disc,
                })

            n_disc = sum(1 for r in disc_rows if r["is_disc"])
            n_tot  = len(disc_rows)
            self._disc_rows = disc_rows

            if n_disc == 0:
                self._pv_stat_disc.set(f"\u2713 Nessuna discordanza su {n_tot} file")
                try: self._stat_disc_label.config(fg=OK_CLR)
                except Exception: pass
            else:
                self._pv_stat_disc.set(
                    f"\u26a0 {n_disc} discordanze su {n_tot} file "
                    f"({100*n_disc/max(n_tot,1):.0f}%)  \u2014  clicca per dettagli")
        else:
            self._disc_rows = []
        # Auto-aggiorna entrambe le tabelle discordanze
        try:
            self._stat_disc_fill(self._disc_rows)
        except Exception:
            pass
        try:
            self._stat_fill_disc_tab(self._disc_rows)
        except Exception:
            pass


    def _stat_finish(self):
        """Tutte le simulazioni completate."""
        try:
            self._stat_update_results()
        except Exception as e:
            try: self.app_log(f"[stat] finish error: {e}", "warn")
            except Exception: pass
        self._stat_prog_bar["value"] = 100
        self._pv_stat_prog_txt.set(
            f"\u2713 Completato \u2014 {self._stat_done:,} / "
            f"{self._stat_total:,} simulazioni")
        self._stat_stop(silent=True)

    def _stat_stop(self, silent=False):
        """Ferma il grid-search: segnala stop event e chiude executor."""
        self._stat_running = False
        if hasattr(self, "_stat_stop_event") and self._stat_stop_event:
            self._stat_stop_event.set()
        if self._stat_poll_id:
            self.after_cancel(self._stat_poll_id)
            self._stat_poll_id = None
        if self._stat_executor:
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                try: self._stat_executor.shutdown(wait=False, cancel_futures=True)
                except (TypeError, OSError, BrokenPipeError): pass
                try: self._stat_executor.shutdown(wait=False)
                except (OSError, BrokenPipeError): pass
            self._stat_executor = None
        self._btn_stat_run.config(state="normal")
        self._btn_stat_stop.config(state="disabled")
        if not silent and self._stat_done < self._stat_total:
            self._pv_stat_prog_txt.set(
                f"Interrotto a {self._stat_done:,} / {self._stat_total:,}")

    def _show_stat_col(self, params):
        """Popola e mostra la colonna Stats nelle righe Soglie/Cluster."""
        if not hasattr(self, '_sv_stat'): return
        _key_map = {"sigma_factor": "sig_f", "min_abs_dev": "min_abs",
                    "hyst_sigmas": "hyst", "window_deg": "win",
                    "min_consecutive": "min_cons", "max_consecutive": "max_cons"}
        for pk, sk in _key_map.items():
            if pk in params and sk in self._sv_stat:
                v = params[pk]
                is_int = sk in ("min_cons", "max_cons")
                self._sv_stat[sk].set(str(int(v)) if is_int else f"{v:.3g}")
        # Mostra intestazione e label Stats su tutte le righe
        if hasattr(self, '_stat_col_hdr'):
            try: self._stat_col_hdr.pack(side="left", padx=1)
            except Exception: pass
        # Mostra tutti i widget Label con _key in _sv_stat
        def _show_children(widget):
            for ch in widget.winfo_children():
                if hasattr(ch, '_key') and ch._key in self._sv_stat:
                    try: ch.pack(side="left", padx=1)
                    except Exception: pass
                _show_children(ch)
        try:
            for w in self.winfo_children():
                _show_children(w)
        except Exception: pass

    def _apply_stat_best_to_sim(self):
        """Applica silenziosamente i parametri del best combo al simulatore.
        Chiamata dopo _preload_sim_params() per sovrascrivere i param DB
        con quelli ottimizzati dal grid search. Se non c'e' un best combo,
        non fa nulla (i parametri DB restano invariati).
        """
        p = getattr(self, "_stat_last_best", {}).get("params", {})
        if not p:
            return  # nessun grid search eseguito, usa parametri DB
        mapping = {"sigma_factor": "sig_f", "min_abs_dev": "min_abs",
                   "hyst_sigmas": "hyst", "window_deg": "win",
                   "min_consecutive": "min_cons"}
        for k, sv_k in mapping.items():
            if k in p and sv_k in self._sv:
                v = p[k]
                self._sv[sv_k].set(
                    str(int(v)) if self._stat_swp.get(k, {}).get("int")
                    else f"{v:.4g}")
        try:
            mc = int(self._stat_fix["max_consecutive"].get() or 60)
            if "max_cons" in self._sv: self._sv["max_cons"].set(str(mc))
        except Exception: pass
        try:
            sw = bool(int(self._stat_fix["stop_on_weld"].get() or 1))
            if hasattr(self, "_sim_stop_var"): self._sim_stop_var.set(sw)
        except Exception: pass
        try:
            pol = int(self._stat_fix["peak_polarity"].get())
            if hasattr(self, "_sim_polarity_var"): self._sim_polarity_var.set(pol)
        except Exception: pass
        # *** v4.6 *** Applica parametri v4.6 se presenti nel best combo
        try:
            if 'adapt_offset' in self._sv and 'adapt_baseline_offset' in p:
                self._sv['adapt_offset'].set(f"{float(p['adapt_baseline_offset']):.4g}")
            if 'flat_samples' in self._sv and 'flat_wait_samples' in p:
                self._sv['flat_samples'].set(str(int(p['flat_wait_samples'])))
            if 'flat_toll' in self._sv and 'flat_wait_toll' in p:
                self._sv['flat_toll'].set(f"{float(p['flat_wait_toll']):.4g}")
            if hasattr(self,'_sv46_adapt_en') and 'adapt_baseline_enable' in p:
                self._sv46_adapt_en.set(bool(p['adapt_baseline_enable']))
            if hasattr(self,'_sv46_flat_en') and 'flat_wait_enable' in p:
                self._sv46_flat_en.set(bool(p['flat_wait_enable']))
        except Exception: pass
        # Mostra colonna Stats con i valori applicati
        try: self._show_stat_col(p)
        except Exception: pass

    def _stat_apply_to_sim(self):
        p = self._stat_last_best.get("params", {})
        if not p:
            messagebox.showwarning("Nessun risultato", "Avvia prima il grid search."); return
        # *** v4.3.76 *** Applica sweep + fissi. params ora contiene entrambi.
        mapping = {"sigma_factor":"sig_f","min_abs_dev":"min_abs",
                   "hyst_sigmas":"hyst","window_deg":"win","min_consecutive":"min_cons"}
        applied = []
        for k, sv_k in mapping.items():
            if k in p and sv_k in self._sv:
                v = p[k]
                self._sv[sv_k].set(str(int(v)) if self._stat_swp.get(k,{}).get("int") else f"{v:.4g}")
                applied.append(k)
        # Applica anche i parametri fissi (max_consec, stop_on_weld, polarity)
        try:
            mc = int(self._stat_fix["max_consecutive"].get() or 60)
            if "max_cons" in self._sv: self._sv["max_cons"].set(str(mc)); applied.append("max_consecutive")
        except Exception: pass
        try:
            sw = bool(int(self._stat_fix["stop_on_weld"].get() or 1))
            if hasattr(self, "_sim_stop_var"): self._sim_stop_var.set(sw); applied.append("stop_on_weld")
        except Exception: pass
        try:
            pol = int(self._stat_fix["peak_polarity"].get())
            if hasattr(self, "_sim_polarity_var"): self._sim_polarity_var.set(pol); applied.append("peak_polarity")
        except Exception: pass
        # *** v4.6 *** Applica parametri v4.6
        try:
            if 'adapt_offset' in self._sv and 'adapt_baseline_offset' in p:
                self._sv['adapt_offset'].set(f"{float(p['adapt_baseline_offset']):.4g}"); applied.append('adapt_baseline_offset')
            if 'flat_samples' in self._sv and 'flat_wait_samples' in p:
                self._sv['flat_samples'].set(str(int(p['flat_wait_samples']))); applied.append('flat_wait_samples')
            if 'flat_toll' in self._sv and 'flat_wait_toll' in p:
                self._sv['flat_toll'].set(f"{float(p['flat_wait_toll']):.4g}"); applied.append('flat_wait_toll')
            if hasattr(self,'_sv46_adapt_en') and 'adapt_baseline_enable' in p:
                self._sv46_adapt_en.set(bool(p['adapt_baseline_enable']))
            if hasattr(self,'_sv46_flat_en') and 'flat_wait_enable' in p:
                self._sv46_flat_en.set(bool(p['flat_wait_enable']))
        except Exception: pass
        if applied:
            try: self._show_stat_col(p)
            except Exception: pass
            messagebox.showinfo("Applicato",
                f"{len(applied)} parametri applicati al Simulatore.")

    def _stat_copy_params(self):
        p = self._stat_last_best.get("params", {})
        if not p:
            messagebox.showwarning("Nessun risultato", "Avvia prima il grid search."); return
        lines = []
        for k, v in p.items():
            lines.append(f"{k} := {int(v) if self._stat_swp.get(k,{}).get('int') else v:.4g}")
        txt = "\n".join(lines)
        self.clipboard_clear(); self.clipboard_append(txt)
        messagebox.showinfo("Copiato", txt)


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = WeldViewerApp()
    app.mainloop()
    def _autoexp_on_trigger(self, row_idx: int, db_num: int):
        """Trigger scattato per db_num: leggi DB completo, salva, aggiorna UI."""
        ms  = self._autoexp_ms
        omap = self._autoexp_omap
        try:
            raw = bytearray(self._autoexp_db_size)
            # Leggi a blocchi (Snap7 max ~65KB per chiamata)
            block = 512
            for off in range(0, self._autoexp_db_size, block):
                sz  = min(block, self._autoexp_db_size - off)
                raw[off:off+sz] = self._autoexp_client.db_read(db_num, off, sz)

            decoded = plc_decode_db(raw, omap, ms)
            sc = decoded["scalars"]

            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seq = self._autoexp_export_count + self._autoexp_reject_count + 1

            # Determina saldatura trovata
            clusters_valid    = int(sc.get("iClustersValid",     0))
            consecutive_count = int(sc.get("iConsecutiveCount",  0))
            min_consecutive   = int(sc.get("I_MinConsecutive",   1))
            weld_found = (clusters_valid >= 1 and consecutive_count >= min_consecutive)

            if weld_found:
                self._autoexp_export_count += 1
                dest_dir = self._pv_autoexp_path.get().strip()
                prefix = "OK"
                self._ae_db_rows[row_idx]["ok"] += 1
            else:
                self._autoexp_reject_count += 1
                dest_dir = self._pv_autoexp_path_rej.get().strip()
                prefix = "SCARTO"
                self._ae_db_rows[row_idx]["rej"] += 1

            ts_file = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18]
            db_name  = f"WeldFind_DB{db_num}"
            filename  = f"{db_name}_{prefix}_{ts_file}_{seq:04d}.db"
            filepath  = os.path.join(dest_dir, filename)

            db_text = plc_generate_db_text(decoded, db_name, ms)

            # Salva file .db
            if self._pv_autoexp_save_file.get():
                os.makedirs(dest_dir, exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(db_text)

            # Salva SQLite
            if self._pv_autoexp_save_sql.get() and self._autoexp_sql_con:
                try:
                    decoded["timestamp"] = ts
                    rid = weld_sqlite_insert(
                        self._autoexp_sql_con, db_num, decoded, filename, ms)
                    self._plc_log_msg(f"  \u2502 SQLite row #{rid}\n")
                except Exception as sql_e:
                    self._plc_log_msg(f"  \u2502 SQLite err: {sql_e}\n", "warn")

            # Log
            peak      = sc.get("rPeakValue", 0)
            dev       = sc.get("rPeakDeviation", 0)
            det_angle = sc.get("rDetectedAtAngle", 0)
            pol       = int(sc.get("I_PeakPolarity", 0))
            pol_name  = {0:"Pos",1:"Neg",2:"Both"}.get(pol,"?")
            n_samp    = int(sc.get("iSamplesAcquired", 0))

            icon = "\u2713 BUONO" if weld_found else "\u2717 SCARTO"
            self._plc_log_msg(f"{icon}  DB{db_num} #{seq}: {filename}\n",
                              "ok" if weld_found else "warn")
            self._plc_log_msg(
                f"  {n_samp} camp. | clV={clusters_valid} cons={consecutive_count}"
                f"/{min_consecutive} | peak={peak:.3f} ang={det_angle:.1f}\u00b0 {pol_name}\n")

            # Aggiorna contatori riga
            rok = self._ae_db_rows[row_idx]["ok"]
            rrj = self._ae_db_rows[row_idx]["rej"]
            self._ae_db_rows[row_idx]["count"].set(f"\u2713{rok} \u2717{rrj}")

            # Contatori globali + SQLite info
            sql_info = ""
            if self._pv_autoexp_save_sql.get() and self._autoexp_sql_con:
                import sqlite3 as _sq3
                try:
                    _n = _sq3.connect(self._pv_autoexp_sql_path.get()).execute(
                        "SELECT COUNT(*) FROM acquisitions").fetchone()[0]
                    sql_info = f"  \U0001f5c4{_n}"
                except Exception: pass
            self._pv_autoexp_count.set(
                f"\u2713 {self._autoexp_export_count}  \u2717 {self._autoexp_reject_count}{sql_info}")
            self._pv_autoexp_last.set(f"{'\u2713' if weld_found else '\u2717'} DB{db_num} {ts[11:]}")
            self._pv_autoexp_status.set("\u25cf Monitoraggio attivo")

            # Carica nel viewer se questo DB ha il flag viewer abilitato
            if self._pv_autoexp_viewer.get() and self._ae_db_rows[row_idx]["viewer"].get():
                data = parse_db_file_from_text(db_text, filename)
                self.db_data = data
                self.lbl_file.config(text=f"\U0001f504 DB{db_num}: {filename}")
                self._update_results_panel()
                self._preload_sim_params()
                self._recompute()
                self._update_raw_tab()

            self.app_log(
                f"AE {'OK' if weld_found else 'SCARTO'} DB{db_num} #{seq}: {filename} "
                f"(clV={clusters_valid} cons={consecutive_count}/{min_consecutive})",
                "ok" if weld_found else "warn")

        except Exception as e:
            self._plc_log_msg(f"\u2717 Errore DB{db_num}: {e}\n", "err")
            self._pv_autoexp_status.set("\u25cf Monitoraggio attivo (errore)")
