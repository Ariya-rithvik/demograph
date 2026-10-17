"""Compose replay keyframes and voiceover into a narrated MP4.

Each scene opens on the page, eases the camera in on the elements the scene cites,
rings them (with a cursor on whatever gets clicked), and holds while the narration
plays. Captions carry the same words. Frames are streamed straight into ffmpeg.
"""

import io
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .media import ffmpeg_exe
from .replay import Keyframe, Mark, SceneCapture

W, H, FPS = 1280, 720, 30
MAX_ZOOM = 1.9
RING = (255, 176, 32)
INK = (16, 20, 31)
FOCUS_ROLES = ("click", "fill", "shown", "result")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (["segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def lerp_rect(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


# --- camera ------------------------------------------------------------------

def focus_rect(marks: list[Mark], iw: int, ih: int) -> tuple:
    """A 16:9 box around what the keyframe is about, zoomed at most MAX_ZOOM."""
    rects = [m.rect for m in marks if m.role == "result"]  # a confirmation is what the shot is about
    rects = rects or [m.rect for m in marks if m.role in FOCUS_ROLES]
    if not rects:
        return (0, 0, iw, ih)
    s = iw / W
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    pad = 110 * s
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    w, h = x1 - x0, y1 - y0
    aspect = iw / ih
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    w = max(w, iw / MAX_ZOOM)
    h = w / aspect
    if w >= iw or h >= ih:
        return (0, 0, iw, ih)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(0, cx - w / 2), iw - w)
    top = min(max(0, cy - h / 2), ih - h)
    return (left, top, left + w, top + h)


# --- decorations drawn on the full-resolution screenshot -------------------------

def _cursor(draw: ImageDraw.ImageDraw, x: float, y: float, s: float) -> None:
    pts = [(0, 0), (0, 22), (6, 17), (10, 26), (14, 24), (10, 16), (17, 16)]
    poly = [(x + px * s * 1.2, y + py * s * 1.2) for px, py in pts]
    draw.polygon(poly, fill=(255, 255, 255), outline=(20, 20, 20), width=max(1, int(1.6 * s)))


def decorate(kf: Keyframe) -> Image.Image:
    img = Image.open(io.BytesIO(kf.png)).convert("RGB")
    s = img.width / W
    focus = [m for m in kf.marks if m.role in FOCUS_ROLES]
    if not focus:
        return img
    pad, radius = 8 * s, 10 * s
    shade = Image.new("L", img.size, int(255 * 0.28))
    sd = ImageDraw.Draw(shade)
    for m in focus:
        x, y, w, h = m.rect
        sd.rounded_rectangle((x - pad, y - pad, x + w + pad, y + h + pad), radius=radius, fill=0)
    img = Image.composite(Image.new("RGB", img.size, (8, 10, 18)), img, shade)
    d = ImageDraw.Draw(img)
    for m in focus:
        x, y, w, h = m.rect
        d.rounded_rectangle((x - pad, y - pad, x + w + pad, y + h + pad), radius=radius,
                            outline=RING, width=max(2, int(3.5 * s)))
    for m in focus:
        if m.role == "click":
            x, y, w, h = m.rect
            _cursor(d, x + w * 0.62, y + h * 0.58, s)
    return img


# --- overlays on the output frame ----------------------------------------------

CAPTION_LINE_H = 40


def caption_height(text: str) -> int:
    return CAPTION_LINE_H * len(textwrap.wrap(text, width=70)[:3]) + 28


def caption_overlay(text: str, chip: str, at_top: bool = False) -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    font = _font(29)
    lines = textwrap.wrap(text, width=70)[:3]
    line_h = CAPTION_LINE_H
    box_h = caption_height(text)
    top = 64 if at_top else H - box_h - 28
    widths = [d.textlength(line, font=font) for line in lines]
    box_w = max(widths) + 56
    left = (W - box_w) / 2
    d.rounded_rectangle((left, top, left + box_w, top + box_h), radius=14, fill=(12, 15, 24, 205))
    for i, line in enumerate(lines):
        d.text(((W - widths[i]) / 2, top + 14 + i * line_h), line, font=font, fill=(255, 255, 255, 255))
    small = _font(17)
    cw = d.textlength(chip, font=small) + 26
    d.rounded_rectangle((W - 20 - cw, 18, W - 20, 50), radius=9, fill=(12, 15, 24, 150))
    d.text((W - 7 - cw, 23), chip, font=small, fill=(230, 233, 240, 255))
    return ov


def covers_caption(marks: list[Mark], cam: tuple, text: str) -> bool:
    """Would the bottom caption sit on top of something this shot highlights?"""
    zoom = W / (cam[2] - cam[0])
    limit = H - caption_height(text) - 40
    for m in marks:
        if m.role in FOCUS_ROLES and (m.rect[1] + m.rect[3] - cam[1]) * zoom > limit:
            return True
    return False


def card(kicker: str, title: str, lines: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    for y in range(H):  # soft vertical gradient
        c = int(16 + 18 * y / H)
        d.line([(0, y), (W, y)], fill=(c, c + 4, c + 18))
    d.rectangle((96, 214, 104, 214 + 150), fill=RING)
    d.text((132, 210), kicker, font=_font(26), fill=(170, 178, 196))
    d.text((128, 248), title, font=_font(64, bold=True), fill=(255, 255, 255))
    for i, line in enumerate(lines):
        d.text((132, 350 + i * 38), line, font=_font(24), fill=(190, 197, 212))
    return img


# --- timeline --------------------------------------------------------------------

@dataclass
class SceneSlot:
    capture: SceneCapture
    duration: float
    voice: Path | None


@dataclass
class Rendered:
    path: Path
    duration: float
    timeline: list[dict]  # scene_id, start, end


TITLE_S, OUTRO_S, XFADE_S, VOICE_LEAD_S = 2.2, 3.2, 0.3, 0.25


def _frames_for_scene(slot: SceneSlot, n_scenes: int, prev_cam: tuple | None, iw: int, ih: int):
    """Yield (key, builder) per output frame. Equal keys mean an identical frame."""
    cap = slot.capture
    images = [decorate(kf) for kf in cap.frames]
    cams = [focus_rect(kf.marks, iw, ih) for kf in cap.frames]
    full = (0, 0, iw, ih)
    start_cam = prev_cam or full
    dur = slot.duration
    zoom_s = min(1.1, 0.32 * dur)
    # When each keyframe takes over: the first holds through the zoom-in, the rest share the remaining time.
    first_hold = zoom_s + (0.7 if len(images) > 1 else dur)
    rest = max(0.0, dur - first_hold)
    starts = [0.0] + [first_hold + i * rest / max(1, len(images) - 1) for i in range(len(images) - 1)]
    chip = f"{cap.index + 1}/{n_scenes}  ·  {cap.page_path}"
    overlays = {top: caption_overlay(cap.narration, chip, at_top=top) for top in (False, True)}
    placement = [covers_caption(kf.marks, cam, cap.narration) for kf, cam in zip(cap.frames, cams)]
    n = int(round(dur * FPS))
    for f in range(n):
        t = f / FPS
        i = max(k for k, st in enumerate(starts) if st <= t)
        if i == 0:
            progress = (t - 0.12) / zoom_s
            cam = cams[0] if progress >= 1 else lerp_rect(start_cam, cams[0], ease(progress))
            blend = 1.0
        else:
            local = t - starts[i]
            cam = cams[i] if local >= 0.55 else lerp_rect(cams[i - 1], cams[i], ease(local / 0.55))
            blend = ease(local / 0.25)
        settled = blend >= 1.0 and cam == cams[i]
        key = (cap.id, i, round(blend, 2), tuple(round(v) for v in cam), settled)

        def build(i=i, blend=blend, cam=cam, settled=settled):
            resample = Image.LANCZOS if settled else Image.BILINEAR
            frame = images[i].resize((W, H), resample, box=cam)
            if blend < 1.0:
                frame = Image.blend(images[i - 1].resize((W, H), resample, box=cam), frame, blend)
            frame = frame.convert("RGBA")
            frame.alpha_composite(overlays[placement[i]])
            return frame.convert("RGB")

        yield key, build
    return cams[-1]


def render(slots: list[SceneSlot], out: Path, audio: Path, title: Image.Image, outro: Image.Image,
           poster: Path | None = None) -> Rendered:
    first = Image.open(io.BytesIO(slots[0].capture.frames[0].png))
    iw, ih = first.size
    cmd = [ffmpeg_exe(), "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio),
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    timeline = []
    written = 0
    last: Image.Image = Image.new("RGB", (W, H), (0, 0, 0))
    poster_at = len(slots) // 2

    def emit(img: Image.Image, data: bytes | None = None):
        nonlocal written, last
        proc.stdin.write(data if data is not None else img.tobytes())
        written += 1
        last = img

    try:
        # Title card, fading in from black.
        black = Image.new("RGB", (W, H), (0, 0, 0))
        for f in range(int(TITLE_S * FPS)):
            a = ease(f / (0.45 * FPS))
            emit(Image.blend(black, title, a) if a < 1 else title)

        prev_cam, prev_page = None, None
        for idx, slot in enumerate(slots):
            start = written / FPS
            fade_from = last
            cam_in = prev_cam if prev_page == slot.capture.page_path else None
            cache_key, cache_img, cache_bytes = None, None, None
            gen = _frames_for_scene(slot, len(slots), cam_in, iw, ih)
            f = 0
            while True:
                try:
                    key, build = next(gen)
                except StopIteration as stop:
                    prev_cam = stop.value
                    break
                if key != cache_key:
                    cache_key, cache_img = key, build()
                    cache_bytes = cache_img.tobytes()
                t = f / FPS
                if cam_in is None and t < XFADE_S:  # cross-fade from the previous scene
                    emit(Image.blend(fade_from, cache_img, ease(t / XFADE_S)))
                else:
                    emit(cache_img, cache_bytes)
                if poster and idx == poster_at and f == int(slot.duration * FPS * 0.6):
                    cache_img.save(poster, quality=90)
                f += 1
            prev_page = slot.capture.page_path
            timeline.append({"scene_id": slot.capture.id, "start": round(start, 2), "end": round(written / FPS, 2)})

        fade_from = last
        for f in range(int(OUTRO_S * FPS)):
            t = f / FPS
            img = Image.blend(fade_from, outro, ease(t / 0.4)) if t < 0.4 else outro
            if t > OUTRO_S - 0.5:
                img = Image.blend(img, black, ease((t - (OUTRO_S - 0.5)) / 0.5))
            emit(img)
    finally:
        proc.stdin.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {code}")
    return Rendered(path=out, duration=round(written / FPS, 2), timeline=timeline)


def write_srt(timeline: list[dict], slots: list[SceneSlot], path: Path) -> None:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    blocks = []
    for n, (t, slot) in enumerate(zip(timeline, slots), 1):
        blocks.append(f"{n}\n{ts(t['start'] + VOICE_LEAD_S)} --> {ts(t['end'] - 0.15)}\n{slot.capture.narration}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
