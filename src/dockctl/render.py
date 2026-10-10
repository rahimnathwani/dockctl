"""Render key faces (background + icon + label) and full-device previews."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

SF = "/System/Library/Fonts/SFNS.ttf"
SF_MONO = "/System/Library/Fonts/SFNSMono.ttf"
HELVETICA = "/System/Library/Fonts/Helvetica.ttc"
EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
EMOJI_STRIKE = 160  # Apple Color Emoji is a bitmap font; Pillow only renders it at a strike size

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dockctl"

WEIGHTS = {"regular": 400, "medium": 500, "semibold": 600, "bold": 700, "heavy": 800, "black": 900}


@dataclass
class Face:
    """Everything that determines what a key looks like."""

    label: str = ""
    icon: str = ""  # emoji, image path, or app:Name / app:/Applications/X.app
    image: str = ""  # full-bleed background image path (cropped to fill the key)
    bg: str = "#000000"
    fg: str = "#ffffff"
    font_size: int = 0  # largest label size in px; 0 = auto. Labels always shrink to fit.
    weight: str = "semibold"
    label_pos: str = "auto"  # auto | top | middle | bottom
    mono: bool = False
    border: str = ""  # optional ring colour, e.g. to mark "active"

    def cache_key(self) -> str:
        return hashlib.sha1(repr(self).encode()).hexdigest()


def parse_color(value: str, default=(0, 0, 0)) -> tuple[int, int, int]:
    if not value:
        return default
    value = value.strip()
    named = {
        "black": "#000000", "white": "#ffffff", "red": "#dc2626", "green": "#16a34a",
        "blue": "#2563eb", "yellow": "#eab308", "orange": "#ea580c", "purple": "#9333ea",
        "pink": "#db2777", "gray": "#4b5563", "grey": "#4b5563", "teal": "#0d9488",
        "cyan": "#0891b2", "indigo": "#4f46e5", "slate": "#1e293b", "amber": "#d97706",
    }
    value = named.get(value.lower(), value)
    m = re.fullmatch(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})", value)
    if not m:
        raise ValueError(f"bad colour {value!r} (use #rrggbb, #rgb or a name like red/green/blue)")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


@lru_cache(maxsize=64)
def _font(size: int, weight: str = "semibold", mono: bool = False) -> ImageFont.FreeTypeFont:
    path = SF_MONO if mono else SF
    try:
        f = ImageFont.truetype(path, size)
        try:
            f.set_variation_by_axes([100, min(max(size, 17), 96), 400, WEIGHTS.get(weight, 600)])
        except Exception:
            pass
        return f
    except OSError:
        return ImageFont.truetype(HELVETICA, size)


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(EMOJI, EMOJI_STRIKE)


def _is_emoji(s: str) -> bool:
    s = s.replace("️", "")
    if not s or len(s) > 4:
        return False
    return all(ord(c) >= 0x2100 for c in s)


def render_emoji(s: str, size: int) -> Image.Image:
    s = s.replace("️", "")
    f = _emoji_font()
    canvas = Image.new("RGBA", (EMOJI_STRIKE * 2, EMOJI_STRIKE * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((EMOJI_STRIKE // 2, EMOJI_STRIKE // 2), s, font=f, embedded_color=True)
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)
    canvas.thumbnail((size, size), Image.LANCZOS)
    return canvas


def _app_icon(app: str) -> Path | None:
    """Extract an .app bundle's icon to a cached PNG using sips."""
    app_path = Path(app).expanduser()
    if not app_path.suffix == ".app":
        for base in ("/Applications", "/System/Applications", "/System/Applications/Utilities", str(Path.home() / "Applications")):
            cand = Path(base) / f"{app}.app"
            if cand.exists():
                app_path = cand
                break
    info = app_path / "Contents" / "Info.plist"
    if not info.exists():
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"appicon-{hashlib.sha1(str(app_path).encode()).hexdigest()[:12]}.png"
    if out.exists() and out.stat().st_mtime >= info.stat().st_mtime:
        return out
    with info.open("rb") as fh:
        plist = plistlib.load(fh)
    name = plist.get("CFBundleIconFile") or plist.get("CFBundleIconName") or ""
    res = app_path / "Contents" / "Resources"
    candidates = [res / name, res / f"{name}.icns"] if name else []
    candidates += sorted(res.glob("*.icns"))
    for icns in candidates:
        if icns.is_file():
            r = subprocess.run(
                ["sips", "-s", "format", "png", "-Z", "256", str(icns), "--out", str(out)],
                capture_output=True,
            )
            if r.returncode == 0 and out.exists():
                return out
    return None


