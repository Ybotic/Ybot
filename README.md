# YBOT

YBOT is a private, single-owner Discord assistant. It provides secure command
routing, SQLite persistence, idempotent Discord server setup, persistent
reminders, an interactive to-do manager, private notes, saved links, and local
search, daily digests, weekly reviews, and optional conversational AI. Notion
is not required; AI is disabled by default and the core bot needs no API key.

## Requirements

- Python 3.12 or newer
- A Discord application and bot
- A dedicated Discord server (guild)
- Bot permissions: **View Channels**, **Send Messages**, **Read Message
  History**, **Embed Links**, **Add Reactions**, **Manage Channels**, and **Manage Roles**. Administrator is not
  required or requested. Manage Roles is needed to apply the private-channel
  permission overwrites.

Discord categories cannot be nested. “YBOT SERVER” is the server heading in
the requested layout; bootstrap creates the five categories at the server
root: `INBOX`, `PLANNER`, `CLASSES`, `SAVED`, and `SYSTEM`.

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Set the values in `.env`:

| Variable | Description |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from the Discord Developer Portal. Keep it secret. |
| `OWNER_ID` | Discord user ID allowed to use YBOT commands. |
| `GUILD_ID` | The only Discord server YBOT will operate in. |
| `TIMEZONE` | Optional IANA timezone for reminder input/display (defaults to `America/New_York`). |

IDs must be positive Discord snowflakes. Enable Developer Mode in Discord to
copy user and server IDs.

## Discord Developer Portal

