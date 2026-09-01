"""Image and video generation.

Two-tier design
---------------
1. **Offline procedural renderer** (default, always available): renders a real
   PNG / animated GIF on this machine from the prompt — no network, no API key,
   no model download. Encoders are written here from the format specs
   (PNG: ISO/IEC 15948 chunks + zlib; GIF: 89a + LZW), so the artifact that
   arrives in the browser is a genuine, standards-compliant file.
2. **Provider adapters** for real text-to-image / text-to-video models
   (OpenAI images, Replicate). Selected by config, invoked over HTTP, and every
   failure degrades back to tier 1 instead of failing the request.

The point of tier 1 is not to pretend to be a diffusion model — it is that a
"generate me a poster for X" request still produces a usable file on a plane,
and that the whole pipeline (queue -> provider -> bytes -> blob store -> artifact
row -> sync metadata -> gallery) is exercised and testable without credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# PNG (24-bit RGB) — real compression, standard chunks
# ---------------------------------------------------------------------------
def encode_png(rgb: np.ndarray) -> bytes:
    """``rgb`` is (H, W, 3) uint8 -> PNG bytes."""
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected (H,W,3) array, got {rgb.shape}")
    h, w, _ = rgb.shape
    data = np.ascontiguousarray(rgb, dtype=np.uint8)
    # every scanline is prefixed by a filter-type byte (0 = None here)
    raw = b"".join(b"\x00" + data[y].tobytes() for y in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit truecolour
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def decode_png(bytes_in: bytes) -> np.ndarray:
    """Reference decoder, used by tests to prove the encoder is spec-correct."""
    if bytes_in[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = bytearray()
    w = h = 0
    while pos < len(bytes_in):
        (length,) = struct.unpack(">I", bytes_in[pos : pos + 4])
        tag = bytes_in[pos + 4 : pos + 8]
        payload = bytes_in[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", bytes_in[pos + 8 + length : pos + 12 + length])
        if zlib.crc32(tag + payload) & 0xFFFFFFFF != crc:
            raise ValueError(f"CRC mismatch in {tag!r} chunk")
        if tag == b"IHDR":
            w, h, depth, colour = struct.unpack(">IIBB", payload[:10])
            if (depth, colour) != (8, 2):
                raise ValueError("this decoder only handles 8-bit truecolour PNG")
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = w * 3
    out = np.zeros((h, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int16)
    for y in range(h):
        line = raw[y * (stride + 1) : y * (stride + 1) + stride + 1]
        ftype, scan = line[0], np.frombuffer(line[1:], dtype=np.uint8).astype(np.int16)
        if ftype == 0:
            cur = scan
        elif ftype == 1:  # Sub
            cur = np.cumsum(scan, dtype=np.int16) & 0xFF
        elif ftype == 2:  # Up
            cur = (scan + prev) & 0xFF
        else:
            raise ValueError(f"unsupported filter type {ftype}")
        out[y] = cur.astype(np.uint8)
        prev = cur
    return out.reshape(h, w, 3)


# ---------------------------------------------------------------------------
# GIF89a (palette, animation, real LZW)
# ---------------------------------------------------------------------------
def _gif_lzw_encode(indices: bytes, min_code_size: int = 8) -> bytes:
    """GIF's variable-width LZW with clear/EOI codes."""
    clear = 1 << min_code_size
    eoi = clear + 1
    out = bytearray()
    bit_buf = 0
    bit_count = 0

    def emit(code: int, width: int) -> None:
        nonlocal bit_buf, bit_count
        bit_buf |= code << bit_count
        bit_count += width
        while bit_count >= 8:
            out.append(bit_buf & 0xFF)
            bit_buf >>= 8
            bit_count -= 8

    table: dict[bytes, int] = {bytes([i]): i for i in range(clear)}
    next_code = eoi + 1
    width = min_code_size + 1
    emit(clear, width)
    prefix = b""
    for byte in indices:
        candidate = prefix + bytes([byte])
        if candidate in table:
            prefix = candidate
            continue
        emit(table[prefix], width)
        table[candidate] = next_code
        next_code += 1
        if next_code > (1 << width):
            if width >= 12:
                emit(clear, width)
                table = {bytes([i]): i for i in range(clear)}
                next_code = eoi + 1
                width = min_code_size + 1
            else:
                width += 1
        prefix = bytes([byte])
    if prefix:
        emit(table[prefix], width)
    emit(eoi, width)
    if bit_count:
        out.append(bit_buf & 0xFF)
    return bytes(out)


