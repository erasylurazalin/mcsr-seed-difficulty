"""Turn the raw match database into a table you can model on.

Filtering lives here, not in collect.py, for two reasons. Downloading is slow
and filtering is instant, so a filter applied at collection time costs hours to
undo. And the elo range you want to look at is a question you will change your
mind about ten times, which is fine as a keyword argument and painful as a
re-download.

The one thing this module is strict about is what `result_time` means.
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "matches.db"

# One row per match. result_time is joined here raw and interpreted below,
# because what it means depends on `forfeited`.
_QUERY = """
SELECT
    m.id, m.date, m.season, m.forfeited, m.result_time,
    m.overworld, m.nether, m.end_towers, m.variations,
    AVG(p.elo_rate) AS elo_mean,
    MIN(p.elo_rate) AS elo_min,
    MAX(p.elo_rate) AS elo_max
FROM matches m
JOIN match_players p ON p.match_id = m.id
WHERE m.match_type = 2          -- ranked only, the rest carry no elo
  AND m.decayed = 0             -- decay matches are not real play
  AND m.overworld IS NOT NULL
GROUP BY m.id
HAVING COUNT(p.uuid) = 2        -- drop anything that is not a clean 1v1
   AND elo_mean IS NOT NULL
"""


def load(min_elo=None, max_elo=None, seasons=None, include_censored=True,
         db_path=DB_PATH):
    """Load matches as a DataFrame.

    Columns of interest:

        minutes   completion time, ONLY for matches somebody actually finished
        censored  True if the match ended in a forfeit, so nobody finished
        cutoff    for censored matches, when the match ended, in minutes.
                  The true completion time is unknown but longer than this.

    min_elo/max_elo filter on the match average elo. Both players are matched
    by elo so they are always close together, which means this filters whole
    matches cleanly rather than half of one.
    """
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(_QUERY, conn)
    finally:
        conn.close()

    # This is the part that matters.
    #
    # `result_time` is populated whether or not anyone finished. On a forfeit
    # it is the moment the match ENDED, not a completion. The detail endpoint
    # confirms this: forfeited matches have an empty `completions` array.
    #
    # Reading a forfeit time as a completion invents impossibly fast runs, and
    # it does it most often at low elo where forfeits are most common. That is
    # the single easiest way to wreck this project.
    df["censored"] = df.forfeited.astype(bool)
    df["minutes"] = (df.result_time / 60000).where(~df.censored)
    df["cutoff"] = (df.result_time / 60000).where(df.censored)

    df["elo_gap"] = df.elo_max - df.elo_min
    # SQL NULL arrives as NaN, which is a float and truthy, so check the type
    def parse_list(s):
        return json.loads(s) if isinstance(s, str) else []

    df["end_towers"] = df.end_towers.map(parse_list)
    df["variations"] = df.variations.map(parse_list)
    df["date"] = pd.to_datetime(df.date, unit="s")

    if min_elo is not None:
        df = df[df.elo_mean >= min_elo]
    if max_elo is not None:
        df = df[df.elo_mean <= max_elo]
    if seasons is not None:
        df = df[df.season.isin(seasons)]
    if not include_censored:
        df = df[~df.censored]

    return df.reset_index(drop=True)


def variation_features(df, min_count=50):
    """Multi-hot encode the seed `variations` tags.

    Each match carries a variable length list like

        ["bastion:triple:1", "biome:fortress:crimson_forest",
         "end_spawn:buried:47"]

    Tags rarer than min_count are dropped, since a coefficient fitted on nine
    examples is a story about nine examples.
    """
    counts = pd.Series(
        [tag for tags in df.variations for tag in tags]
    ).value_counts()
    keep = counts[counts >= min_count].index

    return pd.DataFrame(
        {tag: df.variations.map(lambda tags, t=tag: int(t in tags)) for tag in keep},
        index=df.index,
    )


if __name__ == "__main__":
    df = load()
    print(f"{len(df):,} ranked matches")
    print(f"  finished: {(~df.censored).sum():,}")
    print(f"  censored (forfeit, true time unknown): {df.censored.sum():,}")
    print(f"  elo range: {df.elo_mean.min():.0f} to {df.elo_mean.max():.0f}")
    print(f"  seed variation tags kept: {variation_features(df).shape[1]}")
