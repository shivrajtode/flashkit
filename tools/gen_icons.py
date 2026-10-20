"""Draws the Material-3-Expressive-style icon tiles and nav icons (offline, PIL, 4x supersampled)."""
import math, base64, io, json
from PIL import Image, ImageDraw

SS = 4
# palette: container / on-container (dark M3 tonal roles)
KINDS = {
    "primary":   ("#33477A", "#DCE5FF"),
    "secondary": ("#3D4659", "#DDE3F4"),
    "tertiary":  ("#54406A", "#F0DEFF"),
    "danger":    ("#6B2A29", "#FFDAD6"),
    "neutral":   ("#2A303C", "#C8CEDC"),
}
NAV_PILL = "#3A4A74"; NAV_ON = "#DCE5FF"; NAV_OFF = "#A3ABBB"

def rgba(h, a=255):
    h = h.lstrip("#"); return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)

class Pen:
    """Draw in a 96x96 design space onto a (w x h) canvas, centred at (cx, cy) design units."""
    def __init__(self, img, scale, ox=0, oy=0):
        self.d = ImageDraw.Draw(img); self.k = scale; self.ox = ox; self.oy = oy
    def P(self, x, y): return (self.ox + x * self.k, self.oy + y * self.k)
    def line(self, pts, w, col):
        pp = [self.P(*p) for p in pts]; W = w * self.k
        self.d.line(pp, fill=col, width=int(round(W)), joint="curve")
        r = W / 2
        for (x, y) in (pp[0], pp[-1]):
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    def poly(self, pts, col): self.d.polygon([self.P(*p) for p in pts], fill=col)
    def rrect(self, x0, y0, x1, y1, r, col=None, outline=None, w=0):
        a, b = self.P(x0, y0), self.P(x1, y1)
        self.d.rounded_rectangle([a, b], radius=r * self.k, fill=col, outline=outline, width=int(round(w * self.k)))
    def circle(self, cx, cy, r, col=None, outline=None, w=0):
        x, y = self.P(cx, cy); R = r * self.k
        self.d.ellipse([x - R, y - R, x + R, y + R], fill=col, outline=outline, width=int(round(w * self.k)))
    def arc(self, cx, cy, r, a0, a1, w, col):
        x, y = self.P(cx, cy); R = r * self.k; W = w * self.k
        self.d.arc([x - R, y - R, x + R, y + R], a0, a1, fill=col, width=int(round(W)))
        for a in (a0, a1):
            ex, ey = x + R * math.cos(math.radians(a)), y + R * math.sin(math.radians(a))
            self.d.ellipse([ex - W / 2, ey - W / 2, ex + W / 2, ey + W / 2], fill=col)

# ---------------------------------------------------------------- glyphs (design space 96x96, glyph ~48 wide)
def g_sideload(p, c):      # up arrow into a tray
    p.line([(30, 56), (30, 68), (66, 68), (66, 56)], 7, c)
    p.line([(48, 60), (48, 26)], 7, c)
    p.line([(35, 39), (48, 26), (61, 39)], 7, c)
def g_flash(p, c):         # lightning bolt
    p.poly([(54, 22), (32, 52), (45, 52), (40, 74), (64, 42), (50, 42)], c)
def g_apk(p, c):           # package / box
    p.poly([(48, 24), (68, 35), (68, 59), (48, 70), (28, 59), (28, 35)], None) if False else None
    p.line([(48, 24), (68, 35), (68, 59), (48, 70), (28, 59), (28, 35), (48, 24)], 6, c)
    p.line([(28, 35), (48, 46), (68, 35)], 6, c)
    p.line([(48, 46), (48, 70)], 6, c)
def g_reboot(p, c):        # power
    p.arc(48, 52, 20, -60, 240, 7, c)
    p.line([(48, 24), (48, 48)], 7, c)
def g_erase(p, c):         # trash can
    p.line([(30, 33), (66, 33)], 7, c)
    p.line([(41, 33), (41, 27), (55, 27), (55, 33)], 6, c)
    p.line([(35, 40), (38, 68), (58, 68), (61, 40)], 7, c)
    p.line([(46, 47), (46, 61)], 5, c); p.line([(52, 47), (52, 61)], 5, c)
def g_gsi(p, c):           # layers
    for dy, w in ((0, 7), (14, 7), (28, 7)):
        pass
    p.line([(48, 24), (70, 36), (48, 48), (26, 36), (48, 24)], 6, c)
    p.line([(26, 47), (48, 59), (70, 47)], 6, c)
    p.line([(26, 58), (48, 70), (70, 58)], 6, c)
