#!/usr/bin/env python3
"""Render docs/media/scratchpad.gif and scratchpad-dark.gif: the scratchpad nudge, write and trim cycle."""
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = {"light": ROOT / "docs/media/scratchpad.gif", "dark": ROOT / "docs/media/scratchpad-dark.gif"}
# DMSans.ttf from github.com/google/fonts (ofl/dmsans); SC_FONT_DIR overrides the directory.
FONT_DIR = Path(os.environ.get("SC_FONT_DIR", Path.home() / ".cache/smarter-compact/fonts"))
SANS_FONTS = [FONT_DIR / "DMSans.ttf", Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")]
MONO_FONTS = [
    Path.home() / ".local/share/fonts/nerd-fonts/JetBrainsMonoNerdFontMono-Bold.ttf",
    FONT_DIR / "JetBrainsMono-Bold.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"),
]

W, H = 1068, 536
SS = 2
FRAME_MS = 100
HOLD_FRAMES = 15

THEMES = {
    "light": {
        "outside": (245, 245, 249), "bg": (236, 239, 248), "grid": (214, 218, 236), "frame": (225, 227, 238),
        "card": (252, 253, 253), "card_hl": (248, 248, 253), "border": (214, 218, 236), "accent": (69, 81, 165),
        "text": (26, 28, 41), "muted": (85, 91, 114), "orange": (246, 234, 217), "brown": (184, 135, 95),
        "note": (232, 234, 249), "note_text": (54, 59, 99), "bar_bg": (252, 253, 253),
    },
    "dark": {
        "outside": (14, 16, 23), "bg": (17, 20, 32), "grid": (38, 43, 69), "frame": (52, 58, 84),
        "card": (20, 23, 38), "card_hl": (28, 32, 47), "border": (48, 54, 80), "accent": (170, 180, 245),
        "text": (236, 238, 247), "muted": (180, 185, 208), "orange": (58, 46, 28), "brown": (174, 156, 134),
        "note": (38, 45, 85), "note_text": (180, 187, 232), "bar_bg": (20, 23, 38),
    },
}

LX, LW = 37, 380
RX, RW = 652, 379
CH, PITCH = 32, 38
BAR_Y, BAR_H = 77, 17
TRIM_FRAC = 0.82
NUDGE_AT = 0.75
NOTE_H = 122
NUDGE_H = 28

PAD_LINES = ["goal · how it is checked", "status: what works, what fails", "decisions and why",
             "facts: paths, commands, values", "next step"]
PAD_UPDATED = "next step (rewritten)"
NOTE_HEAD = "Your scratchpad, kept across trims (possibly stale)"
PAD_TOP, PAD_ROW = 158, 36
CALL_Y = 100
SNAP_Y = 384

# Frame timeline: one constant per storyboard beat.
T_SYS, T_T1, T_R1, T_R2, T_R3 = 0, 3, 7, 11, 15
T_NUDGE1 = 21
T_WRITE = 32
T_R4, T_R5 = 52, 56
T_TRIM1 = 60
T_NOTE1 = 70
T_SNAP1 = 78
T_R6 = 88
T_NUDGE2 = 94
T_UPDATE = 100
T_R7 = 108
T_TRIM2 = 112
T_NOTE2 = 120
T_SNAP2 = 126
N_FRAMES = 130 + HOLD_FRAMES

BAR_EVENTS = [(T_SYS, 0.12), (T_T1, 0.22), (T_R1, 0.40), (T_R2, 0.56), (T_R3, 0.82), (T_WRITE + 6, 0.86),
              (T_R4, 0.94), (T_R5, 1.03), (T_TRIM1 + 2, 0.40), (T_NOTE1, 0.48), (T_R6, 0.82),
              (T_UPDATE + 2, 0.86), (T_R7, 1.03), (T_TRIM2 + 2, 0.40), (T_NOTE2, 0.50)]


def load_font(paths, size, weight=None):
    """First loadable font from paths at size, with the variable weight axis set when present."""
    for p in paths:
        if p.exists():
            f = ImageFont.truetype(str(p), size * SS)
            if weight is not None:
                try:
                    f.set_variation_by_axes([min(max(size, 9), 40), weight])
                except OSError:
                    pass
            return f
    sys.exit(f"no font found in {paths}")


