import pytest

from tierbot.models import UserError
from tierbot.store import Store


def names(store, key, tier):
    return [i.name for i in store.tier(key, tier).items]


def test_crud_and_persistence(tmp_path):
    store = Store(tmp_path, 123)
    key = store.create("Films", ["Favorites", "Okay"])
    store.add_item(key, "Arrival", "Favorites")
    item_id = store.item(key, "Arrival").id
    store.rename(key, "Movies")
    store.edit_item(key, f"#{item_id}", name="ARRIVAL")
    store.bind(key, 100, 200)
    revision = store.get(key).revision
    store.close()
    reopened = Store(tmp_path, 123)
    assert reopened.resolve("movies") == key
    assert reopened.item(key, "arrival").id == item_id
    assert reopened.get(key).revision == revision
    assert reopened.get(key).url == "https://discord.com/channels/123/100/200"
    reopened.delete_item(key, "Arrival")
    assert not reopened.get(key).items
    reopened.close()


def test_unicode_uniqueness_and_transaction_rollback(store):
    key = store.create("Café", ["S", "A"])
    with pytest.raises(UserError):
        store.create("CAFE\u0301")
    store.add_item(key, "Straße", "S")
    before = store.get(key)
    with pytest.raises(UserError):
        store.add_item(key, "STRASSE", "A", image=b"staged-image")
    assert store.get(key) == before
    assert not list(store.images.iterdir())
    with pytest.raises(UserError):
        store.edit_tier(key, "A", name="s")
    assert store.get(key) == before


def test_item_ordering_within_and_between_tiers(store):
    key = store.create("Ranking", ["S", "A"])
    for name in ("one", "two", "three"):
        store.add_item(key, name, "S")
    store.move_item(key, "three", "S", 1)
    assert names(store, key, "S") == ["three", "one", "two"]
    store.move_item(key, "one", "A")
    store.move_item(key, "two", "A", 1)
    assert names(store, key, "S") == ["three"]
    assert names(store, key, "A") == ["two", "one"]
    store.move_item(key, "two", "A")
    assert names(store, key, "A") == ["one", "two"]
    store.delete_item(key, "one")
    assert store.item(key, "two").position == 0
    before = store.get(key)
    with pytest.raises(UserError):
        store.move_item(key, "two", "S", 10)
    assert store.get(key) == before


def test_tier_order_color_and_populated_deletion(store):
    key = store.create("Ranking", ["S", "A"])
    store.add_tier(key, "B", 1)
    store.edit_tier(key, "B", name="Best", color="#123AbC", position=2)
    assert [t.name for t in store.get(key).tiers] == ["S", "Best", "A"]
    assert store.tier(key, "Best").color == "#123abc"
    store.add_item(key, "first", "S")
    store.add_item(key, "second", "Best")
    with pytest.raises(UserError, match="destination"):
        store.delete_tier(key, "Best")
    with pytest.raises(UserError, match="different"):
        store.delete_tier(key, "Best", "Best")
    store.delete_tier(key, "Best", "S")
    assert names(store, key, "S") == ["first", "second"]
    store.delete_tier(key, "A")
    with pytest.raises(UserError, match="at least one"):
        store.delete_tier(key, "S")


def test_cascade_and_image_lifecycle(store, image_bytes):
    key = store.create("Pictures")
    other = store.create("Keep me")
    store.add_item(key, "image", "S", image=image_bytes)
    old = store.item(key, "image").image
    store.edit_item(key, "image", image=image_bytes)
    new = store.item(key, "image").image
    assert old != new
    assert not (store.images / old).exists()
    assert (store.images / new).exists()
    store.edit_item(key, "image", remove_image=True)
    assert not (store.images / new).exists()
    store.edit_item(key, "image", image=image_bytes)
    store.add_item(other, "preserve", "S", image=image_bytes)
    preserved = store.item(other, "preserve").image
    store.delete(key)
    assert store.db.execute("SELECT count(*) FROM tiers WHERE list_id=?", (key,)).fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM items WHERE list_id=?", (key,)).fetchone()[0] == 0
    assert [p.name for p in store.images.iterdir()] == [preserved]


def test_image_edit_rollback_and_orphan_collection(store):
    key = store.create("Pictures")
    store.add_item(key, "first", "S", image=b"first")
    store.add_item(key, "second", "S", image=b"second")
    before = store.get(key)
    with pytest.raises(UserError):
        store.edit_item(key, "first", name="second", image=b"third")
    assert store.get(key) == before
    assert len(list(store.images.iterdir())) == 2
    orphan = store.save_image(b"orphan")
    store.collect_images()
    assert not (store.images / orphan).exists()
    assert len(list(store.images.iterdir())) == 2


@pytest.mark.parametrize(
    "title,tiers",
    [
        ("", None),
        ("x" * 101, None),
        ("newline\nname", None),
        ("Valid", []),
        ("Valid", ["a", "A"]),
        ("Valid", ["a", ""]),
        ("Valid", [str(i) for i in range(16)]),
    ],
)
def test_invalid_create(store, title, tiers):
    with pytest.raises(UserError):
        store.create(title, tiers)
    assert not store.all()


def test_limits(store):
    key = store.create("Large", [str(i) for i in range(15)])
    with pytest.raises(UserError):
        store.add_tier(key, "extra")
    for index in range(300):
        store.add_item(key, str(index), "0")
    with pytest.raises(UserError, match="300"):
        store.add_item(key, "overflow", "0")
    assert len(store.get(key).items) == 300


def test_scoped_identifiers(store, tmp_path):
    one = store.create("One")
    two = store.create("Two")
    store.add_item(one, "First", "S")
    item = store.get(one).items[0]
    tier = store.get(one).tiers[0]
    with pytest.raises(UserError):
        store.move_item(two, f"#{item.id}", "A")
    with pytest.raises(UserError):
        store.add_item(two, "wrong tier", f"#{tier.id}")
    foreign = Store(tmp_path, guild_id=456)
    assert foreign.all() == []
    with pytest.raises(UserError):
        foreign.get(one)
    foreign.close()


def test_revision_only_acknowledged_if_current(store):
    key = store.create("Versioned")
    revision = store.get(key).revision
    store.rename(key, "New title")
    store.synced(key, revision)
    assert store.get(key).synced_revision == 0
    store.synced(key, store.get(key).revision)
    assert store.get(key).synced_revision == store.get(key).revision


def test_autocomplete_ids_take_priority_over_id_shaped_names(store):
    key = store.create("Names and IDs")
    store.add_item(key, "#2", "S")
    store.add_item(key, "Actual second item", "S")
    assert store.item(key, "#2").name == "Actual second item"
    store.edit_tier(key, "S", name="#2")
    assert store.tier(key, "#2").name == "A"
