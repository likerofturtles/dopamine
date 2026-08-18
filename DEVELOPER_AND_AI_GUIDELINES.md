# Dopamine Bot - Developer & AI Agent Guidelines (`DEVELOPER_AND_AI_GUIDELINES.md`)

Welcome to the **Dopamine** Discord bot codebase. This document serves as the authoritative, production-ready reference for developers, AI coding agents/assistants, aliens, and everything and everyone else in between who's contributing to this project. All code modifications, architectural additions, and database interactions must strictly adhere to the guidelines set forth herein.

---

## 1. Core Architectural Mandates

1. **Database Engine:** [`Turso`](https://turso.tech) (SQLite-compatible) persistence layer via [`pyturso`](requirements.txt:1). Local file database stored at [`databases/dopamine.db`](config.py:29) with optional remote replication sync urls (`TURSO_DATABASE_URL`).
2. **Connection Management:** A single, shared database connection instance (`DatabaseManager` in [`utils/database.py`](utils/database.py:74)) managed thread-safely using `asyncio.Lock()` and `asyncio.Event()`.
3. **Centralized Data Access:** All SQL queries and database operations must be executed exclusively through [`DatabaseManager`](utils/database.py:74) methods (`execute`, `execute_write`).
4. **Cogs Integration Rule:** Cogs must **directly** call methods on `DatabaseManager` (`self.bot.db.execute(...)` or `self.bot.db.execute_write(...)`). 
   - ❌ **NO custom wrapper methods** in cogs or helpers.
   - ❌ **NO local connection acquisition helpers** (e.g. `acquire_db`).
   - ❌ **NO duplicate query logic**.
4. **User & Server Data Manager:** Ensure that new types of user and server data is exposed through the data manager (`cogs/data.py`).
---

## 2. Project Structure

The project is structured logically into modular components:

- [`main.py`](main.py): Entry point, bot initialization (`BeaconAutoShardedBot`), global database instance binding (`bot.db`), and asynchronous startup sequence.
- [`config.py`](config.py): Environment variable loader (`python-dotenv`), path resolutions, and configuration constants (`TURSO_LOCAL_PATH`, `COMMAND_PREFIX`, etc.).
- [`VERSION.py`](VERSION.py): Bot version tracking file.
- [`cogs/`](cogs/): Discord feature cogs (e.g., [`cogs/notes.py`](cogs/notes.py:103), [`cogs/daily.py`](cogs/daily.py), [`cogs/afk.py`](cogs/afk), [`cogs/moderation.py`](cogs/moderation.py), etc.). Each cog handles specific bot features, UI views, modals, and data privacy protocols.
- [`utils/`](utils/): Core utilities and services:
  - [`utils/database.py`](utils/database.py:74): `DatabaseManager`, schema definitions (`_ensure_schema_sync`), and async synchronization wrappers.
  - [`utils/data_protocol.py`](utils/data_protocol.py): Data export, deletion, and privacy protocols.
  - [`utils/log.py`](utils/log.py): Logging setup and log channel management.
  - [`utils/time.py`](utils/time.py): Time manipulation and formatting helpers.
- [`scripts/`](scripts/): Administrative scripts, migration tools (e.g., [`scripts/migrate_to_turso.py`](scripts/migrate_to_turso.py)).
- [`requirements.txt`](requirements.txt): Python dependencies specification.

---

## 3. Database Architecture & [`DatabaseManager`](utils/database.py:74)

### Initialization & Connection
In [`main.py`](main.py:54), `DatabaseManager` is instantiated and attached to the bot instance:
```python
bot.db = DatabaseManager(
    db_path=TURSO_LOCAL_PATH,
    sync_url=TURSO_DATABASE_URL,
    auth_token=TURSO_AUTH_TOKEN
)
```

### Readiness & Synchronization (`asyncio.Event`)
Background loops and cogs must wait for the database and its schema to be fully initialized before executing queries. [`DatabaseManager.wait_ready()`](utils/database.py:634) uses an `asyncio.Event()` (`self._ready`) to block until [`ensure_schema()`](utils/database.py:629) completes:
```python
async def wait_ready(self):
    await self._ready.wait()
```

### Thread Safety (`asyncio.Lock` & `asyncio.to_thread`)
Because `pyturso` operates synchronously under the hood, all blocking SQLite calls are wrapped with `asyncio.to_thread` inside `asyncio.Lock()` blocks in [`utils/database.py`](utils/database.py:74):
- [`DatabaseManager.execute()`](utils/database.py:642): For `SELECT` queries returning list of dictionary rows (via `_dict_factory`).
- [`DatabaseManager.execute_write()`](utils/database.py:647): For `INSERT`, `UPDATE`, `DELETE` queries committing transactions and returning `rowcount`.

### Schema Migrations & Table Management
All tables are defined inside `_ensure_schema_sync()` in [`utils/database.py`](utils/database.py:99). When adding new features or tables, append `CREATE TABLE IF NOT EXISTS` statements to `_ensure_schema_sync()`. To change existing tables, ensure migration logic.

---

## 4. Coding Standards & Best Practices

1. **Type Hinting:** Use strict type annotations (`from __future__ import annotations`, `dict[int, dict[str, str]]`, `Optional`, `List`, `Any`).
2. **Asynchronous Design:** 
   - Never block the event loop. Wrap blocking I/O or heavy computations with `asyncio.to_thread`.
   - Always await `.wait_ready()` or rely on `DatabaseManager` methods which handle it automatically.
3. **Error Handling:**
   - Wrap database transactions and Discord API interactions in `try...except` blocks.
   - Log errors gracefully using standard logging or Discord error handlers without crashing bot shards.
4. **Caching Strategy:**
   - Cogs should maintain in-memory caches (e.g. `self.notes_cache`) populated during `cog_load()` via [`populate_caches()`](cogs/notes.py:118) and updated atomically on database writes.

---

## 5. Dependency Management (`requirements.txt`)

All project dependencies are pinned in [`requirements.txt`](requirements.txt).

To install or update dependencies:
```bash
pip install -r requirements.txt
```

---

## 6. Common Pitfalls to Avoid

1. **Creating Local SQLite Connections:** Never import `sqlite3` or open `sqlite3.connect()` directly inside cogs or utilities. Always use `self.bot.db.execute(...)`.
2. **Ignoring Event Loop Blocking:** Running synchronous SQLite queries directly in async cog event handlers causes event loop stuttering. Always delegate through `DatabaseManager`.
3. **Race Conditions on Startup:** Background tasks starting before [`ensure_schema()`](utils/database.py:629) finishes will throw operational errors. Always ensure tasks await `bot.db.wait_ready()`.
4. **Hardcoding Credentials & Paths:** Always load configuration through [`config.py`](config.py) and environment variables (`.env`).