FONTS = {
    "card": load_font(SANS_FONTS, 17, 560), "tag": load_font(SANS_FONTS, 13, 600),
    "head": load_font(SANS_FONTS, 16, 700), "label": load_font(SANS_FONTS, 15, 400),
    "note": load_font(SANS_FONTS, 15, 600), "mono": load_font(MONO_FONTS, 15), "mono_s": load_font(MONO_FONTS, 15),
}


def ease(t):
    """Smoothstep on [0, 1]."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def ramp(f, start, length=4):
    """0 before start, eased to 1 over length frames."""
    return ease((f - start) / length)


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(round(lerp(a, b, t)) for a, b in zip(c1, c2))


def bar_value(f):
    """Context fill as a fraction of trim_at_tokens at frame f."""
    v = 0.0
    for start, target in BAR_EVENTS:
        if f >= start:
            v = lerp(v, target, ramp(f, start, 4))
    return v


def bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(p0, p1, p2, p3))


class Canvas:
    """Supersampled frame; all public coordinates are in output pixels."""

    def __init__(self, theme):
        self.c = THEMES[theme]
        self.img = Image.new("RGBA", (W * SS, H * SS), self.c["outside"] + (255,))

    def layer(self, alpha, fn):
        """Draw fn onto a transparent layer and composite it at alpha."""
        if alpha <= 0.01:
            return
        lay = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        fn(ImageDraw.Draw(lay))
        if alpha < 0.99:
            lay.putalpha(lay.getchannel("A").point(lambda a: round(a * alpha)))
        self.img.alpha_composite(lay)

    def background(self):
        d = ImageDraw.Draw(self.img)
        d.rounded_rectangle([0, 0, W * SS - 1, H * SS - 1], 16 * SS, fill=self.c["bg"], outline=self.c["frame"], width=3)
        for x in range(36, W - 4, 36):
            d.rectangle([x * SS, 3 * SS, x * SS + 1, (H - 3) * SS], fill=self.c["grid"])
        for y in range(36, H - 4, 36):
            d.rectangle([3 * SS, y * SS - 1, (W - 3) * SS, y * SS + 1], fill=self.c["grid"])

    def box(self, d, x, y, w, h, fill, outline, width=1.0, r=8):
        d.rounded_rectangle([x * SS, y * SS, (x + w) * SS, (y + h) * SS], r * SS, fill=fill, outline=outline,
                            width=max(1, round(width * SS)))

    def text(self, d, x, y, s, font, fill, anchor="lm", tracking=0.0):
        if not tracking:
            d.text((x * SS, y * SS), s, font=FONTS[font], fill=fill, anchor=anchor)
            return
        for ch in s:
            d.text((x * SS, y * SS), ch, font=FONTS[font], fill=fill, anchor=anchor)
            x += FONTS[font].getlength(ch) / SS + tracking

    def dashed_box(self, d, x, y, w, h, color, r=12, dash=6, gap=5):
        steps = []
        per = 2 * (w - 2 * r) + 2 * (h - 2 * r) + 2 * math.pi * r
        n = int(per)
        for i in range(n):
            steps.append(self._perimeter(x, y, w, h, r, i / n * per))
        for i in range(0, n, dash + gap):
            seg = steps[i:i + dash]
            if len(seg) > 1:
                d.line([(px * SS, py * SS) for px, py in seg], fill=color, width=2)

    @staticmethod
    def _perimeter(x, y, w, h, r, s):
        """Point at arc length s along a rounded rectangle, clockwise from the top-left straight edge."""
        segs = [("l", (x + r, y), (x + w - r, y)), ("a", (x + w - r, y + r), -90),
                ("l", (x + w, y + r), (x + w, y + h - r)), ("a", (x + w - r, y + h - r), 0),
                ("l", (x + w - r, y + h), (x + r, y + h)), ("a", (x + r, y + h - r), 90),
                ("l", (x, y + h - r), (x, y + r)), ("a", (x + r, y + r), 180)]
        for kind, a, b in segs:
            if kind == "l":
                ln = math.dist(a, b)
                if s <= ln:
                    t = s / ln
                    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))
                s -= ln
            else:
                ln = math.pi * r / 2
                if s <= ln:
                    ang = math.radians(b + 90 * s / ln)
                    return (a[0] + r * math.cos(ang), a[1] + r * math.sin(ang))
                s -= ln
        return (x + r, y)

    def dashed_curve(self, d, p0, p3, color, progress=1.0, bend=0.5):
        """Dashed cubic curve from p0 toward p3, drawn up to progress."""
        dx = p3[0] - p0[0]
        p1, p2 = (p0[0] + dx * bend, p0[1]), (p3[0] - dx * bend, p3[1])
        n = 160
        pts = [bezier(p0, p1, p2, p3, i / n) for i in range(int(n * progress) + 1)]
        for i in range(0, len(pts) - 1, 4):
            seg = pts[i:i + 3]
            if len(seg) > 1:
                d.line([(px * SS, py * SS) for px, py in seg], fill=color, width=round(2.2 * SS))
        if progress >= 0.999 and len(pts) > 2:
            self.arrowhead(d, pts[-3], pts[-1], color)

    def arrowhead(self, d, a, b, color):
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        pts = [b] + [(b[0] - 9 * math.cos(ang + s), b[1] - 9 * math.sin(ang + s)) for s in (0.45, -0.45)]
        d.polygon([(px * SS, py * SS) for px, py in pts], fill=color)

    def final(self):
        return self.img.convert("RGB").resize((W, H), Image.LANCZOS)


def card(cv, alpha, x, y, label, tag=None, hl=False, w=LW, font="card", fill=None, outline=None):
    """One context or pad card with an optional right-aligned tag."""
    c = cv.c

    def draw(d):
        cv.box(d, x, y, w, CH, fill or (c["card_hl"] if hl else c["card"]),
               outline or (c["accent"] if hl else c["border"]), 1.5 if (hl or outline) else 1.0)
        cv.text(d, x + 15, y + CH / 2, label, font, c["text"])
        if tag:
            cv.text(d, x + w - 11, y + CH / 2 + 1, tag, "tag", c["muted"], anchor="rm")

    cv.layer(alpha, draw)


def nudge_card(cv, alpha, y):
    """The <compact-scratchpad-nudge> block attached to the newest tool result."""
    c = cv.c

    def draw(d):
        d.line([((LX + 14) * SS, (y - 2) * SS), ((LX + 14) * SS, (y + NUDGE_H / 2) * SS),
                ((LX + 26) * SS, (y + NUDGE_H / 2) * SS)], fill=c["brown"], width=3)
        cv.box(d, LX + 26, y, 262, NUDGE_H, c["orange"], c["brown"], 1.5, r=14)
        cv.text(d, LX + 38, y + NUDGE_H / 2, "<compact-scratchpad-nudge>", "mono", c["text"])

    cv.layer(alpha, draw)


def note_block(cv, alpha, y, lines):
    """The trim note's scratchpad block at the cut."""
    c = cv.c

    def draw(d):
        cv.box(d, LX - 4, y, LW + 8, NOTE_H, c["note"], c["accent"], 2, r=10)
        cv.text(d, LX + 10, y + 17, NOTE_HEAD, "note", c["note_text"])
        for i, s in enumerate(lines):
            cv.text(d, LX + 12, y + 41 + i * 17, "· " + s, "mono_s", c["note_text"])

    cv.layer(alpha, draw)


