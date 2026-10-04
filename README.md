# Discord tier-list bot

A self-hosted bot for collaborative image tier lists in a personal Discord server.
Everyone in the configured server can create, edit, reorder, and delete lists and
items. Each list can have one public board in a chosen text channel. Changes edit
that message, including replacing its images; they do not post another board.

- Custom list titles and 1–15 named, ordered, colored tiers.
- Up to 300 items per list, with names and optional uploaded images.
- Manual ordering within tiers; move items between tiers without recreating them.
- Slash commands work in any channel. Confirmations and previews are private.
- SQLite and local image storage persist across container restarts and upgrades.

## Set up Discord

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Open **Bot**, create/reset its token, and save it in your local `.env` file.
   Do not share the token or commit it. Privileged gateway intents, including
   Message Content, are not needed. Keep the bot private if only you will invite it.
3. In Discord, enable **User Settings → Advanced → Developer Mode**, then right-click
   your server and copy its ID.
4. In the application's installation/OAuth2 settings, generate a server-install
   link with the `bot` and `applications.commands` scopes. Request **View Channels**,
   **Send Messages**, **Embed Links**, **Attach Files**, and **Read Message History**.
   Open the link and invite the bot to your server.
5. Ensure those permissions are allowed in each board's destination channel.
   Administrator and Manage Messages are not required: the bot only removes its
   own board messages. Users need access to Discord's application commands.

The command groups register automatically in `DISCORD_GUILD_ID` on startup. They
are not globally registered. Commands and autocomplete reject other servers.
Changing the configured server does not transfer lists between servers.

## Run with Docker Compose

Install Docker with Compose on your Linux home server. From this repository:

```sh
cp .env.example .env
# Edit .env with your token and server ID, then:
docker compose up -d --build
docker compose logs -f bot
```

The log should show registered commands and a successful connection. Your home
server needs outbound HTTPS/WebSocket access to Discord; no exposed ports, public
domain, or inbound port forwarding are needed. The container runs as UID/GID
10001, uses a named persistent volume, and restarts automatically.

Do not run more than one bot instance for the same token/data directory. A local
file lock prevents two processes from sharing the same data directory.

## Use the bot

These examples show slash-command options; choose values from Discord's UI.

```text
/tierlist create title:Movie night tiers:Favorites | Good | Okay | Skip
/item add list:Movie night name:Arrival tier:Favorites image:<upload>
/item add list:Movie night name:Interstellar tier:Good
/tierlist hook list:Movie night channel:#movie-tierlist
/item move list:Movie night item:Interstellar tier:Favorites position:1
/tier color list:Movie night tier:Favorites color:#ff7f7f
/tierlist show list:Movie night
```

The `tiers` value is a single string, separated by `|`, in top-to-bottom order.
Omit it to use **S, A, B, C, D, F**. Names may contain spaces and accented letters.
List titles must be unique in the server; tier and item names must be unique
within their list (ignoring case and Unicode normalization).

| Command | Options / behavior |
| --- | --- |
| `/tierlist create` | `title`, optional `tiers` |
| `/tierlist list` | Private directory, optional `page` (10 lists per page) |
| `/tierlist show` | `list`; private preview of all board pages |
| `/tierlist rename` | `list`, `title` |
| `/tierlist delete` | `list`; private confirmation expires after 60 seconds |
| `/tierlist hook` | `list`, `channel`; publish, refresh, move, or explicitly recreate a missing board |
| `/tierlist unhook` | `list`; remove the public message and retain all data |
| `/tierlist refresh` | `list`; retry an update without creating a message |
| `/tier add` | `list`, `name`, optional `position` |
| `/tier rename` | `list`, `tier`, `name` |
| `/tier move` | `list`, `tier`, `position` |
| `/tier color` | `list`, `tier`, `color` (`#RRGGBB`) |
| `/tier delete` | `list`, `tier`, optional `destination` for its items |
| `/item add` | `list`, `name`, `tier`, optional `image`, optional `position` |
| `/item show` | `list`, `item`; full name, tier, position, and original normalized image |
| `/item edit` | `list`, `item`, optional `name`, `image`, or `remove-image:true` |
| `/item move` | `list`, `item`, `tier`, optional `position` |
| `/item delete` | `list`, `item`; delete the item and its stored image |

Use autocomplete to select lists, tiers, and items; type part of an item's name
to search beyond the first 25 suggestions. Autocomplete uses stable internal IDs,
so renaming an entity does not break its references. You can also type exact names
or `#ID` values. Positions start at 1. Omitting an optional position appends the
item or tier. Moving an item within its current tier removes it first, then inserts
it at the requested position.

Deleting a nonempty tier requires a different destination; its items append there
in their existing order. At least one tier must remain. Deleting a list removes
all tiers, items, uploaded image copies, and its board. If the list changes after
the deletion prompt, confirmation is rejected so you can review it again.

