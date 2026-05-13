"""
RescueNet — Offline Sync Service
SQLite (local) → PostgreSQL (cloud) delta sync.
Runs as background task. Fires when connectivity restored.
"""

import asyncio
import json
import logging
import socket
import sqlite3
import time
from contextlib import asynccontextmanager

import asyncpg

log = logging.getLogger("rescuenet.sync")

SQLITE_PATH  = "data/rescuenet_local.db"
POSTGRES_DSN = "postgresql://rescuenet:rescuenet@postgres:5432/rescuenet"
SYNC_INTERVAL_ONLINE  = 30    # seconds between sync attempts when online
SYNC_INTERVAL_OFFLINE = 120   # seconds between connectivity checks when offline
BATCH_SIZE = 50


def is_online() -> bool:
    try:
        socket.setdefaulttimeout(2)
        socket.gethostbyname("8.8.8.8")
        return True
    except OSError:
        return False


def get_unsynced(conn: sqlite3.Connection, limit: int = BATCH_SIZE) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM triage_log WHERE synced = 0 ORDER BY timestamp ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_synced(conn: sqlite3.Connection, log_ids: list[str]):
    conn.executemany(
        "UPDATE triage_log SET synced = 1 WHERE log_id = ?",
        [(lid,) for lid in log_ids],
    )
    conn.commit()


async def ensure_pg_schema(pg: asyncpg.Connection):
    await pg.execute("""
        CREATE TABLE IF NOT EXISTS triage_log (
            log_id              TEXT PRIMARY KEY,
            patient_id          TEXT,
            final_level         TEXT,
            deterministic_level TEXT,
            discrepancy         BOOLEAN,
            discrepancy_detail  TEXT,
            triggered_alerts    JSONB,
            final_actions       JSONB,
            protocol_ref        TEXT,
            latency_ms          FLOAT,
            synced              INTEGER DEFAULT 1,
            timestamp           TIMESTAMPTZ,
            synced_at           TIMESTAMPTZ DEFAULT NOW()
        )
    """)


async def push_batch(pg: asyncpg.Connection, records: list[dict]) -> list[str]:
    """
    Upsert batch to PostgreSQL. Returns list of successfully synced log_ids.
    """
    synced_ids = []
    async with pg.transaction():
        for r in records:
            try:
                await pg.execute("""
                    INSERT INTO triage_log (
                        log_id, patient_id, final_level, deterministic_level,
                        discrepancy, discrepancy_detail, triggered_alerts,
                        final_actions, protocol_ref, latency_ms, synced, timestamp
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (log_id) DO NOTHING
                """,
                    r["log_id"], r["patient_id"], r["final_level"],
                    r["deterministic_level"], bool(r["discrepancy"]),
                    r["discrepancy_detail"],
                    json.loads(r["triggered_alerts"]),
                    json.loads(r["final_actions"]),
                    r["protocol_ref"], r["latency_ms"], 1,
                    r["timestamp"],
                )
                synced_ids.append(r["log_id"])
            except Exception as e:
                log.warning(f"Failed to sync record {r['log_id']}: {e}")
    return synced_ids


async def sync_loop():
    """
    Main sync loop.
    Offline → wait → check connectivity → sync when back online.
    """
    log.info("Offline sync service started")
    pg_conn = None

    while True:
        if not is_online():
            log.debug("C-BLACKOUT — offline mode, sync paused")
            if pg_conn:
                await pg_conn.close()
                pg_conn = None
            await asyncio.sleep(SYNC_INTERVAL_OFFLINE)
            continue

        # Online — attempt sync
        try:
            if pg_conn is None or pg_conn.is_closed():
                pg_conn = await asyncpg.connect(POSTGRES_DSN, timeout=5)
                await ensure_pg_schema(pg_conn)

            sqlite_conn = sqlite3.connect(SQLITE_PATH)
            unsynced = get_unsynced(sqlite_conn)

            if unsynced:
                log.info(f"Syncing {len(unsynced)} records to PostgreSQL")
                synced_ids = await push_batch(pg_conn, unsynced)
                mark_synced(sqlite_conn, synced_ids)
                log.info(f"Synced {len(synced_ids)}/{len(unsynced)} records")
            else:
                log.debug("No unsynced records")

            sqlite_conn.close()

        except asyncpg.PostgresConnectionFailureError as e:
            log.warning(f"Postgres unreachable: {e}")
            pg_conn = None
        except Exception as e:
            log.error(f"Sync error: {e}")
        finally:
            await asyncio.sleep(SYNC_INTERVAL_ONLINE)


# ── FastAPI lifespan integration ─────────────────────────────────────────────
@asynccontextmanager
async def sync_lifespan(app):
    """Mount into FastAPI lifespan to run sync as background task."""
    task = asyncio.create_task(sync_loop())
    log.info("Offline sync task started")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.info("Offline sync task stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_loop())