def heading(cv, x, title, sub=None):
    def draw(d):
        cv.text(d, x, 36, title, "head", cv.c["muted"], tracking=2.4)
        if sub:
            cv.text(d, x, 61, sub, "label", cv.c["muted"])

    cv.layer(1.0, draw)


def context_bar(cv, f):
    c = cv.c
    v = bar_value(f)
    trim_x = LX + TRIM_FRAC * LW
    nudge_x = LX + TRIM_FRAC * LW * NUDGE_AT
    near = max(ramp(f, T_R3 + 2, 2) * (1 - ramp(f, T_NUDGE1 + 6, 3)), ramp(f, T_R6 + 2, 2) * (1 - ramp(f, T_NUDGE2 + 6, 3)))

    def draw(d):
        cv.box(d, LX, BAR_Y, LW, BAR_H, c["bar_bg"], c["border"], 1, r=BAR_H / 2)
        fill_w = max(BAR_H, min(LW, TRIM_FRAC * LW * v))
        cv.box(d, LX, BAR_Y, fill_w, BAR_H, c["accent"], None, r=BAR_H / 2)
        for mx, col in ((nudge_x, mix(c["muted"], c["brown"], near)), (trim_x, c["brown"])):
            for yy in range(BAR_Y - 9, BAR_Y + BAR_H + 9, 5):
                d.rectangle([round((mx - 1) * SS), yy * SS, round((mx + 1) * SS), (yy + 3) * SS], fill=col)
        cv.text(d, nudge_x - 8, BAR_Y + 37, "nudge 0.75", "label", mix(c["muted"], c["brown"], near), anchor="rm")
        cv.text(d, trim_x + 10, BAR_Y + 37, "trim_at_tokens", "label", c["muted"])

    cv.layer(1.0, draw)


