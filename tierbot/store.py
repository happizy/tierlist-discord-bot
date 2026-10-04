"""Synchronous, short SQLite transactions; called only on the event-loop thread."""

import logging
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from tierbot.models import COLORS, DEFAULT_TIERS, MAX_ITEMS, MAX_TIERS, Board, Item, Tier, UserError

log = logging.getLogger(__name__)


def name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def clean_name(value: str, limit: int = 80) -> str:
    value = value.strip()
    if not value or len(value) > limit or any(unicodedata.category(c) == "Cc" for c in value):
        raise UserError(
            f"Names must contain 1–{limit} characters and no line breaks/control characters."
        )
    return value


def position_index(position: int | None, length: int) -> int:
    """length is the sequence length AFTER removing an existing member, if any."""
    if position is None:
        return length
    if not 1 <= position <= length + 1:
        raise UserError(f"Position must be between 1 and {length + 1}.")
    return position - 1


class Store:
    def __init__(self, directory: Path, guild_id: int):
        directory.mkdir(parents=True, exist_ok=True)
        self.images = directory / "images"
        self.images.mkdir(exist_ok=True)
        self.guild_id = guild_id
        self.db = sqlite3.connect(directory / "tierlists.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError(f"Unsupported database version {version}; upgrade the bot.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                name_key TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                synced_revision INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER,
                message_id INTEGER,
                sync_error TEXT,
                UNIQUE(guild_id, name_key),
                CHECK ((channel_id IS NULL) = (message_id IS NULL))
            );
            CREATE TABLE IF NOT EXISTS tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                color TEXT NOT NULL,
                position INTEGER NOT NULL,
                UNIQUE(list_id, name_key),
                UNIQUE(list_id, id)
            );
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                tier_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                image TEXT,
                UNIQUE(list_id, name_key),
                FOREIGN KEY(list_id, tier_id) REFERENCES tiers(list_id, id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS items_order ON items(tier_id, position);
            CREATE INDEX IF NOT EXISTS tiers_order ON tiers(list_id, position);
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, list_id: int):
        self.get(list_id)
        try:
            with self.db:
                yield
                self.db.execute(
                    "UPDATE lists SET revision=revision+1, sync_error=NULL WHERE id=?",
                    (list_id,),
                )
        except sqlite3.IntegrityError as exc:
            raise UserError("That name is already in use in this list.") from exc

    def all(self) -> list[Board]:
        rows = self.db.execute(
            "SELECT id FROM lists WHERE guild_id=? ORDER BY name_key", (self.guild_id,)
        ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def get(self, list_id: int) -> Board:
        row = self.db.execute(
            "SELECT * FROM lists WHERE id=? AND guild_id=?", (list_id, self.guild_id)
        ).fetchone()
        if row is None:
            raise UserError("List not found. Choose a list from autocomplete.")
        tiers = []
        for tier in self.db.execute(
            "SELECT * FROM tiers WHERE list_id=? ORDER BY position, id", (list_id,)
        ).fetchall():
            items = tuple(
                Item(i["id"], i["name"], i["tier_id"], i["position"], i["image"])
                for i in self.db.execute(
                    "SELECT * FROM items WHERE tier_id=? ORDER BY position, id", (tier["id"],)
                )
            )
            tiers.append(Tier(tier["id"], tier["name"], tier["color"], tier["position"], items))
        return Board(
            row["id"],
            row["guild_id"],
            row["title"],
            row["revision"],
            row["synced_revision"],
            row["channel_id"],
            row["message_id"],
            row["sync_error"],
            tuple(tiers),
        )

    def resolve(self, value: str) -> int:
        # Autocomplete sends #ID, avoiding ambiguity with numeric list titles.
        if re.fullmatch(r"#[0-9]+", value):
            return self.get(int(value[1:])).id
        row = self.db.execute(
            "SELECT id FROM lists WHERE guild_id=? AND name_key=?",
            (self.guild_id, name_key(value.strip())),
        ).fetchone()
        if row is None:
            raise UserError("List not found. Choose a list from autocomplete.")
        return row["id"]

    def tier(self, list_id: int, value: str) -> Tier:
        by_id = re.fullmatch(r"#[0-9]+", value)
        for tier in self.get(list_id).tiers:
            if value == f"#{tier.id}" if by_id else name_key(value.strip()) == name_key(tier.name):
                return tier
        raise UserError("Tier not found in this list.")

    def item(self, list_id: int, value: str) -> Item:
        by_id = re.fullmatch(r"#[0-9]+", value)
        for item in self.get(list_id).items:
            if value == f"#{item.id}" if by_id else name_key(value.strip()) == name_key(item.name):
                return item
        raise UserError("Item not found in this list.")

    def create(self, title: str, tier_names: list[str] | None = None) -> int:
        title = clean_name(title, 100)
        names = [
            clean_name(n, 40) for n in (tier_names if tier_names is not None else DEFAULT_TIERS)
        ]
        if not 1 <= len(names) <= MAX_TIERS:
            raise UserError(f"A list must have 1–{MAX_TIERS} tiers.")
        if len({name_key(n) for n in names}) != len(names):
            raise UserError("Tier names must be unique (ignoring case).")
        try:
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO lists(guild_id, title, name_key) VALUES (?, ?, ?)",
                    (self.guild_id, title, name_key(title)),
                )
                list_id = cursor.lastrowid
                self.db.executemany(
                    "INSERT INTO tiers(list_id, name, name_key, color, position) VALUES(?,?,?,?,?)",
                    [
                        (list_id, n, name_key(n), COLORS[p % len(COLORS)], p)
                        for p, n in enumerate(names)
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise UserError("A list with that title already exists.") from exc
        return list_id

    def rename(self, list_id: int, title: str):
        title = clean_name(title, 100)
        with self.transaction(list_id):
            self.db.execute(
                "UPDATE lists SET title=?, name_key=? WHERE id=?", (title, name_key(title), list_id)
            )

    def delete(self, list_id: int):
        board = self.get(list_id)
        with self.db:
            self.db.execute("DELETE FROM lists WHERE id=?", (list_id,))
        for item in board.items:
            self.remove_image(item.image)

    def _order(self, table: str, ids: list[int]):
        assert table in ("tiers", "items")
        self.db.executemany(f"UPDATE {table} SET position=? WHERE id=?", enumerate(ids))

    def add_tier(self, list_id: int, name: str, position: int | None = None):
        name = clean_name(name, 40)
        tiers = self.get(list_id).tiers
        if len(tiers) >= MAX_TIERS:
            raise UserError(f"A list can have at most {MAX_TIERS} tiers.")
        ids = [t.id for t in tiers]
        index = position_index(position, len(ids))
        with self.transaction(list_id):
            cursor = self.db.execute(
                "INSERT INTO tiers(list_id, name, name_key, color, position) VALUES(?,?,?,?,?)",
                (list_id, name, name_key(name), COLORS[len(tiers) % len(COLORS)], index),
            )
            ids.insert(index, cursor.lastrowid)
            self._order("tiers", ids)

    def edit_tier(
        self,
        list_id: int,
        value: str,
        *,
        name: str | None = None,
        color: str | None = None,
        position: int | None = None,
    ):
        tier = self.tier(list_id, value)
        name = clean_name(name, 40) if name is not None else tier.name
        color = color or tier.color
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise UserError("Use a six-digit hex color, such as #ff7f7f.")
        with self.transaction(list_id):
            self.db.execute(
                "UPDATE tiers SET name=?, name_key=?, color=? WHERE id=?",
                (name, name_key(name), color.lower(), tier.id),
            )
            if position is not None:
                ids = [t.id for t in self.get(list_id).tiers if t.id != tier.id]
                ids.insert(position_index(position, len(ids)), tier.id)
                self._order("tiers", ids)

    def delete_tier(self, list_id: int, value: str, destination: str | None = None):
        board = self.get(list_id)
        tier = self.tier(list_id, value)
        if len(board.tiers) == 1:
            raise UserError("Keep at least one tier in the list.")
        target = self.tier(list_id, destination) if destination is not None else None
        if target and target.id == tier.id:
            raise UserError("The destination must be a different tier.")
        if tier.items and target is None:
            raise UserError("Select a destination tier for the items before deleting this tier.")
        with self.transaction(list_id):
            if target:
                self.db.execute("UPDATE items SET tier_id=? WHERE tier_id=?", (target.id, tier.id))
                self._order("items", [i.id for i in target.items + tier.items])
            self.db.execute("DELETE FROM tiers WHERE id=?", (tier.id,))
            self._order("tiers", [t.id for t in board.tiers if t.id != tier.id])

    def add_item(
        self,
        list_id: int,
        name: str,
        tier: str,
        position: int | None = None,
        image: bytes | None = None,
    ):
        name = clean_name(name)
        if len(self.get(list_id).items) >= MAX_ITEMS:
            raise UserError(f"A list can have at most {MAX_ITEMS} items.")
        target = self.tier(list_id, tier)
        ids = [i.id for i in target.items]
        index = position_index(position, len(ids))
        filename = self.save_image(image) if image is not None else None
        try:
            with self.transaction(list_id):
                cursor = self.db.execute(
                    "INSERT INTO items(list_id,tier_id,name,name_key,position,image) "
                    "VALUES(?,?,?,?,?,?)",
                    (list_id, target.id, name, name_key(name), index, filename),
                )
                ids.insert(index, cursor.lastrowid)
                self._order("items", ids)
        except Exception:
            self.remove_image(filename)
            raise

    def edit_item(
        self,
        list_id: int,
        value: str,
        *,
        name: str | None = None,
        image: bytes | None = None,
        remove_image: bool = False,
    ):
        item = self.item(list_id, value)
        if image is not None and remove_image:
            raise UserError("Choose either a replacement image or remove-image, not both.")
        if name is None and image is None and not remove_image:
            raise UserError("Provide a new name, an image, or remove-image.")
        name = clean_name(name) if name is not None else item.name
        new_image = self.save_image(image) if image is not None else None
        filename = new_image if image is not None else (None if remove_image else item.image)
        try:
            with self.transaction(list_id):
                self.db.execute(
                    "UPDATE items SET name=?, name_key=?, image=? WHERE id=?",
                    (name, name_key(name), filename, item.id),
                )
        except Exception:
            self.remove_image(new_image)
            raise
        if filename != item.image:
            self.remove_image(item.image)

    def move_item(self, list_id: int, value: str, tier: str, position: int | None = None):
        item = self.item(list_id, value)
        source = self.tier(list_id, f"#{item.tier_id}")
        target = self.tier(list_id, tier)
        ids = [i.id for i in target.items if i.id != item.id]
        ids.insert(position_index(position, len(ids)), item.id)
        with self.transaction(list_id):
            self.db.execute("UPDATE items SET tier_id=? WHERE id=?", (target.id, item.id))
            self._order("items", ids)
            if source.id != target.id:
                self._order("items", [i.id for i in source.items if i.id != item.id])

    def delete_item(self, list_id: int, value: str):
        item = self.item(list_id, value)
        tier = self.tier(list_id, f"#{item.tier_id}")
        with self.transaction(list_id):
            self.db.execute("DELETE FROM items WHERE id=?", (item.id,))
            self._order("items", [i.id for i in tier.items if i.id != item.id])
        self.remove_image(item.image)

    def bind(self, list_id: int, channel_id: int | None, message_id: int | None):
        self.get(list_id)
        with self.db:
            self.db.execute(
                "UPDATE lists SET channel_id=?, message_id=?, synced_revision=0, sync_error=NULL "
                "WHERE id=?",
                (channel_id, message_id, list_id),
            )

    def synced(self, list_id: int, revision: int):
        with self.db:
            self.db.execute(
                "UPDATE lists SET synced_revision=?, sync_error=NULL WHERE id=? AND revision=?",
                (revision, list_id, revision),
            )

    def sync_error(self, list_id: int, error: str):
        with self.db:
            self.db.execute("UPDATE lists SET sync_error=? WHERE id=?", (error, list_id))

    def save_image(self, image: bytes) -> str:
        filename = f"{uuid4().hex}.png"
        (self.images / filename).write_bytes(image)
        return filename

    def remove_image(self, filename: str | None):
        if filename:
            try:
                (self.images / filename).unlink(missing_ok=True)
            except OSError:
                # The DB is authoritative; garbage collection retries on next startup.
                log.exception("Could not remove unused image %s", filename)

    def collect_images(self):
        """Run at startup, before commands/rendering can access staged images."""
        used = {r[0] for r in self.db.execute("SELECT image FROM items WHERE image IS NOT NULL")}
        for path in self.images.glob("*.png"):
            if re.fullmatch(r"[0-9a-f]{32}\.png", path.name) and path.name not in used:
                self.remove_image(path.name)