def g_recovery(p, c):      # wrench: open-ended head (gap facing up-right) + handle
    p.line([(55, 41), (30, 66)], 9, c)
    p.arc(59, 37, 13, 350, 640, 9, c)
    p.circle(30, 66, 3.2, col=(0, 0, 0, 0)) if False else None
def g_refresh(p, c):       # circular arrow, head at the end of the arc
    cx, cy, r = 48, 49, 19
    p.arc(cx, cy, r, 25, 315, 7, c)
    th = math.radians(315)
    ex, ey = cx + r * math.cos(th), cy + r * math.sin(th)
    tx, ty = -math.sin(th), math.cos(th)          # clockwise tangent
    nx, ny = math.cos(th), math.sin(th)           # outward normal
    tip = (ex + tx * 13, ey + ty * 13)
    p.poly([tip, (ex + nx * 11, ey + ny * 11), (ex - nx * 11, ey - ny * 11)], c)
def g_fix(p, c):           # plug
    p.rrect(35, 38, 61, 62, 6, col=None, outline=c, w=6)
    p.line([(42, 38), (42, 26)], 6, c); p.line([(54, 38), (54, 26)], 6, c)
    p.line([(48, 62), (48, 74)], 6, c)
# nav
def g_home(p, c):
    p.line([(26, 48), (48, 28), (70, 48)], 7, c)
    p.line([(32, 44), (32, 68), (64, 68), (64, 44)], 7, c)
    p.line([(44, 68), (44, 54), (52, 54), (52, 68)], 6, c)
def g_folder(p, c):
    p.line([(26, 36), (26, 66), (70, 66), (70, 42), (50, 42), (45, 34), (30, 34), (26, 36)], 6, c)
def g_terminal(p, c):
    p.rrect(24, 30, 72, 66, 9, col=None, outline=c, w=6)
    p.line([(35, 41), (44, 48), (35, 55)], 6, c)
    p.line([(49, 56), (60, 56)], 6, c)
def g_gear(p, c):
    p.circle(48, 48, 19, col=c)
    for i in range(8):
        a = math.radians(i * 45)
        x, y = 48 + 22 * math.cos(a), 48 + 22 * math.sin(a)
        p.circle(x, y, 5, col=c)
    p.circle(48, 48, 8.5, col=(0, 0, 0, 0))   # punched hole
GLYPHS = {"sideload": g_sideload, "flash": g_flash, "apk": g_apk, "reboot": g_reboot, "erase": g_erase,
          "gsi": g_gsi, "recovery": g_recovery, "refresh": g_refresh, "fix": g_fix}
def g_storage(p, c):       # a drive: rounded box, a line and a dot
    p.rrect(22, 36, 74, 64, 9, col=None, outline=c, w=6)
    p.line([(32, 50), (50, 50)], 6, c)
    p.circle(63, 50, 3.6, col=c)
NAV = {"home": g_home, "files": g_folder, "storage": g_storage, "console": g_terminal, "settings": g_gear}
TILE_KIND = {"sideload": "primary", "flash": "primary", "apk": "secondary", "reboot": "secondary", "erase": "danger",
             "gsi": "tertiary", "recovery": "tertiary", "refresh": "neutral", "fix": "neutral"}

def punch(img):
    """gear hole: pixels painted fully transparent need replace-not-blend; handled by drawing on a mask copy."""
    return img

def tile(name, size=192):
    cont, on = KINDS[TILE_KIND[name]]
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    p = Pen(img, size * SS / 96.0)
    p.rrect(2, 2, 94, 94, 34, col=rgba(cont))
    GLYPHS[name](p, rgba(on))
    return img.resize((size, size), Image.LANCZOS)

def nav(name, selected, w=192, h=96):
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    k = h * SS / 96.0
    if selected:
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2 * k, 8 * k, (w * SS / k - 2) * k, 88 * k], radius=40 * k, fill=rgba(NAV_PILL))
    glyph = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    p = Pen(glyph, k, ox=(w * SS - 96 * k) / 2, oy=0)
    p_scale_fix = p
    NAV[name](p, rgba(NAV_ON if selected else NAV_OFF))
    if name == "settings":                       # punch the gear hole out of the glyph layer
        hole = Image.new("L", glyph.size, 255); hd = ImageDraw.Draw(hole)
        cx, cy = p.P(48, 48); R = 8.5 * k
        hd.ellipse([cx - R, cy - R, cx + R, cy + R], fill=0)
        glyph.putalpha(Image.composite(glyph.getchannel("A"), Image.new("L", glyph.size, 0), hole))
    img = Image.alpha_composite(img, glyph)
    return img.resize((w, h), Image.LANCZOS)