def slide(f, start, y0, y1, length=5):
    return lerp(y0, y1, ramp(f, start, length))


def left_column(cv, f):
    y = [135 + i * PITCH for i in range(12)]
    card(cv, ramp(f, T_SYS, 3), LX, y[0], "system prompt · tools", "untouched")
    card(cv, ramp(f, T_T1, 3), LX, y[1], "turn 1 · user (task)", "pinned")
    out1 = 1 - ramp(f, T_TRIM1, 5)
    dx1 = -24 * ramp(f, T_TRIM1, 5)
    for i, t in enumerate((T_R1, T_R2, T_R3)):
        card(cv, ramp(f, t, 3) * out1, LX + dx1, y[2 + i] + 8 * (1 - ramp(f, t, 3)), f"tool_use + result {i + 1}")
    nudge_card(cv, ramp(f, T_NUDGE1, 4) * out1, y[4] + CH + 2)
    y_after1 = [211 + NOTE_H + 8 + i * PITCH for i in range(3)]
    out2 = 1 - ramp(f, T_TRIM2, 5)
    dx2 = -24 * ramp(f, T_TRIM2, 5)
    kept1 = ramp(f, T_TRIM1 + 6, 3)
    for i, t in enumerate((T_R4, T_R5)):
        yy = slide(f, T_TRIM1 + 5, y[4] + CH + NUDGE_H + 8 + i * PITCH, y_after1[i])
        card(cv, ramp(f, t, 3) * out2, LX + dx2, yy, f"tool_use + result {4 + i}",
             "kept" if kept1 > 0.5 else None)
    note_block(cv, ramp(f, T_NOTE1, 4) * out2, 211, PAD_LINES)
    y6 = y_after1[2]
    y_after2 = [211 + NOTE_H + 8 + i * PITCH for i in range(2)]
    kept2 = ramp(f, T_TRIM2 + 6, 3) > 0.5
    card(cv, ramp(f, T_R6, 3), LX, slide(f, T_TRIM2 + 5, y6, y_after2[0]), "tool_use + result 6",
         "kept" if kept2 else None)
    nudge_card(cv, ramp(f, T_NUDGE2, 4) * out2, y6 + CH + 2)
    card(cv, ramp(f, T_R7, 3), LX, slide(f, T_TRIM2 + 5, y6 + CH + NUDGE_H + 8, y_after2[1]), "tool_use + result 7",
         "kept" if kept2 else None)
    note_block(cv, ramp(f, T_NOTE2, 4), 211, PAD_LINES[:4] + [PAD_UPDATED])


