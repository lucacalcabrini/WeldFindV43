"""
Genera icon.ico con stile line-art maschera da saldatura.
Richiede: pip install Pillow
"""
from PIL import Image, ImageDraw
import math, os

def draw_icon(size=256):
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    d   = ImageDraw.Draw(img)
    s   = size / 256
    ink = (30, 40, 60, 255)
    lw  = max(1, round(7 * s))
    lw2 = max(1, round(5 * s))
    lw3 = max(1, round(4 * s))

    # ── MASCHERA ─────────────────────────────────────────────────────────────
    mx0, my0 = int(28*s), int(18*s)
    mx1, my1 = int(168*s), int(148*s)
    r = int(14*s)
    d.rounded_rectangle([mx0, my0, mx1, my1], radius=r, outline=ink, width=lw)

    # orecchie laterali
    ear_w, ear_h = int(16*s), int(26*s)
    ear_y = int(48*s)
    d.rounded_rectangle([mx0-ear_w, ear_y, mx0, ear_y+ear_h],
                        radius=int(5*s), outline=ink, width=lw2)
    d.rounded_rectangle([mx1, ear_y, mx1+ear_w, ear_y+ear_h],
                        radius=int(5*s), outline=ink, width=lw2)

    # finestrino
    fw, fh = int(72*s), int(32*s)
    fx0 = (mx0+mx1)//2 - fw//2
    fy0 = my0 + int(26*s)
    fx1, fy1 = fx0+fw, fy0+fh
    d.rounded_rectangle([fx0, fy0, fx1, fy1], radius=int(5*s),
                        outline=ink, width=lw2)
    # righe diagonali riflesso
    for i in range(2):
        ox = int((14 + i*16)*s)
        d.line([(fx0+ox, fy0+int(6*s)), (fx0+ox+int(8*s), fy1-int(6*s))],
               fill=ink, width=max(1, round(3*s)))

    # ── ELETTRODO ────────────────────────────────────────────────────────────
    # linea diagonale dalla maschera verso basso-destra
    el_x0, el_y0 = int(145*s), int(148*s)
    tip_x, tip_y = int(188*s), int(191*s)      # punta (dove scoccano le scintille)
    # corpo dell'elettrodo (linea spessa)
    d.line([(el_x0, el_y0), (tip_x, tip_y)], fill=ink, width=lw)
    # impugnatura (parte più in basso, più sottile)
    grip_x, grip_y = int(216*s), int(219*s)
    d.line([(tip_x+int(2*s), tip_y+int(2*s)), (grip_x, grip_y)],
           fill=ink, width=lw2)
    # cappuccino arrotondato dell'impugnatura
    gr = int(6*s)
    d.ellipse([grip_x-gr, grip_y-gr, grip_x+gr, grip_y+gr],
              outline=ink, width=lw2)

    # ── SCINTILLE ────────────────────────────────────────────────────────────
    # raggi irregolari che escono dalla punta, con zigzag
    cx, cy = tip_x + int(6*s), tip_y + int(6*s)
    sparks = [
        # (angolo_gradi, lunghezza, zigzag_offset_px)
        (  0,  22, +6),
        ( 30,  20, -5),
        ( 60,  18, +5),
        ( 90,  20, -6),
        (120,  17, +4),
        (-30,  19, -5),
        (-60,  16, +5),
        (150,  14, -4),
    ]
    for ang, length, zoff in sparks:
        rad   = math.radians(ang)
        ex    = cx + int(length * s * math.cos(rad))
        ey    = cy + int(length * s * math.sin(rad))
        mid_x = cx + int((length/2) * s * math.cos(rad))
        mid_y = cy + int((length/2) * s * math.sin(rad))
        perp  = rad + math.pi/2
        zx    = mid_x + int(abs(zoff)*s * math.cos(perp)) * (1 if zoff>0 else -1)
        zy    = mid_y + int(abs(zoff)*s * math.sin(perp)) * (1 if zoff>0 else -1)
        # spezzata: start → midpoint zigzag → end
        d.line([(cx, cy), (zx, zy)], fill=ink, width=lw3)
        d.line([(zx, zy), (ex, ey)], fill=ink, width=lw3)

    return img

sizes  = [16, 24, 32, 48, 64, 128, 256]
frames = [draw_icon(sz) for sz in sizes]

out  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
prev = os.path.join(os.path.dirname(out), "icon_preview.png")

frames[-1].save(out, format="ICO", sizes=[(sz, sz) for sz in sizes],
                append_images=frames[:-1])
frames[-1].save(prev)
print(f"Salvato:  {out}")
print(f"Preview:  {prev}")