def encode_gif(frames: list[np.ndarray], *, width: int, height: int, delay_cs: int = 12, loop: int = 0) -> bytes:
    """Animated GIF89a. ``frames`` are RGB uint8 arrays of the same size.

    Frames are quantised to a shared 256-colour palette; the first 256 distinct
    colours win, remaining pixels snap to the nearest winner (an octree would be
    better quality, this is simpler and deterministic).
    """
    flat = np.concatenate([np.asarray(f, dtype=np.uint8).reshape(-1, 3) for f in frames], axis=0)
    colours, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(-counts)
    palette = colours[order[:256]] if len(colours) >= 256 else colours
    if len(palette) < 2:
        palette = np.vstack([palette, np.array([[255, 255, 255]], dtype=np.uint8)])
    size = len(palette)
    bits = max(2, int(np.ceil(np.log2(size))))
    palette = np.vstack([palette, np.zeros((2**bits - size, 3), dtype=np.uint8)])[: 2**bits]

    # nearest-colour snap via a small lookup on the 5-bit cube to stay fast
    palette_key = (palette.astype(np.int32) >> 3)
    pal_hash = palette_key @ np.array([32 * 32, 32, 1], dtype=np.int64)
    lut: dict[int, int] = {}
    for i, h in enumerate(pal_hash):
        lut.setdefault(int(h), i)
    snapped = np.empty(flat.shape[0], dtype=np.uint8)
    per_frame = width * height
    palette_arr = palette.astype(np.int16)
    for f in range(len(frames)):
        seg = flat[f * per_frame : (f + 1) * per_frame]
        seg_key = (seg >> 3) @ np.array([32 * 32, 32, 1], dtype=np.int64)
        idx = np.array([lut.get(int(k), -1) for k in seg_key], dtype=np.int32)
        missing = np.where(idx < 0)[0]
        if len(missing):
            nearest = np.argmin(
                np.abs(palette_arr[None, :, :].astype(np.int16) - seg[missing, None, :].astype(np.int16)).astype(np.int32).sum(axis=2),
                axis=1,
            )
            idx[missing] = nearest
        snapped[f * per_frame : (f + 1) * per_frame] = idx.astype(np.uint8)

    out = bytearray()
    out += b"GIF89a"
    out += struct.pack("<HH", width, height)
    out += struct.pack("BBB", 0x80 | (bits - 1), 0, 0)  # global colour table, no bg
    out += palette.tobytes()
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01" + struct.pack("<H", loop) + b"\x00"
    for f in range(len(frames)):
        out += b"\x21\xf9\x04\x04" + struct.pack("<HB", delay_cs, 0) + b"\x00"
        out += b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"
        frame_indices = snapped[f * per_frame : (f + 1) * per_frame].tobytes()
        block = _gif_lzw_encode(frame_indices, bits)
        out += bytes([bits])
        for i in range(0, len(block), 255):
            chunk = block[i : i + 255]
            out += bytes([len(chunk)]) + chunk
        out += b"\x00"
    out += b"\x3b"
    return bytes(out)


def gif_lzw_decode_payload(payload: bytes, *, min_code_size: int = 8, expected_pixels: int = 1 << 30) -> bytes:
    """Reference GIF-LZW decoder. Tests use it to prove the encoder round-trips."""
    clear = 1 << min_code_size
    eoi = clear + 1
    width = min_code_size + 1
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    out = bytearray()
    bit_buf = 0
    bit_count = 0
    prev: bytes | None = None
    i = 0
    while i < len(payload) or bit_count >= width:
        while bit_count < width and i < len(payload):
            bit_buf |= payload[i] << bit_count
            i += 1
            bit_count += 8
        code = bit_buf & ((1 << width) - 1)
        bit_buf >>= width
        bit_count -= width
        if code == clear:
            table = [bytes([j]) for j in range(clear)] + [b"", b""]
            width = min_code_size + 1
            prev = None
            continue
        if code == eoi:
            break
        if code < len(table) and table[code]:
            entry = table[code]
        elif prev is not None:
            entry = prev + prev[:1]
        else:
            break
        out += entry
        if prev is not None:
            table.append(prev + entry[:1])
            if len(table) >= (1 << width) and width < 12:
                width += 1
        prev = entry
        if len(out) >= expected_pixels:
            break
    return bytes(out[:expected_pixels])


