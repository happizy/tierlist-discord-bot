import io
from dataclasses import replace

import pytest
from PIL import Image

from tierbot.models import Item, UserError
from tierbot.render import MAX_UPLOAD_BYTES, board_rows, normalize_image, render_board


def test_normalize_image(image_bytes):
    normalized = normalize_image(image_bytes)
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "PNG"
        assert image.size == (256, 192)
        assert image.mode == "RGBA"


@pytest.mark.parametrize("data", [b"not an image", b"<svg></svg>", b"x" * (MAX_UPLOAD_BYTES + 1)])
def test_reject_invalid_uploads(data):
    with pytest.raises(UserError):
        normalize_image(data)


def test_reject_pixel_bomb_without_decoding():
    import struct
    import zlib

    ihdr = struct.pack("!IIBBBBB", 6000, 6000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    data = b"\x89PNG\r\n\x1a\n" + struct.pack("!I", len(ihdr)) + chunk
    data += struct.pack("!I", zlib.crc32(chunk))
    # A valid header is enough to reject the dimensions before loading pixels.
    with pytest.raises(UserError):
        normalize_image(data)


@pytest.mark.parametrize("format", ["JPEG", "WEBP"])
def test_supported_formats(format):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, format)
    assert normalize_image(output.getvalue()).startswith(b"\x89PNG")


def test_exif_orientation_and_first_frame():
    source = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    source.save(output, "JPEG", exif=exif)
    with Image.open(io.BytesIO(normalize_image(output.getvalue()))) as normalized:
        assert normalized.size == (20, 40)
    animation = io.BytesIO()
    source.save(
        animation,
        "WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (40, 20), "blue")],
        duration=100,
        lossless=True,
    )
    with Image.open(io.BytesIO(normalize_image(animation.getvalue()))) as first:
        assert first.n_frames == 1
        assert first.getpixel((0, 0))[:3] == (255, 0, 0)


def test_empty_mixed_and_missing_images(store, image_bytes):
    key = store.create("My films — café", ["Great", "Okay"])
    assert len(render_board(store.get(key), store.images)) == 1
    store.add_item(key, "Arrival", "Great", image=normalize_image(image_bytes))
    store.add_item(key, "A very long label " * 4, "Great")
    board = store.get(key)
    pages = render_board(board, store.images)
    with Image.open(io.BytesIO(pages[0].data)) as image:
        assert image.format == "JPEG"
        assert image.width == 1184
    (store.images / board.items[0].image).unlink()
    assert render_board(board, store.images)  # Visible placeholder, not a broken board.


def test_300_item_board_keeps_all_items_and_respects_attachment_limits(store):
    key = store.create("Maximum size", [f"Tier {i}" for i in range(15)])
    board = store.get(key)
    tiers = tuple(
        replace(
            tier,
            items=tuple(
                Item(t * 20 + i, f"Item {t * 20 + i}", tier.id, i, None) for i in range(20)
            ),
        )
        for t, tier in enumerate(board.tiers)
    )
    board = replace(board, tiers=tiers)
    rows = board_rows(board)
    assert sum(len(items) for _, items, _ in rows) == 300
    pages = render_board(board, store.images)
    assert len(pages) == 6
    assert len({p.filename for p in pages}) == len(pages)
    assert sum(len(p.data) for p in pages) < 20 * 1024 * 1024
    assert all(len(p.data) < 8 * 1024 * 1024 for p in pages)
    assert any(continued for _, _, continued in rows)


def test_output_size_failure_is_explicit(store):
    key = store.create("Board")
    with pytest.raises(UserError, match="attachment limit"):
        render_board(store.get(key), store.images, max_file_bytes=10)
