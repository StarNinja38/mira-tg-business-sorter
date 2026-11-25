"""
v4.114 Картинка анонса стрима для канала (как «Аватар на кадре» у Satomi).
Размытый кадр эфира, по центру аватар в градиентном кольце, ник на белой плашке,
слева сверху метка «● В ЭФИРЕ». После эфира кадр серый, метка «ЗАПИСЬ ЭФИРА».
Только Pillow, без сети: кадр и аватар приходят байтами, нет их — тёмный фон и буква ника.
"""
import io

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

W, H = 1280, 720
AV = 300            # диаметр аватара
RING = 14           # толщина кольца
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "/System/Library/Fonts/Supplemental/Arial Bold.ttf")


def _font(size: int):
    for path in _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _open(data: bytes | None):
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


def _background(frame, live: bool) -> Image.Image:
    if frame is None:
        bg = Image.new("RGB", (W, H), (24, 26, 36))
        glow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(glow).ellipse((W // 2 - 420, H // 2 - 300, W // 2 + 420, H // 2 + 300), fill=90)
        glow = glow.filter(ImageFilter.GaussianBlur(120))
        bg.paste(Image.new("RGB", (W, H), (88, 60, 170)), mask=glow)
    else:
        bg = ImageOps.fit(frame, (W, H)).filter(ImageFilter.GaussianBlur(26))
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
    if not live:
        bg = ImageEnhance.Color(bg).enhance(0.15)
    return bg


def _ring(size: int, live: bool) -> Image.Image:
    """Кольцо с диагональным градиентом: фиолетовый Twitch → голубой (после эфира — серое)."""
    a, b = ((145, 70, 255), (62, 166, 255)) if live else ((120, 120, 130), (170, 170, 180))
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return grad


def _circle_mask(size: int, scale: int = 4) -> Image.Image:
    m = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def _avatar(img, nick: str) -> Image.Image:
    if img is None:
        img = Image.new("RGB", (AV, AV), (58, 52, 92))
        d = ImageDraw.Draw(img)
        letter = (nick or "?")[:1].upper()
        f = _font(150)
        d.text((AV / 2, AV / 2), letter, font=f, fill=(255, 255, 255), anchor="mm")
    return ImageOps.fit(img, (AV, AV))


def _pill(draw, xy, text, font, fill, color, pad=(22, 10), dot=None):
    x, y = xy
    tw = draw.textlength(text, font=font)
    asc, desc = font.getmetrics()
    dot_w = 26 if dot else 0
    w, h = int(tw + pad[0] * 2 + dot_w), asc + desc + pad[1] * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2 if dot else 14, fill=fill)
    if dot:
        cy = y + h / 2
        draw.ellipse((x + pad[0], cy - 7, x + pad[0] + 14, cy + 7), fill=dot)
    draw.text((x + pad[0] + dot_w, y + pad[1]), text, font=font, fill=color)
    return w, h


def render(frame: bytes | None, avatar: bytes | None, nick: str, live: bool = True) -> bytes:
    """JPEG 1280×720 для send_photo."""
    img = _background(_open(frame), live).convert("RGBA")
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shade).ellipse((W // 2 - 330, 60, W // 2 + 330, H - 60), fill=(0, 0, 0, 70))
    img = Image.alpha_composite(img, shade.filter(ImageFilter.GaussianBlur(60)))

    outer = AV + RING * 2 + 8
    cx, cy = W // 2, 300
    ring = _ring(outer, live)
    img.paste(ring, (cx - outer // 2, cy - outer // 2), _circle_mask(outer))
    gap = AV + 8
    img.paste(Image.new("RGB", (gap, gap), (16, 18, 26)), (cx - gap // 2, cy - gap // 2), _circle_mask(gap))
    img.paste(_avatar(_open(avatar), nick), (cx - AV // 2, cy - AV // 2), _circle_mask(AV))

    draw = ImageDraw.Draw(img)
    f_nick = _font(46)
    nick = (nick or "").strip()[:24]
    tw = draw.textlength(nick, font=f_nick)
    asc, desc = f_nick.getmetrics()
    pw, ph = int(tw + 48), asc + desc + 22
    top = cy + outer // 2 + 26
    draw.rounded_rectangle((cx - pw // 2, top, cx + pw // 2, top + ph), radius=14, fill=(255, 255, 255))
    draw.text((cx, top + ph / 2), nick, font=f_nick, fill=(18, 18, 24), anchor="mm")

    f_badge = _font(30)
    if live:
        _pill(draw, (40, 40), "В ЭФИРЕ", f_badge, (229, 45, 63), (255, 255, 255), dot=(255, 255, 255))
    else:
        _pill(draw, (40, 40), "ЗАПИСЬ ЭФИРА", f_badge, (58, 60, 70), (230, 230, 235), dot=(150, 150, 160))

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()