def _gif_image_payload(data: bytes) -> tuple[bytes, int]:
    """Extract (lzw_payload, min_code_size) of the first frame of a GIF.

    Locates the image descriptor by structure rather than by scanning for 0x2C,
    which also occurs as a data byte inside the colour table.
    """
    pos = 6 + 4  # header(6) + logical screen descriptor: width(2) height(2) -> packed
    packed = data[pos]
    pos += 3  # skip packed, background colour index, pixel aspect ratio
    if packed & 0x80:
        pos += 3 * (2 << (packed & 0x07))
    while pos < len(data):
        introducer = data[pos]
        if introducer == 0x2C:
            pos += 1 + 9
            min_code_size = data[pos]
            pos += 1
            payload = bytearray()
            while True:
                size = data[pos]
                if size == 0:
                    break
                payload += data[pos + 1 : pos + 1 + size]
                pos += 1 + size
            return bytes(payload), min_code_size
        if introducer == 0x21:  # extension: sub-blocks
            pos += 2
            while data[pos]:
                pos += 1 + data[pos]
            pos += 1
        elif introducer == 0x3B:
            break
        else:
            pos += 1
    raise ValueError("no image descriptor found")


# ---------------------------------------------------------------------------
# procedural rendering
# ---------------------------------------------------------------------------
def _seed(text: str, extra: str = "") -> int:
    return int.from_bytes(hashlib.sha256(f"{text}|{extra}".encode()).digest()[:8], "big")


def _palette(rng: np.random.Generator, *, dark: bool) -> np.ndarray:
    base = rng.uniform(0.05, 0.95, size=3) if not dark else rng.uniform(0.02, 0.4, size=3)
    hues = [(base + np.array([d, e, f])) % 1.0 for d, e, f in ((0, 0, 0), (0.12, 0.05, -0.08), (-0.1, 0.18, 0.06), (0.3, -0.2, 0.15))]
    return np.clip(np.array([[int(c * 255) for c in h] for h in hues]), 0, 255).astype(np.uint8)


def render_image(
    prompt: str,
    *,
    width: int = 800,
    height: int = 500,
    style: str = "poster",
    seed_extra: str = "",
    draw_text: str | None = None,
) -> np.ndarray:
    """Deterministic abstract composition seeded by the prompt.

    Motifs are chosen from prompt keywords (see ``_MOTIFS``) so the output is
    *responsive* to the request rather than random noise: the same prompt always
    returns the same image, and "sunset over mountains" really does draw
    mountains. Text is rendered with a built-in 5x7 bitmap font, so a caption is
    legible without any font file on the system.
    """
    rng = np.random.default_rng(_seed(prompt, seed_extra or style))
    dark = not re.search(r"\bbright\b|\bday\b|\bsunrise\b|\bsunset\b|\bsky\b|\bsummer\b", prompt, re.I)
    img = np.zeros((height, width, 3), dtype=np.float64)

    # background gradient
    top, bottom = _palette(rng, dark=dark)[0].astype(float), _palette(rng, dark=dark)[1].astype(float)
    ramp = np.linspace(0, 1, height)[:, None, None]
    img += (top[None, None, :] * (1 - ramp) + bottom[None, None, :] * ramp)

    words = {w.lower() for w in re.findall(r"[a-z]{3,}", prompt.lower())}
    motifs = [m for m, keys in _MOTIFS.items() if keys & words]
    if not motifs:
        motifs = ["orbits", "grid", "waves"][rng.integers(0, 3) % 3 :][:1] or ["orbits"]

    for motif in motifs[:4]:
        img = _MOTIF_DRAW[motif](img, rng, palette=_palette(rng, dark=dark), prompt=prompt)

    if style in ("poster", "card"):
        border = max(6, int(min(width, height) * 0.02))
        edge = _palette(rng, dark=dark)[3].astype(float)
        for arr, _sl in ((img[:border], None), (img[-border:], None), (img[:, :border], None), (img[:, -border:], None)):
            arr[:] = 0.75 * arr + 0.25 * edge
    if style == "blueprint":
        img = 0.35 * img + 0.65 * np.array([12, 30, 64], dtype=float)
        grid = np.zeros_like(img[:, :, 0])
        grid[:: 16, :] += 1
        grid[:, :: 16] += 1
        img[:, :, 2] += 26 * (grid > 0)
    img = np.clip(img, 0, 255)
    out = img.astype(np.uint8)
    caption = draw_text if draw_text is not None else _caption(prompt)
    if caption:
        out = _draw_caption(out, caption)
    return out


