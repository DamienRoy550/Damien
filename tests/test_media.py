"""Generated media: real files, honest encoders, deterministic prompts, sync metadata."""

from __future__ import annotations

import json
import struct
import time
import zlib

import numpy as np
import pytest
from jarvis.media import (
    MediaService,
    _gif_image_payload,
    decode_png,
    encode_gif,
    encode_png,
    gif_lzw_decode_payload,
    render_image,
    render_video,
)


def rgb_pattern(w: int = 37, h: int = 19) -> np.ndarray:
    x, y = np.meshgrid(np.arange(w), np.arange(h))
    return np.stack([(x * 7) % 256, (y * 13) % 256, ((x + y) * 5) % 256], axis=-1).astype(np.uint8)


# ------------------------------------------------------------------- formats
def test_png_encoder_output_is_valid_and_lossless():
    img = rgb_pattern()
    data = encode_png(img)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # chunk order and CRCs must satisfy the spec
    pos = 8
    seen = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, tag
        seen.append(tag)
        pos += 12 + length
    assert seen[:2] == [b"IHDR", b"IDAT"] and seen[-1] == b"IEND"
    assert np.array_equal(decode_png(data), img), "PNG must round-trip bit-exact"


def test_png_crc_is_actually_verified():
    data = bytearray(encode_png(rgb_pattern()))
    data[20] ^= 0xFF  # corrupt an IDAT byte
    with pytest.raises((ValueError, zlib.error)):
        decode_png(bytes(data))


def test_gif_encoder_produces_a_real_animated_file():
    frames = [rgb_pattern(), rgb_pattern()[::-1].copy(), np.roll(rgb_pattern(), 4, axis=1)]
    data = encode_gif(frames, width=37, height=19, delay_cs=10)
    assert data[:6] == b"GIF89a"
    assert data[-1:] == b"\x3b"
    assert data.count(b"\x2c\x00\x00\x00\x00") >= 1
    assert data.count(b"\x21\xf9\x04") == 3, "one graphics control extension per frame"
    payload, min_code_size = _gif_image_payload(data)
    decoded = gif_lzw_decode_payload(payload, min_code_size=min_code_size, expected_pixels=37 * 19)
    assert len(decoded) == 37 * 19, "LZW stream must decode to exactly one frame of pixels"
    assert len(payload) < 37 * 19, "LZW must actually compress, not just wrap the bytes"


def test_gif_rejects_wrong_shape():
    with pytest.raises(ValueError):
        encode_gif([np.zeros((4, 4), dtype=np.uint8)], width=4, height=4)


# -------------------------------------------------------------- determinism
def test_same_prompt_renders_the_same_image():
    a = render_image("a poster about coastal erosion for the office wall")
    b = render_image("a poster about coastal erosion for the office wall")
    assert np.array_equal(a, b)
    c = render_image("a poster about coastal erosion for the OFFICE WALL")  # case differs
    assert not np.array_equal(a, c), "the renderer is responsive to the prompt text, not to a fixed seed"


def test_prompt_content_selects_motifs():
    mountains = render_image("sunset over mountains", width=200, height=140)
    circuits = render_image("circuit chip server network", width=200, height=140)
    assert not np.array_equal(mountains, circuits)
    from jarvis.media import _MOTIFS

    words = {w for w in "sunset over mountains".lower().split()}
    assert "mountains" in _MOTIFS and words & _MOTIFS["mountains"]


def test_styles_produce_different_output():
    poster = render_image("grid data dashboard", style="poster")
    blueprint = render_image("grid data dashboard", style="blueprint")
    assert poster.shape == blueprint.shape
    assert not np.array_equal(poster, blueprint)


def test_video_frames_are_animated_but_coherent():
    frames = render_video("calm ocean waves", width=96, height=64, seconds=1.2, fps=6)
    assert 4 <= len(frames) <= 64
    assert all(f.shape == (64, 96, 3) for f in frames)
    assert not np.array_equal(frames[0], frames[-1]), "frames must actually move"


# ------------------------------------------------------------------- service
def test_generate_image_stores_blob_and_metadata(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    media = MediaService(db, settings)
    result = media.generate("u1", "image", "mountains at dusk", params={"width": 240, "height": 160})
    assert result["status"] == "ready"
    assert result["provider"] == "local"
    assert result["bytes"] > 1000
    blob, mime = media.read_blob(result["id"])
    assert mime == "image/png"
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    row = db.one("SELECT * FROM artifacts WHERE id=?", (result["id"],))
    assert row["user_id"] == "u1" and row["prompt"] == "mountains at dusk"
    # metadata is journalled so the gallery appears on the other devices too
    op = db.one("SELECT payload FROM oplog WHERE entity='memory' ORDER BY seq DESC LIMIT 1")
    assert op and "mountains at dusk" in json.loads(op["payload"])["body"]


def test_generate_video_writes_a_playable_gif(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    media = MediaService(db, settings)
    result = media.generate("u1", "video", "waves loop", params={"width": 120, "height": 80, "seconds": 1.0, "fps": 5})
    assert result["status"] == "ready"
    blob, mime = media.read_blob(result["id"])
    assert mime == "image/gif" and blob[:6] == b"GIF89a"


def test_oversized_request_is_refused_not_attempted(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    media = MediaService(db, settings)
    result = media.generate("u1", "image", "huge", params={"width": 4000, "height": 4000})
    assert result["status"] == "failed" and "4 MP" in result["error"]


def test_remote_provider_failure_falls_back_to_offline_render(db, settings, monkeypatch):
    """An unreachable model must not cost the user the request."""
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    monkeypatch.setattr(settings, "image_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1")  # nothing listening
    media = MediaService(db, settings)
    result = media.generate("u1", "image", "fallback check", params={"width": 160, "height": 120})
    assert result["status"] == "ready", result
    assert result["fallback_used"] is True or "local" in result["provider"]
    assert result["duration_ms"] < 5000


def test_replicate_provider_requires_a_token_and_says_so(db, settings, monkeypatch):
    from jarvis.media import ReplicateProvider

    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    result = ReplicateProvider(settings).generate("video", "wave", params={})
    assert result.ok is False and "REPLICATE_API_TOKEN" in result.error


def test_local_provider_is_named_honestly_in_results(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    result = MediaService(db, settings).generate("u1", "image", "honest labelling", params={"width": 160, "height": 120})
    assert result["provider"] == "local"
    assert "offline procedural render" in result["detail"]["note"]