Slash commands do not leave ordinary request messages. Replies are ephemeral
(visible only to the person issuing the command), so text-message deletion and
Message Content access are unnecessary. Prefix commands such as `!tierlist` are
not supported.

## Boards and images

Hooking initially creates one message. Subsequent changes replace that message's
attachments and embeds, preserving its message ID and link. Several lists can
share a channel, but each list has at most one hooked channel. Commands may be
used from any channel in the server, including a different channel from the board.

Large boards use up to seven image attachments inside the same message, with
eight tiles per row and eight rows per page. Tier labels repeat across page breaks.
Empty tiers remain visible. Long tile labels may be shortened visually; use
`/item show` to read the complete name. Titles allow 100 characters, tier names 40,
and item names 80. The bundled Noto Sans font covers Latin, Greek, and Cyrillic;
other scripts and color emoji may display missing glyphs in rendered boards.

Images must be PNG, JPEG, or WebP, at most 10 MiB and 25 megapixels. The bot keeps a
256-pixel normalized PNG copy, applies EXIF orientation, strips metadata, and uses
the first frame of animated uploads. Transparent images are supported. Raw
originals are not retained. Uploads are stored locally, so expiring Discord
attachment URLs do not break your lists. External image URLs are not accepted.

## Recovery and troubleshooting

- **Update failed:** list changes are saved first. Transient failures are retried
  every 30 seconds and after restart. `/tierlist list` shows synchronization state.
- **Missing permissions:** restore the channel permissions above, then use
  `/tierlist refresh`. The bot preserves list data and does not repost the board.
- **Deleted board or channel:** ordinary edits never create a replacement.
  Use `/tierlist hook` to explicitly publish again in your chosen channel.
- **Move a board:** run `hook` with another channel. The bot checks the destination
  and renders first, then removes the old message and publishes in the new channel.
  If cleanup is denied, the original binding and all data remain. If publishing
  fails after cleanup, the list remains saved but unhooked.
- **Publishing times out:** Discord might have accepted the new message even if
  the response was lost. Check the destination and remove any orphaned board
  before retrying `hook`. Initial publication is not automatically retried. A
  process crash between publication and saving the returned message ID has the
  same limitation; ordinary edit retries are safe and do not create messages.
- **Image file missing:** rendering uses a visible placeholder; re-upload that
  item's image. Preserve the complete data volume when restoring a backup.
- **Slash commands absent:** check the server ID, installation scopes, and logs.
  Restart after correcting `.env` with `docker compose up -d --force-recreate`.

## Back up, restore, and upgrade

The named volume contains `tierlists.sqlite3`, any SQLite WAL files, and `images/`.
Back up the entire volume with the bot stopped. From the repository directory:

```sh
docker compose stop bot
docker compose run --rm --no-deps -T bot tar -C /data -czf - . > tierlists-backup.tar.gz
docker compose start bot
```

Before restoring, stop the bot, verify your backup archive, and back up its
current data. A restore replaces the database and images with the backup contents.
Remove the old database and its transaction logs together before extraction:

```sh
docker compose stop bot
tar -tzf tierlists-backup.tar.gz
docker compose run --rm --no-deps -T bot rm -f /data/tierlists.sqlite3 /data/tierlists.sqlite3-wal /data/tierlists.sqlite3-shm
docker compose run --rm --no-deps -T bot tar -C /data -xzf - < tierlists-backup.tar.gz
docker compose start bot
```

Startup removes unreferenced normalized images, including files left over after
a restore. Keep backup archives private: they contain your list data and images.
Never use `docker compose down -v` unless you intend to delete all persistent data.

For an upgrade, back up first, update the checkout, and run:

```sh
docker compose up -d --build
docker compose logs --tail=100 bot
```

## Local development and checks

Python 3.12+ on Linux/macOS and [uv](https://docs.astral.sh/uv/) are supported.
Docker uses Python 3.14. `uv.lock` pins development and runtime dependencies;
`requirements.txt` is its hash-verified production export.

```sh
uv sync --frozen
uv run --env-file .env python -m tierbot
uv run python -m pytest -q
uv run ruff check .
uv run ruff format --check .
```

Local runs use `./data` unless `DATA_DIR` is set. `.env` is loaded by Compose or
`uv run --env-file`; plain `python -m tierbot` reads the process environment only.

After changing dependencies, update the lock and production export together:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

Automated tests use temporary databases and fake Discord gateways. They cover
CRUD, ordering, image validation, 300-item pagination, attachment replacement,
concurrency, restarts, and failed/missing board recovery without a bot token.

For a live acceptance check, use a test server: create a list with custom tiers,
add items with and without images, hook it, edit from another channel, and verify
that **Copy Message Link** stays identical. Restart the container and repeat an
edit. Test permission loss and recovery, manually delete the board and confirm
an ordinary edit does not repost, then explicitly re-hook and delete the list.

The bundled font is licensed under the [SIL Open Font License](tierbot/assets/OFL.txt).