_MOTIFS: dict[str, set[str]] = {
    "mountains": {"mountain", "mountains", "valley", "hiking", "sunset", "sunrise", "sky", "landscape", "alps", "peak"},
    "waves": {"wave", "waves", "ocean", "sea", "water", "beach", "lake", "river", "rain", "flow"},
    "grid": {"grid", "data", "dashboard", "chart", "graph", "table", "code", "matrix", "blueprint", "plan", "schedule"},
    "orbits": {"space", "planet", "orbits", "orbit", "moon", "star", "stars", "universe", "cosmic", "rocket", "galaxy"},
    "leaves": {"leaf", "leaves", "plant", "garden", "forest", "tree", "nature", "jungle", "flower", "growth"},
    "flames": {"fire", "flame", "flames", "heat", "lava", "ember", "forge", "energy", "power"},
    "circuits": {"circuit", "circuitry", "chip", "cpu", "robot", "ai", "machine", "network", "server", "tech"},
    "confetti": {"party", "celebrate", "birthday", "confetti", "festival", "wedding", "fun", "game"},
}


def _canvas_ops(fn: Callable[[np.ndarray, np.random.Generator, np.ndarray, str], np.ndarray]) -> Callable:
    def wrapped(img, rng, *, palette, prompt):
        return fn(img, rng, palette, prompt)

    return wrapped


@_canvas_ops
def _mountains(img, rng, palette, prompt):
    h, w, _ = img.shape
    for layer in range(3):
        base_y = h * (0.52 + 0.13 * layer)
        amp = h * (0.16 - 0.03 * layer)
        phase = rng.uniform(0, 2 * np.pi)
        x = np.linspace(0, 1, w)
        ridge = base_y + amp * np.sin(x * (3 + layer * 2) * np.pi + phase) - amp * 0.5 * np.abs(np.sin(x * (11 + layer) + phase))
        colour = palette[(layer + 1) % len(palette)].astype(float) * (0.55 + 0.18 * layer)
        for xi in range(w):
            y = int(max(0, min(h - 1, ridge[xi])))
            img[y:, xi] = 0.35 * img[y:, xi] + 0.65 * colour
    sun_r = int(h * rng.uniform(0.06, 0.13))
    sx, sy = int(w * rng.uniform(0.15, 0.85)), int(h * rng.uniform(0.12, 0.3))
    yy, xx = np.ogrid[:h, :w]
    glow = np.clip(1.0 - np.hypot(xx - sx, yy - sy) / (sun_r * 4.5), 0, 1) ** 2
    img += glow[:, :, None] * np.array([120, 95, 40], dtype=float)
    return img


@_canvas_ops
def _waves(img, rng, palette, prompt):
    h, w, _ = img.shape
    yy, xx = np.mgrid[0:h, 0:w]
    for k in range(5):
        freq = rng.uniform(0.006, 0.03)
        phase = rng.uniform(0, 2 * np.pi)
        band = np.sin(xx * freq * 2 * np.pi + yy * freq * 0.6 * np.pi + phase)
        mask = np.clip(1.0 - np.abs(band) * 6.0, 0, 1)
        img += mask[:, :, None] * palette[k % len(palette)].astype(float) * 0.5
    return img


@_canvas_ops
def _grid(img, rng, palette, prompt):
    h, w, _ = img.shape
    step = int(rng.choice([16, 20, 24, 32]))
    for y in range(0, h, step):
        img[y, :, :] = 0.86 * img[y] + 0.14 * palette[2].astype(float)
    for x in range(0, w, step):
        img[:, x, :] = 0.86 * img[:, x] + 0.14 * palette[2].astype(float)
    for _ in range(int(rng.integers(4, 11))):
        bw, bh = int(rng.uniform(0.08, 0.3) * w), int(rng.uniform(0.06, 0.22) * h)
        x0, y0 = int(rng.uniform(0, max(1, w - bw))), int(rng.uniform(0, max(1, h - bh)))
        colour = palette[int(rng.integers(0, len(palette)))].astype(float)
        img[y0 : y0 + bh, x0 : x0 + bw] = 0.3 * img[y0 : y0 + bh, x0 : x0 + bw] + 0.7 * colour
        img[y0 : y0 + 2, x0 : x0 + bw] = colour * 1.15
    return img


@_canvas_ops
def _orbits(img, rng, palette, prompt):
    h, w, _ = img.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w * rng.uniform(0.35, 0.65), h * rng.uniform(0.35, 0.65)
    r = np.hypot(xx - cx, yy - cy)
    for ring in range(1, 5):
        radius = min(w, h) * 0.09 * ring
        band = np.clip(1.0 - np.abs(r - radius) / (2.5 + ring), 0, 1)
        angle = rng.uniform(0, 2 * np.pi)
        planet_a = angle + rng.uniform(-0.6, 0.6)
        px, py = cx + radius * np.cos(planet_a), cy + radius * np.sin(planet_a)
        glow = np.clip(1.0 - np.hypot(xx - px, yy - py) / (10 + 6 * ring), 0, 1)
        colour = palette[ring % len(palette)].astype(float)
        img += (0.22 * band + 0.9 * glow)[:, :, None] * colour
    core = np.clip(1.0 - r / (min(w, h) * 0.11), 0, 1) ** 1.5
    img += core[:, :, None] * np.array([210, 180, 120], dtype=float)
    for _ in range(int(rng.integers(40, 130))):
        sx, sy = int(rng.uniform(0, w)), int(rng.uniform(0, h))
        img[sy, sx] = np.clip(img[sy, sx] + 150, 0, 255)
    return img