def load_icon(spec: str, size: int, base_dir: Path | None = None) -> Image.Image | None:
    """Turn an icon spec into an RGBA image no bigger than size x size."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.startswith("app:"):
        p = _app_icon(spec[4:])
        if not p:
            raise ValueError(f"no icon found for {spec}")
        img = Image.open(p).convert("RGBA")
    elif _is_emoji(spec):
        return render_emoji(spec, size)
    else:
        p = Path(spec).expanduser()
        if not p.is_absolute() and base_dir:
            p = base_dir / p
        if not p.exists():
            raise ValueError(f"icon file not found: {p}")
        img = Image.open(p)
        img.seek(0)
        img = img.convert("RGBA")
    img = ImageOps.contain(img, (size, size), Image.LANCZOS)
    return img


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if font.getlength(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def _fit_text(text: str, box_w: int, box_h: int, face: Face, max_size: int, scale: float = 1.0) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Pick the largest font that fits (font_size, if set, is the upper bound).
    Short labels shrink onto one line before they are allowed to wrap."""
    if face.font_size:
        max_size = max(6, round(int(face.font_size) * scale))
    min_single = max(9, box_w // 5)
    for allow_wrap in (False, True):
        size = max_size
        while size >= (min_single if not allow_wrap else 8):
            font = _font(size, face.weight, face.mono)
            lines = text.split("\n") if not allow_wrap else _wrap(text, font, box_w)
            asc, desc = font.getmetrics()
            line_h = asc + desc
            if all(font.getlength(line) <= box_w for line in lines) and line_h * len(lines) <= box_h:
                return font, lines, line_h
            size -= 1
    font = _font(8, face.weight, face.mono)
    asc, desc = font.getmetrics()
    return font, _wrap(text, font, box_w), asc + desc


def render_face(face: Face, size: int, base_dir: Path | None = None, scale: float = 1.0) -> Image.Image:
    """Render one key face as an RGB image of size x size. scale multiplies an explicit font_size (for previews)."""
    img = Image.new("RGB", (size, size), parse_color(face.bg))
    draw = ImageDraw.Draw(img)
    pad = max(3, size // 14)
    label = face.label or ""

    if face.image:
        p = Path(face.image).expanduser()
        if not p.is_absolute() and base_dir:
            p = base_dir / p
        if not p.exists():
            raise ValueError(f"image not found: {p}")
        bg = Image.open(p)
        bg.seek(0)
        img.paste(ImageOps.fit(bg.convert("RGB"), (size, size), Image.LANCZOS), (0, 0))

    icon = None
    if face.icon:
        icon_box = size - 2 * pad if not label else int(size * 0.56)
        icon = load_icon(face.icon, icon_box, base_dir)

    pos = face.label_pos
    if pos == "auto":
        pos = "bottom" if icon or face.image else "middle"

    if icon is not None:
        if label:
            ix = (size - icon.width) // 2
            iy = pad if pos == "bottom" else size - pad - icon.height
            if pos == "middle":
                iy = (size - icon.height) // 2
        else:
            ix, iy = (size - icon.width) // 2, (size - icon.height) // 2
        img.paste(icon, (ix, iy), icon)

    if label:
        if icon is not None and pos in ("top", "bottom"):
            box_h = size - icon.height - 2 * pad - 2
            max_size = max(10, size // 6)
        elif face.image and pos in ("top", "bottom"):  # a caption over artwork
            box_h = int(size * 0.4)
            max_size = max(10, size // 5)
        else:
            box_h = size - 2 * pad
            max_size = max(12, size // 3)
        font, lines, line_h = _fit_text(label, size - 2 * pad, box_h, face, max_size, scale)
        total_h = line_h * len(lines)
        if pos == "top":
            y = pad
        elif pos == "bottom":
            y = size - pad - total_h
        else:
            y = (size - total_h) // 2
        fg = parse_color(face.fg, (255, 255, 255))
        outline = {"stroke_width": max(1, size // 40), "stroke_fill": (0, 0, 0)} if face.image else {}
        for line in lines:
            draw.text((size // 2, y), line, font=font, fill=fg, anchor="ma", **outline)
            y += line_h

    if face.border:
        w = max(2, size // 22)
        draw.rectangle([w // 2, w // 2, size - 1 - w // 2, size - 1 - w // 2], outline=parse_color(face.border), width=w)
    return img


def render_preview(
    faces: dict[int, Image.Image | None],
    cols: int,
    rows: int,
    key_px: int,
    title: str = "",
    numbers: bool = True,
    side_keys: int = 0,
    side_px: int = 0,
) -> Image.Image:
    """Lay the key faces out like the physical device, with key numbers, for humans and agents to inspect."""
    gap = key_px // 5
    margin = key_px // 3
    title_h = key_px // 3 if title else 0
    side_px = side_px or key_px
    side_w = (gap * 2 + side_px) if side_keys else 0
    w = margin * 2 + cols * key_px + (cols - 1) * gap + side_w
    h = margin * 2 + title_h + rows * key_px + (rows - 1) * gap
    out = Image.new("RGB", (w, h), (63, 63, 70))
    draw = ImageDraw.Draw(out)
    small = _font(max(10, key_px // 8), "medium")
    if title:
        draw.text((margin, margin // 2), title, font=_font(max(12, key_px // 5), "semibold"), fill=(228, 228, 231))

    slots = []  # (key, x, y, size)
    for idx in range(cols * rows):
        r, c = divmod(idx, cols)
        slots.append((idx + 1, margin + c * (key_px + gap), margin + title_h + r * (key_px + gap), key_px))
    for i in range(side_keys):
        x = margin + cols * (key_px + gap) + gap
        y = margin + title_h + i * (key_px + gap) + (key_px - side_px) // 2
        slots.append((cols * rows + i + 1, x, y, side_px))

    for key, x, y, size in slots:
        radius = size // 10
        face = faces.get(key)
        if face is None:
            draw.rounded_rectangle([x, y, x + size, y + size], radius=radius, fill=(9, 9, 11), outline=(82, 82, 91))
        else:
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
            out.paste(face.resize((size, size)), (x, y), mask)
            draw.rounded_rectangle([x, y, x + size, y + size], radius=radius, outline=(113, 113, 122))
        if numbers:
            draw.text((x + 4, y + 2), str(key), font=small, fill=(250, 204, 21), stroke_width=2, stroke_fill=(0, 0, 0))
    return out


def fitted_size(text: str, size: int, weight: str = "bold") -> int:
    """The font size render_face would auto-pick for a text-only key."""
    pad = max(3, size // 14)
    font, _, _ = _fit_text(text, size - 2 * pad, size - 2 * pad, Face(label=text, weight=weight), max(12, size // 3))
    return int(font.size)


def chunk_text(text: str, max_chunks: int, key_px: int) -> list[str]:
    """Split text into whole-word chunks, one per key, so nothing is lost in the bezels between screens.

    Prefers few large chunks that stay readable; squeezes harder only when the text
    would not otherwise fit in max_chunks keys (the last chunk then takes the rest).
    """
    words = text.split()
    if not words:
        return []
    box = key_px - 2 * max(3, key_px // 14)

    def fits(chunk: str, px: int, max_lines: int) -> bool:
        font = _font(px, "bold")
        lines = _wrap(chunk, font, box)
        return len(lines) <= max_lines and all(font.getlength(line) <= box for line in lines)

    chunks: list[str] = []
    for px, max_lines in ((key_px // 4, 2), (key_px // 5, 3), (key_px // 7, 4)):
        chunks, cur = [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if not cur or fits(trial, px, max_lines):
                cur = trial
            else:
                chunks.append(cur)
                cur = w
        chunks.append(cur)
        if len(chunks) <= max_chunks:
            return chunks
    return chunks[: max_chunks - 1] + [" ".join(chunks[max_chunks - 1 :])]
