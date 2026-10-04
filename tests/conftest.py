import io

import pytest
from PIL import Image

from tierbot.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path, guild_id=123)
    yield instance
    instance.close()


@pytest.fixture
def image_bytes():
    output = io.BytesIO()
    Image.new("RGBA", (320, 240), (35, 140, 200, 180)).save(output, "PNG")
    return output.getvalue()