@_canvas_ops
def _leaves(img, rng, palette, prompt):
    h, w, _ = img.shape
    for _ in range(int(rng.integers(22, 60))):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        rx, ry = rng.uniform(0.02, 0.09) * w, rng.uniform(0.05, 0.18) * h
        ang = rng.uniform(0, np.pi)
        t = np.linspace(0, 2 * np.pi, 90)
        pts = np.stack([rx * np.cos(t), ry * np.sin(t)], axis=1) @ np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
        pts[:, 0] += cx
        pts[:, 1] += cy
        colour = palette[int(rng.integers(0, len(palette)))].astype(float) * rng.uniform(0.5, 1.0)
        for i in range(len(pts) - 1):
            x0, y0 = np.clip(pts[i], [0, 0], [w - 1, h - 1]).astype(int)
            x1, y1 = np.clip(pts[i + 1], [0, 0], [w - 1, h - 1]).astype(int)
            n = max(abs(x1 - x0), abs(y1 - y0), 1)
            xs = np.linspace(x0, x1, n).astype(int)
            ys = np.linspace(y0, y1, n).astype(int)
            img[ys, xs] = 0.45 * img[ys, xs] + 0.55 * colour
    return img


@_canvas_ops
def _flames(img, rng, palette, prompt):
    h, w, _ = img.shape
    yy, _xx = np.mgrid[0:h, 0:w]
    noise = rng.standard_normal((h // 4 + 1, w // 4 + 1))
    noise = np.kron(noise, np.ones((4, 4)))[:h, :w]
    heat = np.clip(1.0 - yy / h + 0.35 * noise / (noise.std() + 1e-9), 0, 1) ** 2.2
    ramp = np.stack([np.ones_like(heat), heat, heat**2.5], axis=-1) * np.array([255, 130, 60], dtype=float)
    return 0.45 * img + 0.75 * ramp * heat[:, :, None] * np.array([1.0, 0.75, 0.5])


@_canvas_ops
def _circuits(img, rng, palette, prompt):
    h, w, _ = img.shape
    step = int(rng.choice([14, 18, 22]))
    colour = palette[3].astype(float)
    for _ in range(int(rng.integers(14, 34))):
        x = int(rng.integers(0, max(1, w // step)) * step)
        y = int(rng.integers(0, max(1, h // step)) * step)
        length = int(rng.integers(3, 14)) * step
        horizontal = rng.random() > 0.5
        if horizontal:
            img[min(y, h - 1), x : min(x + length, w)] = colour
            node = (min(y, h - 1), min(x + length, w - 1))
        else:
            img[x : min(x + length, h), min(y, w - 1)] = colour
            node = (min(x + length, h - 1), min(y, w - 1))
        img[max(0, node[0] - 2) : node[0] + 3, max(0, node[1] - 2) : node[1] + 3] = np.clip(colour * 1.4, 0, 255)
    return img


@_canvas_ops
def _confetti(img, rng, palette, prompt):
    h, w, _ = img.shape
    for _ in range(int(rng.integers(120, 420))):
        size = int(rng.integers(3, 14))
        x, y = int(rng.uniform(0, max(1, w - size))), int(rng.uniform(0, max(1, h - size)))
        colour = palette[int(rng.integers(0, len(palette)))].astype(float) * rng.uniform(0.7, 1.3)
        if rng.random() > 0.5:
            img[y : y + size, x : x + size] = colour
        else:
            t = np.linspace(0, 2 * np.pi, 12)
            xs = (x + size / 2 + size / 2 * np.cos(t)).astype(int)
            ys = (y + size / 2 + size / 2 * np.sin(t)).astype(int)
            img[np.clip(ys, 0, h - 1), np.clip(xs, 0, w - 1)] = colour
    return img


_MOTIF_DRAW = {
    "mountains": _mountains,
    "waves": _waves,
    "grid": _grid,
    "orbits": _orbits,
    "leaves": _leaves,
    "flames": _flames,
    "circuits": _circuits,
    "confetti": _confetti,
}


# --- built-in 5x7 bitmap font, so captions need no system fonts -------------
_FONT_SRC = {
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#", "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.", "D": "####.|#...#|#...#|#...#|#...#|#...#|####.",
    "E": "#####|#....|#....|####.|#....|#....|#####", "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.###.", "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": "#####|..#..|..#..|..#..|..#..|..#..|#####", "J": "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#", "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#...#|#...#|#...#|#...#", "N": "#...#|##..#|#.#.#|#..##|#...#|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.", "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#", "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.", "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.", "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#", "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..", "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    "0": ".###.|#...#|#..##|.#.#.|##..#|#...#|.###.", "1": "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####", "3": "####.|....#|....#|.###.|....#|....#|####.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.", "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": ".###.|#...#|#....|####.|#...#|#...#|.###.", "7": "#####|....#|...#.|..#..|.#...|.#...|.#...",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.", "9": ".###.|#...#|#...#|.####|....#|#...#|.###.",
    " ": ".....|.....|.....|.....|.....|.....|.....", ".": ".....|.....|.....|.....|.....|.##..|.##..",
    ",": ".....|.....|.....|.....|.##..|.##..|.#...", "'": ".#...|.#...|.....|.....|.....|.....|.....",
    "-": ".....|.....|.....|#####|.....|.....|.....", "?": ".###.|#...#|....#|..##.|..#..|.....|..#..",
    "!": "..#..|..#..|..#..|..#..|..#..|.....|..#..", ":": ".....|.##..|.##..|.....|.##..|.##..|.....",
    "&": ".##..|#..#.|.#.#.|..#..|.#.#.|#..#.|.##.#", "/": "....#|...#.|...#.|..#..|.#...|.#...|#....",
    "(": "..#..|.#...|.#...|.#...|.#...|.#...|..#..", ")": "..#..|...#.|...#.|...#.|...#.|...#.|..#..",
    "#": ".#.#.|#####|.#.#.|.#.#.|#####|.#.#.|.....", "+": ".....|..#..|..#..|#####|..#..|..#..|.....",
}
_FONT: dict[str, list[int]] = {
    ch: [int(row.replace(".", "0").replace("#", "1"), 2) for row in blob.split("|")] for ch, blob in _FONT_SRC.items()
}


def text_size(text: str, *, scale: int = 2, gap: int = 1) -> tuple[int, int]:
    return len(text) * (5 + gap) * scale - gap * scale, 7 * scale


def draw_text(img: np.ndarray, text: str, *, x: int, y: int, scale: int = 2, colour=(255, 255, 255), shadow: bool = True) -> np.ndarray:
    out = img.astype(np.float64).copy()
    gap = 1
    pen = x
    if shadow:
        out = draw_text(out, text, x=x + scale, y=y + scale, scale=scale, colour=(0, 0, 0), shadow=False)
    for ch in text.upper():
        glyph = _FONT.get(ch, _FONT[" "])
        for row_i, bits in enumerate(glyph):
            for col in range(5):
                if bits & (1 << (4 - col)):
                    x0, y0 = pen + col * scale, y + row_i * scale
                    out[y0 : y0 + scale, x0 : x0 + scale] = np.array(colour, dtype=float)
        pen += (5 + gap) * scale
    return np.clip(out, 0, 255).astype(np.uint8)


def _caption(prompt: str) -> str:
    """Short, readable caption derived from the prompt."""
    cleaned = re.sub(r"[^\w\s,'&()+\-/.:?]", " ", prompt).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 46:
        cleaned = cleaned[:44].rstrip(" ,.-") + "..."
    return cleaned or "Jarvis"


def _draw_caption(img: np.ndarray, caption: str) -> np.ndarray:
    h, w, _ = img.shape
    scale = max(1, min(3, w // 220))
    tw, th = text_size(caption, scale=scale)
    while tw > w - 24 and scale > 1:
        scale -= 1
        tw, th = text_size(caption, scale=scale)
    x = max(8, min(w - tw - 8, 24))
    y = max(8, h - th - 24)
    pad = 6 * scale
    band = img[max(0, y - pad) : y + th + pad, max(0, x - pad) : min(w, x + tw + pad)].astype(np.float64)
    img[max(0, y - pad) : y + th + pad, max(0, x - pad) : min(w, x + tw + pad)] = np.clip(band * 0.42, 0, 255).astype(np.uint8)
    return draw_text(img, caption, x=x, y=y, scale=scale)


def render_video(prompt: str, *, width: int = 320, height: int = 200, seconds: float = 3.0, fps: int = 8) -> list[np.ndarray]:
    """Frame sequence for the offline renderer: the same motif system, animated by
    a per-frame seed so motion follows the prompt's style rather than drifting randomly."""
    frames = int(max(4, min(64, seconds * fps)))
    out = []
    for i in range(frames):
        frame = render_image(prompt, width=width, height=height, seed_extra=f"f{i}")
        t = i / frames
        # a slow vertical drift + brightness pulse reads as motion without needing a physics model
        shift = int(t * height * 0.06)
        if shift:
            frame = np.concatenate([frame[shift:], frame[:shift]], axis=0)
        frame = np.clip(frame.astype(np.float64) * (1.0 + 0.08 * np.sin(2 * np.pi * t)), 0, 255).astype(np.uint8)
        out.append(frame)
    return out


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
@dataclass
class GenerationResult:
    ok: bool
    provider: str
    kind: str
    blob: bytes | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    frames: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    fallback_used: bool = False
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class LocalProvider:
    name = "local"

    def generate(self, kind: str, prompt: str, *, params: dict[str, Any]) -> GenerationResult:
        started = time.time()
        if kind == "image":
            width = int(params.get("width", 800))
            height = int(params.get("height", 500))
            style = str(params.get("style", "poster"))
            if width * height > 4_000_000:
                return GenerationResult(False, self.name, kind, error=f"requested size {width}x{height} exceeds the 4 MP offline cap")
            img = render_image(prompt, width=width, height=height, style=style, seed_extra=str(params.get("seed", "")), draw_text=params.get("caption"))
            return GenerationResult(
                True, self.name, kind, blob=encode_png(img), mime="image/png", width=width, height=height,
                duration_ms=int((time.time() - started) * 1000), detail={"note": "offline procedural render; set JARVIS_IMAGE_PROVIDER=openai|replicate for model-based output"},
            )
        width = int(params.get("width", 320))
        height = int(params.get("height", 200))
        seconds = float(params.get("seconds", 3.0))
        fps = int(params.get("fps", 8))
        frames = render_video(prompt, width=width, height=height, seconds=seconds, fps=fps)
        gif = encode_gif(frames, width=width, height=height, delay_cs=max(2, int(100 / max(1, fps))))
        return GenerationResult(
            True, self.name, kind, blob=gif, mime="image/gif", width=width, height=height, frames=len(frames),
            duration_ms=int((time.time() - started) * 1000),
            detail={"note": "animated GIF rendered locally; a real text-to-video model (JARVIS_VIDEO_PROVIDER=replicate) produces mp4"},
        )


class OpenAIImageProvider:
    """DALL-E / gpt-image-1 via the images endpoint."""

    name = "openai"

    def __init__(self, settings):
        self.settings = settings

    def generate(self, kind: str, prompt: str, *, params: dict[str, Any]) -> GenerationResult:
        import httpx

        size = f"{int(params.get('width', 1024))}x{int(params.get('height', 1024))}"
        payload = {"model": os.environ.get("JARVIS_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt, "size": size, "n": 1}
        try:
            resp = httpx.post(
                self.settings.llm_base_url.rstrip("/") + "/images/generations",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                timeout=180.0,
            )
            resp.raise_for_status()
            item = resp.json()["data"][0]
            if item.get("b64_json"):
                import base64

                blob = base64.b64decode(item["b64_json"])
            else:
                blob = httpx.get(item["url"], timeout=120.0).content
            return GenerationResult(True, self.name, kind, blob=blob, mime="image/png", width=int(params.get("width", 1024)), height=int(params.get("height", 1024)))
        except Exception as exc:
            return GenerationResult(False, self.name, kind, error=str(exc))


class ReplicateProvider:
    """Any Replicate model by name (works for SDXL and video models alike)."""

    name = "replicate"

    def __init__(self, settings):
        self.settings = settings

    def generate(self, kind: str, prompt: str, *, params: dict[str, Any]) -> GenerationResult:
        import httpx

        token = os.environ.get("REPLICATE_API_TOKEN", "")
        if not token:
            return GenerationResult(False, self.name, kind, error="REPLICATE_API_TOKEN not set")
        model = params.get("model") or os.environ.get(
            "JARVIS_REPLICATE_IMAGE_MODEL" if kind == "image" else "JARVIS_REPLICATE_VIDEO_MODEL",
            "stability-ai/sdxl:39ed52f2a78eaf99942b968f10be6bac2a3bfd78b0f2d0f0e1b4a2d3f4a5b6c7",
        )
        version = None
        if ":" in model:
            model, version = model.split(":", 1)
        input_spec: dict[str, Any] = {"prompt": prompt}
        if kind == "image":
            input_spec.update({"width": int(params.get("width", 1024)), "height": int(params.get("height", 1024))})
        else:
            input_spec.update({"num_frames": int(params.get("frames", 24))})
        base = "https://api.replicate.com/v1"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{base}/models/{model}/predictions" if version is None else f"{base}/predictions"
        body = {"input": input_spec} if version is None else {"version": version, "input": input_spec}
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=60.0)
            resp.raise_for_status()
            prediction = resp.json()
            for _ in range(120):
                if prediction.get("status") in {"succeeded", "failed", "canceled"}:
                    break
                time.sleep(2.0)
                poll = httpx.get(prediction["urls"]["get"], headers=headers, timeout=30.0)
                prediction = poll.json()
            if prediction.get("status") != "succeeded":
                return GenerationResult(False, self.name, kind, error=f"replicate {prediction.get('status')}: {prediction.get('error')}")
            output = prediction["output"]
            if isinstance(output, list):
                output = output[0]
            blob = httpx.get(output, timeout=180.0).content
            return GenerationResult(True, self.name, kind, blob=blob, mime="video/mp4" if kind == "video" else "image/png")
        except Exception as exc:
            return GenerationResult(False, self.name, kind, error=str(exc))


def provider_for(kind: str, settings) -> tuple[Any, str]:
    requested = settings.image_provider if kind == "image" else settings.video_provider
    if kind == "image" and requested == "openai" and settings.llm_api_key:
        return OpenAIImageProvider(settings), requested
    if requested == "replicate":
        return ReplicateProvider(settings), requested
    return LocalProvider(), "local"


class MediaService:
    """Orchestrates generation, blob storage, and replicated artifact metadata."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.blob_dir = settings.blob_dir

    def generate(
        self,
        user_id: str,
        kind: str,
        prompt: str,
        *,
        params: dict[str, Any] | None = None,
        force_offline: bool = False,
    ) -> dict[str, Any]:
        params = dict(params or {})
        provider, requested = ("local", "local") if force_offline else provider_for(kind, self.settings)
        started = time.time()
        result = provider.generate(kind, prompt, params=params)
        fallback = False
        if not result.ok and not isinstance(provider, LocalProvider):
            # a remote model being unavailable must not lose the user's request
            result = LocalProvider().generate(kind, prompt, params=params)
            fallback = True
            requested = f"{requested}->local"
        artifact_id = f"art_{uuid.uuid4().hex[:10]}"
        path: Path | None = None
        if result.ok and result.blob is not None:
            ext = "png" if (result.mime or "").endswith("png") else "mp4" if (result.mime or "").endswith("mp4") else "gif"
            path = self.blob_dir / f"{artifact_id}.{ext}"
            path.write_bytes(result.blob)
        record = {
            "id": artifact_id,
            "user_id": user_id,
            "kind": kind,
            "prompt": prompt,
            "provider": requested,
            "status": "ready" if result.ok else "failed",
            "mime": result.mime,
            "width": result.width,
            "height": result.height,
            "frames": result.frames,
            "duration_ms": result.duration_ms or int((time.time() - started) * 1000),
            "params": params,
            "created_at": time.time(),
            "error": result.error,
            "fallback_used": fallback,
            "bytes": len(result.blob) if result.blob else 0,
            "detail": result.detail or {},
        }
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO artifacts(id, user_id, kind, prompt, provider, status, path, mime, width, height, duration_ms, params, created_at, error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id, user_id, kind, prompt, record["provider"], record["status"],
                    str(path) if path else None, result.mime, result.width, result.height,
                    record["duration_ms"], json.dumps(params), record["created_at"], result.error,
                ),
            )
        # metadata syncs so every device lists the same gallery; blob bytes stay
        # on the generating device and are fetched on demand (see /artifacts/{id}/file)
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=user_id,
            entity="memory",
            entity_key=f"artifact:{artifact_id}",
            field=None,
            kind="set",
            payload={"body": f"[{kind}] {prompt}", "tags": [f"artifact:{kind}", f"status:{record['status']}"], "source": f"media:{record['provider']}"},
        )
        return {k: v for k, v in record.items() if k != "user_id"}

    def list(self, user_id: str, *, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.db.query(
            """SELECT id, kind, prompt, provider, status, mime, width, height, duration_ms, created_at, params, error
               FROM artifacts WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit),
        )
        return [
            {
                **{k: r[k] for k in r.keys() if k != "params"},
                "params": self.db.jloads(r["params"], {}),
                "url": f"/api/artifacts/{r['id']}/file",
            }
            for r in rows
        ]

    def read_blob(self, artifact_id: str) -> tuple[bytes, str] | None:
        row = self.db.one("SELECT path, mime FROM artifacts WHERE id=?", (artifact_id,))
        if row is None or not row["path"] or not Path(row["path"]).exists():
            return None
        return Path(row["path"]).read_bytes(), row["mime"] or "application/octet-stream"