CAP = {"primary": "#2D4275", "tonal": "#2A303C", "danger": "#5F1A17", "flat": "#1B1F27"}

def cap(kind, side, h=168):
    w = h // 2
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = rgba(CAP[kind])
    R = h * SS / 2
    if side == "L":
        d.ellipse([0, 0, 2 * R, h * SS - 1], fill=col)
        d.rectangle([R, 0, w * SS, h * SS], fill=col)
    else:
        d.ellipse([w * SS - 2 * R, 0, w * SS, h * SS - 1], fill=col)
        d.rectangle([0, 0, w * SS - R, h * SS], fill=col)
    return img.resize((w, h), Image.LANCZOS)

def flat_png(im, rgb_hex, levels=16):
    """One exact colour + a 16-step alpha ramp (palette PNG with tRNS): tiny, and no colour seam."""
    a = im.getchannel("A")
    idx = a.point(lambda v: int(round(v / 255.0 * (levels - 1))))
    p = Image.new("P", im.size)
    r, g, b = int(rgb_hex[1:3], 16), int(rgb_hex[3:5], 16), int(rgb_hex[5:7], 16)
    p.putpalette([r, g, b] * levels + [0] * (3 * (256 - levels)))
    p.putdata(list(idx.getdata()))
    buf = io.BytesIO()
    p.save(buf, "PNG", optimize=True, transparency=bytes(int(round(i * 255.0 / (levels - 1))) for i in range(levels)))
    return buf.getvalue()

def small(im, colors=48):
    """palette-quantise (keeps alpha) - the art is flat colour, this cuts the size to a third."""
    return im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)

def png_bytes(im):
    b = io.BytesIO(); im.save(b, "PNG", optimize=True); return b.getvalue()

if __name__ == "__main__":
    out = {}
    for n in GLYPHS: out["tile_" + n] = png_bytes(small(tile(n, 192)))
    for n in NAV:
        out["nav_" + n] = png_bytes(small(nav(n, False, 192, 96), 24))
        out["nav_%s_sel" % n] = png_bytes(small(nav(n, True, 192, 96), 24))
    for k in CAP:
        out["cap_%s_L" % k] = flat_png(cap(k, "L"), CAP[k]); out["cap_%s_R" % k] = flat_png(cap(k, "R"), CAP[k])
    json.dump({k: base64.b64encode(v).decode() for k, v in out.items()}, open("icons.json", "w"))
    print({k: len(v) for k, v in out.items()}); print("total raw bytes:", sum(len(v) for v in out.values()))
    bg = rgba("#101318")
    sheet = Image.new("RGBA", (10 + 5 * 202, 10 + 2 * 202 + 20 + 2 * 110 + 20 + 100), bg)
    for i, n in enumerate(GLYPHS):
        t = Image.open(io.BytesIO(out["tile_" + n])).convert("RGBA"); sheet.alpha_composite(t, (10 + (i % 5) * 202, 10 + (i // 5) * 202))
    y0 = 10 + 2 * 202 + 20
    for i, n in enumerate(NAV):
        for j, sel in enumerate((False, True)):
            t = Image.open(io.BytesIO(out["nav_%s%s" % (n, "_sel" if sel else "")])).convert("RGBA")
            sheet.alpha_composite(t, (10 + i * 250, y0 + j * 110))
    # a demo pill built from the real parts: cap + colour block + cap
    y1 = y0 + 2 * 110 + 20
    x = 10
    for kind in CAP:
        L = Image.open(io.BytesIO(out["cap_%s_L" % kind])).convert("RGBA"); R = Image.open(io.BytesIO(out["cap_%s_R" % kind])).convert("RGBA")
        mid = Image.new("RGBA", (150, 168 // 1), rgba(CAP[kind]))
        pill = Image.new("RGBA", (L.width + 150 + R.width, 168), (0, 0, 0, 0))
        pill.alpha_composite(L, (0, 0)); pill.alpha_composite(mid, (L.width, 0)); pill.alpha_composite(R, (L.width + 150, 0))
        pill = pill.resize((pill.width * 100 // 168, 100), Image.LANCZOS)
        sheet.alpha_composite(pill, (x, y1)); x += pill.width + 14
    sheet.convert("RGB").save("preview.png")
