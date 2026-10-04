from dataclasses import dataclass

MAX_TIERS = 15
MAX_ITEMS = 300
MAX_DESCRIPTION_LENGTH = 4000
MAX_DESCRIPTION_IMAGES = 4
DEFAULT_TIERS = ("S", "A", "B", "C", "D", "F")
COLORS = ("#ff7f7f", "#ffbf7f", "#ffdf7f", "#ffff7f", "#bfff7f", "#7fff7f")


class UserError(Exception):
    """An expected problem which can safely be explained to the user."""


@dataclass(frozen=True)
class Item:
    id: int
    name: str
    tier_id: int
    position: int
    image: str | None
    description: str = ""
    description_images: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tier:
    id: int
    name: str
    color: str
    position: int
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Board:
    id: int
    guild_id: int
    title: str
    revision: int
    synced_revision: int
    channel_id: int | None
    message_id: int | None
    sync_error: str | None
    tiers: tuple[Tier, ...]

    @property
    def items(self) -> tuple[Item, ...]:
        return tuple(item for tier in self.tiers for item in tier.items)

    @property
    def url(self) -> str | None:
        if self.channel_id and self.message_id:
            return (
                f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"
            )
        return None
