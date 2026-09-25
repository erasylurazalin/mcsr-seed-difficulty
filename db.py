"""SQLite storage for MCSR Ranked matches.

Two tables, because a match has two players and cramming both into one row
makes every later query awkward:

    matches        one row per match, plus the untouched JSON
    match_players  one row per player per match

Every row keeps the full API response in `matches.raw`. Parsing can be redone
later, a re-download of 11 million matches cannot.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "matches.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY,
    date        INTEGER NOT NULL,   -- unix seconds
    season      INTEGER,
    match_type  INTEGER,            -- see README, 2 is the normal ranked 1v1
    category    TEXT,               -- ANY, etc
    game_mode   TEXT,
    forfeited   INTEGER,            -- 0/1, someone quit instead of finishing
    decayed     INTEGER,            -- 0/1, elo decay match, not real play
    beginner    INTEGER,            -- 0/1, placement match
    bot_source  INTEGER,            -- match id this bot replays, NULL for humans

    seed_id     TEXT,
    overworld   TEXT,               -- BURIED_TREASURE, SHIPWRECK, RUINED_PORTAL...
    nether      TEXT,               -- STABLES, HOUSING, BRIDGE, TREASURE
    end_towers  TEXT,               -- JSON array of 4 tower heights
    variations  TEXT,               -- JSON array of seed feature tags

    winner_uuid TEXT,               -- NULL if nobody won
    result_time INTEGER,            -- winning time in ms, NULL if no completion

    raw         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_date      ON matches(date);
CREATE INDEX IF NOT EXISTS idx_matches_season    ON matches(season);
CREATE INDEX IF NOT EXISTS idx_matches_type      ON matches(match_type);
CREATE INDEX IF NOT EXISTS idx_matches_overworld ON matches(overworld);

CREATE TABLE IF NOT EXISTS match_players (
    match_id   INTEGER NOT NULL,
    uuid       TEXT NOT NULL,
    nickname   TEXT,
    elo_rate   INTEGER,             -- elo BEFORE the match
    elo_rank   INTEGER,
    country    TEXT,
    elo_change INTEGER,             -- what the match did to their elo
    PRIMARY KEY (match_id, uuid)
);

CREATE INDEX IF NOT EXISTS idx_players_uuid ON match_players(uuid);

-- where an unfinished `collect.py update` stopped, see update() there
CREATE TABLE IF NOT EXISTS collector_state (
    key   TEXT PRIMARY KEY,
    value INTEGER
);
"""


def connect(path=DB_PATH):
    """Open the database, creating the file and tables if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL lets you read the database from another terminal while the
    # collector is still writing to it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_matches(conn, matches):
    """Insert a page of matches. Returns how many were actually new.

    INSERT OR IGNORE means running the collector twice is harmless.
    """
    match_rows = []
    player_rows = []

    for m in matches:
        seed = m.get("seed") or {}
        result = m.get("result") or {}
        # elo change is reported separately from the player list, so index it
        changes = {c["uuid"]: c["change"] for c in (m.get("changes") or [])}

        match_rows.append((
            m["id"],
            m["date"],
            m.get("season"),
            m.get("type"),
            m.get("category"),
            m.get("gameMode"),
            int(bool(m.get("forfeited"))),
            int(bool(m.get("decayed"))),
            int(bool(m.get("beginner"))),
            m.get("botSource"),
            seed.get("id"),
            seed.get("overworld"),
            seed.get("nether"),
            json.dumps(seed.get("endTowers")) if seed.get("endTowers") else None,
            json.dumps(seed.get("variations")) if seed.get("variations") else None,
            result.get("uuid"),
            result.get("time"),
            json.dumps(m, separators=(",", ":")),
        ))

        for p in (m.get("players") or []):
            player_rows.append((
                m["id"],
                p["uuid"],
                p.get("nickname"),
                p.get("eloRate"),
                p.get("eloRank"),
                p.get("country"),
                changes.get(p["uuid"]),
            ))

    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        match_rows,
    )
    inserted = conn.total_changes - before
    conn.executemany(
        "INSERT OR IGNORE INTO match_players VALUES (?,?,?,?,?,?,?)",
        player_rows,
    )
    conn.commit()
    return inserted


def bounds(conn):
    """Lowest and highest match id stored, or (None, None) on an empty db."""
    row = conn.execute("SELECT MIN(id) lo, MAX(id) hi FROM matches").fetchone()
    return row["lo"], row["hi"]


def count(conn):
    return conn.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"]


def get_state(conn, key):
    row = conn.execute("SELECT value FROM collector_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO collector_state VALUES (?, ?)", (key, value))
    conn.commit()


def clear_state(conn):
    conn.execute("DELETE FROM collector_state")
    conn.commit()
