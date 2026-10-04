"""Bounded image ingestion and deterministic, paginated tier-board rendering."""

import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from tierbot.models import Board, Item, Tier, UserError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
FONT = Path(__file__).parent / "assets" / "NotoSans-Regular.ttf"
WIDTH = 1184
TIER_WIDTH = 160
CELL_WIDTH = 128
ROW_HEIGHT = 196
HEADER_HEIGHT = 112
ROWS_PER_PAGE = 8
COLUMNS = 8


@dataclass(frozen=True)
class Page:
    filename: str
    data: bytes


def normalize_image(data: bytes) -> bytes:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UserError("Images must be no larger than 10 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise UserError("Images must be no larger than 25 megapixels.")
                source.seek(0)
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((256, 256), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "PNG")
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise UserError("Upload a valid PNG, JPEG, or WebP image, up to 25 megapixels.") from exc


def wrapped(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int
) -> list[str]:
    # Character wrapping also handles long words and scripts without spaces.
    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        end = 1
        while end < len(remaining) and draw.textlength(remaining[: end + 1], font=font) <= width:
            end += 1
        if end < len(remaining):
            boundary = remaining.rfind(" ", 0, end + 1)
            if boundary > end // 2:
                end = boundary
        line, remaining = remaining[:end].rstrip(), remaining[end:].lstrip()
        if remaining and len(lines) == max_lines - 1:
            while line and draw.textlength(line + "…", font=font) > width:
                line = line[:-1]
            line += "…"
        lines.append(line)
    return lines


def text_block(draw, text, box, font, color, max_lines, spacing, centered=False):
    x, y, width, height = box
    lines = wrapped(draw, text, font, width, max_lines)
    if centered:
        y += max(0, (height - spacing * len(lines)) // 2)
    for line in lines:
        line_x = x + (width - draw.textlength(line, font=font)) // 2 if centered else x
        draw.text((line_x, y), line, font=font, fill=color)
        y += spacing


def board_rows(board: Board) -> list[tuple[Tier, tuple[Item, ...], bool]]:
    rows = []
    for tier in board.tiers:
        for start in range(0, max(1, len(tier.items)), COLUMNS):
            rows.append((tier, tier.items[start : start + COLUMNS], start > 0))
    return rows


def render_board(
    board: Board, image_dir: Path, max_file_bytes: int = 8 * 1024 * 1024
) -> list[Page]:
    title_font = ImageFont.truetype(str(FONT), 28)
    tier_font = ImageFont.truetype(str(FONT), 20)
    label_font = ImageFont.truetype(str(FONT), 14)
    small_font = ImageFont.truetype(str(FONT), 13)
    rows = board_rows(board)
    page_count = (len(rows) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE
    pages = []
    # Bound aggregate request size as well as each attachment.
    byte_limit = min(max_file_bytes, 20 * 1024 * 1024 // max(1, page_count))
    for offset in range(0, len(rows), ROWS_PER_PAGE):
        selected = rows[offset : offset + ROWS_PER_PAGE]
        image = Image.new(
            "RGB", (WIDTH, HEADER_HEIGHT + len(selected) * ROW_HEIGHT + 44), "#171923"
        )
        draw = ImageDraw.Draw(image)
        text_block(draw, board.title, (20, 12, WIDTH - 40, 80), title_font, "#f5f6fa", 2, 38)
        page_number = len(pages) + 1
        for row_number, (tier, items, continued) in enumerate(selected):
            top = HEADER_HEIGHT + row_number * ROW_HEIGHT
            draw.rectangle((0, top, TIER_WIDTH - 1, top + ROW_HEIGHT - 2), fill=tier.color)
            r, g, b = (int(tier.color[i : i + 2], 16) for i in (1, 3, 5))
            foreground = "#171923" if r * 0.299 + g * 0.587 + b * 0.114 > 145 else "#ffffff"
            text_block(
                draw,
                tier.name,
                (10, top + 6, TIER_WIDTH - 20, ROW_HEIGHT - 30),
                tier_font,
                foreground,
                5,
                26,
                centered=True,
            )
            if continued:
                draw.text(
                    (12, top + ROW_HEIGHT - 24), "continued", font=small_font, fill=foreground
                )
            if not items:
                draw.text(
                    (TIER_WIDTH + 20, top + 76), "No items yet", font=label_font, fill="#a5adbd"
                )
            for column, item in enumerate(items):
                left = TIER_WIDTH + column * CELL_WIDTH
                draw.rounded_rectangle(
                    (left + 4, top + 4, left + CELL_WIDTH - 4, top + ROW_HEIGHT - 5),
                    radius=6,
                    fill="#252938",
                )
                if item.image:
                    try:
                        with Image.open(image_dir / item.image) as source:
                            thumb = ImageOps.contain(source.convert("RGBA"), (112, 112))
                            image.paste(
                                thumb,
                                (
                                    left + (CELL_WIDTH - thumb.width) // 2,
                                    top + 8 + (112 - thumb.height) // 2,
                                ),
                                thumb,
                            )
                    except (OSError, ValueError):
                        draw.text(
                            (left + 12, top + 50), "Image missing", font=small_font, fill="#f6b26b"
                        )
                else:
                    text_block(
                        draw,
                        str(item.position + 1),
                        (left + 8, top + 8, 112, 100),
                        title_font,
                        "#a5adbd",
                        1,
                        38,
                        centered=True,
                    )
                text_block(
                    draw, item.name, (left + 8, top + 122, 112, 68), label_font, "#f5f6fa", 4, 16
                )
        footer = f"List #{board.id}  •  Revision {board.revision}  •  {len(board.items)} items"
        footer += f"  •  Page {page_number}/{page_count}"
        draw.text((16, image.height - 31), footer, font=small_font, fill="#a5adbd")
        encoded = b""
        for quality in (88, 78, 65, 50):
            output = io.BytesIO()
            image.save(output, "JPEG", quality=quality, optimize=True)
            encoded = output.getvalue()
            if len(encoded) <= byte_limit:
                break
        if len(encoded) > byte_limit:
            raise UserError("This board exceeds the channel's attachment limit. Try fewer images.")
        pages.append(Page(f"tierlist-{board.id}-r{board.revision}-{page_number}.jpg", encoded))
    return pages