1. Create an application and add a bot in the [Discord Developer
   Portal](https://discord.com/developers/applications).
2. Copy the bot token into `DISCORD_TOKEN` in `.env`; never commit or share it.
3. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
4. Invite the bot to the configured server with the permissions listed above.
   Do not grant Administrator. YBOT uses the configured server ID as an
   additional runtime boundary.

The bot needs to be able to see the server and send messages for the command
system. Bootstrap checks for Manage Channels and Manage Roles before making
any changes; if either is missing it logs the exact missing permission and
does not begin setup.

## Run

```bash
source .venv/bin/activate
python bot.py
```

The SQLite database is created automatically at `data/ybot.sqlite3`. It is
ignored by Git. On startup (and when joining the configured server), YBOT
creates or reuses the required categories and channels, stores their IDs, and
verifies the resulting structure. Repeated setup does not create duplicates
or delete existing channels or messages.

The reminder engine stores due timestamps in UTC and interprets/displays
reminder times in `TIMEZONE`. It polls the database every 15 seconds, checks
for overdue pending reminders immediately on startup, and retries failed
Discord deliveries. Reminders remain pending until delivery succeeds, so they
are recovered after a restart. The schema update is additive and preserves
existing Phase 1 data.

Available commands, usable only by `OWNER_ID` in `GUILD_ID`:

- `!ybot ping`
- `!ybot help`
- `!ybot status`
- `!ybot remind me Friday at 5pm submit lab`
- `!ybot remind me in 30 minutes check laundry`
- `!ybot reminders` / `!ybot reminders today`
- `!ybot complete reminder 12`
- `!ybot cancel reminder 12`
- `!ybot delete reminder 12`
- `!ybot snooze reminder 12 30m` (also `10m`, `1h`, or `tomorrow`)
- `!ybot todo` — show the task panel link and counts.
- `!ybot todo add buy groceries` — quick-add a normal-priority task.
- `!ybot todo buy groceries` — equivalent quick-add form.
- `!ybot todo add study | class: CMSC 216 | due: Friday at 5 PM | priority: high`
- `!ybot todo list`
- `!ybot todo done 12`
- `!ybot todo remove 12` — archive the task; it is preserved.
- `!ybot todo edit 12` — open the task editor using the button in the reply.
- `!ybot todo completed`
- `!ybot todo clear` — show the archive confirmation; repeat with `confirm` to proceed.
- `!ybot note Remember to research quantum error correction`
- `!ybot note title: Quantum Computing | content: Research IBM quantum hardware`
- `!ybot notes` / `!ybot notes recent`
- `!ybot note edit 12`
- `!ybot note delete 12` — archive the note.
- `!ybot link https://example.com/article`
- `!ybot links` / `!ybot links recent`
- `!ybot link edit 12`
- `!ybot link delete 12` — archive the link.
- `!ybot find quantum error correction`
- `!ybot find quantum | type: notes | class: PHYSICS`
- `!ybot channel new CMSC216` — create `#cmsc216` under `CLASSES`.
- `!ybot channel archive CMSC216`
- `!ybot channel restore CMSC216`
- `!ybot class CMSC216` — show its overview.
- `!ybot classes` — list active classes (`archived` and `all` filters are available).

Guild-scoped slash commands are also available: `/remind` (time,
description, optional class), `/reminders`, and `/reminder-cancel` (select a
pending reminder). Due notifications are sent to `#reminders` with Complete,
Cancel, and Snooze controls (10 minutes, 30 minutes, 1 hour, or tomorrow).
Controls are restored after restart and are usable only by `OWNER_ID` in
`GUILD_ID`.

To-do slash commands are `/todo`, `/todo-add`, `/todo-complete`,
`/todo-remove`, `/todo-edit`, and `/todo-completed`. Task edits accept title,
description, class, due date/time, and priority; `clear` removes an optional
field. Slash replies are ephemeral and task-ID options provide autocomplete.

Class commands are `/class-create`, `/class`, `/classes`, `/class-archive`,
and `/class-restore`. Class creation is idempotent: normalized class names are
unique per guild, and repeated create requests never make duplicate channels.
If a registered channel was deleted manually, YBOT offers a private recreate
action. Archiving retains the channel and its messages, marks it archived in
SQLite, renames it with an `archived-` prefix when possible, and disables
automatic class inference. Restoring reactivates or recreates the channel.

Class-to-channel mappings are persisted in SQLite. Reminders, tasks, notes, and
links created in an active registered class channel inherit that class unless
the owner supplies an explicit class value. Unregistered channels under
`CLASSES` do not infer a class; archived class channels are excluded from
automatic tagging. `/class` shows task, note, link, and reminder counts, the
next upcoming reminder, recent notes/links, and a channel navigation control.

Phase 4 adds guild-scoped `/note` (opens a modal), `/notes`, `/note-edit`,
`/note-delete`, `/link`, `/links`, `/link-delete`, and `/find`. `/find` accepts
`query`, `type` (`all`, `notes`, `links`, or `todos`), and an optional class
filter. Search results are private, embedded, paginated, and include a link or
the appropriate resource-management command.

## Notes and saved links

Notes are private and are never posted to `#links`. Prefix notes and search are
restricted to `#ybot`; class-channel note/link saves are supported when the
channel is under `CLASSES`, with confirmations routed privately to `#ybot`.
Prefix note/link saves react to the source message with ✅. Slash note creation
opens a modal and responds ephemerally. Untitled notes receive a short title
from their first non-empty content line; no AI is used. Deleting notes/links
archives them rather than removing their records.

Saved links are posted as metadata-only embeds in `#links`; the embed includes
title, URL, description, saved date, and class where available. Full extracted
page text is not posted there. Link metadata uses the page title, meta/Open
Graph description, canonical URL, and bounded plain text. The meta description
is the link summary; no AI key or paid API is needed. Failed metadata fetches
still leave the URL saved with `extraction_status=failed`.

Link fetching uses asynchronous HTTP with a 10-second total timeout, a 1.5 MB
response limit, HTML-only content types, and at most five manually validated
redirects. Only HTTP/HTTPS on standard ports is accepted. Localhost, internal
hostnames, private/reserved/loopback IPs, and DNS answers containing any
non-public address are rejected. A custom resolver validates the addresses at
connection time with DNS caching and proxy-environment use disabled, so a
redirect or DNS rebinding cannot make the bot connect to a private destination.
JavaScript is never run and non-HTML files are not downloaded.

Unified search indexes notes, links, and open/completed tasks, including titles,
content, URLs, classes, and tags. SQLite FTS5 is enabled automatically when
available; if the SQLite build lacks FTS5, parameterized case-insensitive
`LIKE` queries are used instead. Search stays entirely local in SQLite.

## To-do panel

YBOT maintains a single interactive embed in the private `#todo` channel.
Bootstrap creates the panel after the channel is ready. It updates that same
message after task changes and stores the message/channel IDs and selected
filter/page in SQLite. On startup the bot restores its persistent controls,
fetches and refreshes the existing message, and creates a replacement only if
Discord reports the stored panel message was deleted.

The panel shows up to 25 tasks per page, sorted by high/normal/low priority,
then due date, then creation time. It includes task IDs, classes, due times,
overdue status, outstanding counts, and completed counts. Filters show open,
completed, archived, or all tasks. Controls add tasks with a modal, complete,
archive/remove, edit, refresh, filter, and paginate. Task changes are committed
to SQLite before the panel is edited. If Discord cannot update the panel, the
task remains saved and the Refresh Panel control can retry.

Tasks have `open`, `completed`, and `archived` states. Remove and clear archive
tasks rather than deleting them; clear requires a second confirmation command.
Completed tasks remain available from the panel filter and `!ybot todo
completed`. Due times are stored in UTC and interpreted/displayed in `TIMEZONE`.
Tasks added in a channel under `CLASSES` inherit a class label from that
channel; no class-channel creation or archival is performed.

Prefix task management is restricted to private `#todo`. A quick-add from a
channel under `CLASSES` is supported for future class channels; its confirmation
is sent privately to `#todo`. Slash command replies are ephemeral.

Prefix examples accept relative times, today/tomorrow, weekdays (including
`next Monday`), and named month/day dates. AM/PM or an unambiguous 24-hour
time is required. Daylight-saving gaps and repeated local times are rejected
for clarification rather than scheduled silently. For example:

```text
!ybot remind me Friday at 5pm submit lab
!ybot remind me in 30 minutes check laundry
!ybot remind me tomorrow at 21:30 review notes
```

The private `#ybot`, `#todo`, `#bot-log`, `#digest`, and `#reminders` channels are
restricted to the configured owner, the Discord server owner, and YBOT. Other
expected channels retain the server's existing permissions
or use the server's normal permissions when created. Bootstrap does not change
permissions on unrelated channels. Prefix reminder commands must be issued in
the private `#ybot` channel; slash command responses are ephemeral. Link embeds
are the only Phase 4 content posted to `#links`.

## Daily digest and weekly review

YBOT posts a **daily digest at 8:00 AM** and a **weekly review on Sunday at
7:00 PM** to private `#digest`, using `TIMEZONE` (default
`America/New_York`). Summaries use SQLite data only; they need no AI key.
They include reminders, overdue work, priorities, completed tasks, upcoming
deadlines, recent notes/links, and class labels. Weekly reviews also show
class-wise progress and unresolved reminders due during the week. “Missed”
means due and still pending/notified, not cancelled or completed. Completion
timestamps begin being recorded for reminders with this schema upgrade;
historical completion dates are not invented.

`!ybot digest` and `!ybot review` generate on-demand overviews in private
`#ybot`. Scheduled job IDs are stable, and delivery slots/leases/message IDs are
persisted in SQLite. A recovery job retries failures every minute and catches
up the most recent daily and weekly slot after downtime rather than flooding
the server with old reports. It checks recent bot posts for delivery markers
to reconcile a restart between sending and recording delivery. Run one YBOT
instance against this database. Report embeds show bounded previews and totals;
the normal list/search commands provide full resource access.

## Optional conversational AI

When enabled, conversational prompts, recent context, and data retrieved for
the response are sent to your configured OpenAI API account.

Fixed commands always take precedence. Other `!ybot ...` messages from the
owner **in private #ybot only** go to the assistant. Unprefixed conversation,
DMs, bot messages, and other guilds are ignored. Existing fixed commands retain
their channel rules.

AI is disabled by default. To enable it, set these in `.env`:

```env
AI_ENABLED=true
AI_PROVIDER=openai
OPENAI_API_KEY=your_own_key
AI_MODEL=your_chosen_tool_capable_model
```

Then restart YBOT (with Docker, `sudo docker compose up -d --force-recreate`).
The model must support Chat Completions function tools. OpenAI requests use
your own API account and its usage limits/billing; all non-AI features and
deployment work without that account or key.

Examples: `!ybot What do I need to finish today?`, `!ybot Move my lab report
to high priority.`, `!ybot What have I saved about CMSC216?`.

The provider is isolated in `services/ai_provider.py`. The assistant calls an
allowlisted set of application functions for reminders, tasks, notes, links,
search, class progress and overviews. It cannot run SQL, Python, shell commands
or arbitrary code. Ambiguous task matches require clarification. Destructive
requests create Confirm/Cancel controls valid for 15 minutes, restricted to
the owner and #ybot. These confirmations are restored after restart and cannot
be reused. Notes/links/tasks are archived rather than permanently deleted.

SQLite retains about 20 conversation turns (40 messages), only from private
#ybot assistant conversations, across restarts. When AI is enabled, the chosen
context and requested tool results are sent to the configured OpenAI model.
No tokens or keys are included in this context. Optional owner preferences may
be loaded from `AI_SYSTEM_PROMPT_FILE` (a UTF-8 file accessible inside the
container); it is not needed for normal operation.

### Additional environment settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `AI_ENABLED` | `false` | Enable conversational AI. |
| `AI_PROVIDER` | `openai` | Provider adapter. |
| `OPENAI_API_KEY` | empty | Required only when AI is enabled. |
| `AI_MODEL` | empty | Tool-capable OpenAI model when enabled. |
| `AI_SYSTEM_PROMPT_FILE` | empty | Optional local preference file. |
| `DATABASE_PATH` | project `data/ybot.sqlite3` | SQLite file location. |
| `BACKUP_DIR` | project `backups/` | Online SQLite snapshots. |
| `BACKUP_RETENTION_DAYS` | `14` | Local automatic backup retention. |
| `LOG_LEVEL` | `INFO` | Python log level; HTTP/provider debug content stays suppressed. |

Docker Compose sets the database and backup paths to its persistent mounts.
Backups run at startup and daily at 3:00 AM local time using SQLite's online
backup API, then rotate snapshots older than the retention window.

## Oracle Cloud Ubuntu ARM64 deployment

These are manual steps for when your VM is ready; development does not create
cloud resources. Use an Ubuntu 24.04 ARM64 image on an Oracle Ampere VM. The
official Python Docker base supports ARM64 natively; no emulation, database
server, website, or inbound application port is required. Allow SSH to the VM
and outbound DNS/HTTPS (HTTP is used only for public link fetching). Keep time
synchronization enabled.

### 1. Install Docker and Compose on the VM

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
```

### 2. Copy the project and configure it

Upload the **complete updated project**, or first publish these files to your
repository and clone that revision on the VM:

```bash
git clone https://github.com/Ybotic/Ybot.git
cd Ybot
cp .env.example .env
nano .env
chmod 600 .env
mkdir -p data backups
sudo chown -R 10001:10001 data backups
sudo chmod 700 data backups
```

Set `DISCORD_TOKEN`, `OWNER_ID`, `GUILD_ID`, and your desired `TIMEZONE`. Leave
AI disabled and its key/model empty unless you want the optional integration.
Enable Message Content Intent in Discord and invite the bot with `bot` and
`applications.commands` scopes. Give it its documented bootstrap permissions
plus **Embed Links**, **Add Reactions**, and **Read Message History**. Bootstrap
also makes `#digest` private. Administrator is not needed.

To preserve an existing installation, stop that bot and copy its SQLite file
to `data/ybot.sqlite3` before the first container start, then assign ownership
to UID/GID `10001:10001`. Keep this data directory when updating the code.

### 3. Build, start, and view logs

```bash
sudo docker compose up -d --build
sudo docker compose logs -f --tail=100 ybot
```

Compose runs a non-root container with persistent `./data` and `./backups`
mounts, rotated logs, and automatic restart after crashes/VM reboots. There are
no published ports. SIGTERM triggers graceful scheduler/provider/SQLite cleanup.

### 4. Restart or update

```bash
sudo docker compose restart ybot
# After updating .env, recreate the container to load the new values:
sudo docker compose up -d --force-recreate
# For a code update from your published repository:
git pull --ff-only
sudo docker compose up -d --build
```

### 5. Backup and restore

Automatic snapshots are stored on the VM under `backups/`. Create an extra
consistent snapshot while the bot runs (choose a new output filename):

```bash
sudo docker compose exec ybot python -m utils.backup --output /app/backups/manual-YYYYMMDD.sqlite3
```

Copy important snapshots off the VM yourself; local snapshots do not survive
loss of the VM disk. Restore a chosen snapshot with the bot stopped:

```bash
sudo docker compose stop ybot
sudo mv data/ybot.sqlite3 data/ybot.pre-restore.sqlite3
sudo cp backups/CHOSEN-SNAPSHOT.sqlite3 data/ybot.sqlite3
sudo rm -f data/ybot.sqlite3-wal data/ybot.sqlite3-shm data/ybot.sqlite3-journal
sudo chown 10001:10001 data/ybot.sqlite3
sudo docker compose up -d
```

Do not remove `data/` or `backups/` during deployment or updates. Existing
tables are preserved and the additive schema changes are applied on startup.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use temporary SQLite databases and fake guild/channel objects; no real
Discord credentials or server are required.

## Manual Discord reminder test

1. Start YBOT and confirm `#reminders` exists under `PLANNER`.
2. As the configured owner, run `!ybot remind me in 1 minute reminder test`.
3. Confirm the reminder appears in `!ybot reminders` and is delivered to
   `#reminders` when due.
4. Try Complete, Cancel, and each Snooze option; confirm only the configured
   owner can use the controls.
5. Create another reminder, restart YBOT before it is due, and verify it still
   appears and is delivered. Repeat while it is overdue to verify startup
   recovery.

## Manual Discord to-do test

1. Start YBOT and confirm the private panel appears in `#todo`.
2. Use **Add Task** to create a task with a due time, class, and high priority;
   confirm the panel updates in place and the owner receives a private
   confirmation.
3. Try Complete Task, Remove Task, Edit Task, each filter, pagination, and
   Refresh Panel. Confirm removed tasks appear under Archived rather than
   disappearing from the database.
4. Create tasks with `!ybot todo buy groceries`, `/todo-add`, and the prefix
   field syntax; verify they appear on the same panel.
5. Restart YBOT and confirm the same panel message and its controls remain
   active. Delete the panel message manually and confirm startup creates one
   replacement.

## Manual Discord notes, links, and search test

1. Save a prefix note in `#ybot`, then use `/note` and submit its modal. Confirm
   both are acknowledged privately and appear in `!ybot notes` or `/notes`.
2. Save a public webpage with `!ybot link https://example.com/` or `/link`.
   Confirm its metadata appears in `#links`; verify no note content appears
   there. Try an invalid scheme and a localhost/private URL and confirm they
   are rejected.
3. Run `!ybot find example`, `/find`, and class/type-filtered searches. Use the
   page controls and confirm results stay private.
4. Try note/link edit and archive commands; confirm archived records no longer
   appear in the normal lists but remain stored.

## Manual Discord class test

1. In private `#ybot`, run `!ybot channel new CMSC216` and confirm `#cmsc216`
   appears under `CLASSES` with a class-identifying topic.
2. Repeat the create command and confirm it reports the existing class instead
   of creating a duplicate channel.
3. In `#cmsc216`, create a reminder, task, note, and link. Confirm the class
   label is inferred; supply an explicit class value once and confirm it
   overrides the channel inference.
4. Run `!ybot class CMSC216` and check its counts, next reminder, recent
   notes/links, and channel link.
5. Archive and restore the class. Confirm the archived channel stops automatic
   class tagging and restoring re-enables it without deleting channel messages.