def right_column(cv, f):
    c = cv.c
    heading(cv, RX, "SCRATCHPAD", "scratchpad/<sid>.md · 2,500 chars max")
    updating = f >= T_UPDATE + 3
    call = 'smart_compact_scratchpad("str_replace")' if updating else 'smart_compact_scratchpad("create")'
    pop = ramp(f, T_UPDATE + 3, 3) if updating else 1.0

    def draw_call(d):
        cv.box(d, RX - 8, CALL_Y, RW + 16, CH, c["card_hl"], c["accent"], 1.5)
        cv.text(d, RX + 6, CALL_Y + CH / 2, call, "mono", c["text"])

    cv.layer(ramp(f, T_WRITE + 5, 3) * pop, draw_call)
    cv.layer(ramp(f, T_WRITE + 7, 3), lambda d: cv.dashed_box(d, RX - 12, PAD_TOP - 12, RW + 24,
                                                              5 * PAD_ROW + 20, c["border"]))
    flash = ramp(f, T_UPDATE + 4, 2) * (1 - ramp(f, T_UPDATE + 12, 4))
    for i, s in enumerate(PAD_LINES):
        t = T_WRITE + 9 + 2 * i
        a = ramp(f, t, 3)
        label = PAD_UPDATED if (i == 4 and f >= T_UPDATE + 5) else s
        fill = mix(c["card"], c["orange"], flash) if i == 4 else None
        outline = mix(c["border"], c["brown"], flash) if (i == 4 and flash > 0.05) else None
        card(cv, a, RX - 8 + 10 * (1 - a), PAD_TOP + i * PAD_ROW, label, w=RW + 16, font="mono", fill=fill,
             outline=outline)

    def draw_snap_label(d):
        cv.text(d, RX - 8, SNAP_Y - 20, "copy saved at each trim", "label", c["muted"])

    cv.layer(ramp(f, T_SNAP1, 3), draw_snap_label)
    for k, t in enumerate((T_SNAP1, T_SNAP2)):
        a = ramp(f, t, 3)

        def draw_chip(d, k=k):
            x = RX - 8 + k * 132
            cv.box(d, x, SNAP_Y, 120, 28, c["card"], c["border"], 1, r=14)
            cv.text(d, x + 60, SNAP_Y + 14, f"<sid>.{k + 1}.md", "mono", c["text"], anchor="mm")

        cv.layer(a, draw_chip)


def arrows(cv, f):
    c = cv.c
    nudge1 = (LX + 296, 135 + 4 * PITCH + CH + 2 + NUDGE_H / 2)
    nudge2 = (LX + 296, 211 + NOTE_H + 8 + 2 * PITCH + CH + 2 + NUDGE_H / 2)
    call_in = (RX - 14, CALL_Y + CH / 2)
    pad_out = (RX - 16, PAD_TOP + 2.5 * PAD_ROW)
    note_in = (LX + LW + 10, 211 + NOTE_H / 2)
    spans = [(nudge1, call_in, T_WRITE, T_R4), (pad_out, note_in, T_NOTE1 - 4, T_R6),
             (nudge2, call_in, T_UPDATE - 2, T_R7), (pad_out, note_in, T_NOTE2 - 4, N_FRAMES + 10)]
    for p0, p3, start, end in spans:
        prog = ramp(f, start, 6)
        alpha = min(1.0, prog * 4) * (1 - ramp(f, end, 4))
        cv.layer(alpha, lambda d, p0=p0, p3=p3, prog=prog: cv.dashed_curve(d, p0, p3, c["brown"], prog))


def render(theme, f):
    cv = Canvas(theme)
    cv.background()
    heading(cv, LX, "MODEL CONTEXT")
    context_bar(cv, f)
    left_column(cv, f)
    right_column(cv, f)
    arrows(cv, f)
    return cv.final()


def save_gif(frames, path):
    """Quantize all frames to one shared palette so unchanged regions stay identical between frames."""
    sample = Image.new("RGB", (W, H * 4))
    for i, f in enumerate((T_WRITE + 20, T_NOTE1 + 8, T_UPDATE + 6, N_FRAMES - 1)):
        sample.paste(frames[f], (0, H * i))
    ref = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = [fr.quantize(palette=ref, dither=Image.Dither.NONE) for fr in frames]
    pal[0].save(path, save_all=True, append_images=pal[1:], duration=FRAME_MS, loop=0, optimize=True, disposal=1)


def main():
    preview = sys.argv[1] if len(sys.argv) > 1 else None
    for theme, path in OUT.items():
        frames = [render(theme, f) for f in range(N_FRAMES)]
        if preview:
            for f in range(0, N_FRAMES, 1):
                frames[f].save(Path(preview) / f"{theme}_{f:03d}.png")
        save_gif(frames, path)
        print(path, len(frames))


if __name__ == "__main__":
    main()
