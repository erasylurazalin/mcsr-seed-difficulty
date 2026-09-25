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

import numpy as np
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "matches.db"

# Each season has 4 phases of about a month. Elo drifts upward across a season:
# a 1500 in phase 1 can be the same player as a 2200 in phase 4. So elo only
# means one thing within a phase, and comparisons have to hold the phase fixed.
#
# The dates are when phases 1, 2 and 3 end, taken from Liquipedia's phase
# pages. Phase 4 runs to the end of the season. Read as midnight UTC, which is
# what the API reports for season 12. Season 12 only has its first boundary so
# far, from the API's phase-leaderboard endpoint.
PHASE_ENDS = {
    9: ["2025-09-21", "2025-10-25", "2025-11-29"],
    10: ["2026-02-02", "2026-03-02", "2026-04-02"],
    11: ["2026-05-30", "2026-06-30", "2026-07-30"],
    12: ["2026-09-30"],
}

# One row per match. result_time is joined here raw and interpreted below,
# because what it means depends on `forfeited`.
_QUERY = """
SELECT
    m.id, m.date, m.season, m.forfeited, m.result_time,
    m.seed_id, m.overworld, m.nether, m.end_towers, m.variations,
    AVG(p.elo_rate) AS elo_mean,
    MIN(p.elo_rate) AS elo_min,
    MAX(p.elo_rate) AS elo_max
FROM matches m
JOIN match_players p ON p.match_id = m.id
WHERE m.match_type = 2          -- ranked only. 1 is casual and placements,
                                -- 3 is private and solo, 4 is event matches.
                                -- None of the others carry elo.
  AND m.decayed = 0             -- decay matches are not real play
  AND m.overworld IS NOT NULL
GROUP BY m.id
HAVING COUNT(p.uuid) = 2        -- drop anything that is not a clean 1v1
   AND elo_mean IS NOT NULL
"""


def add_phase(df):
    """Phase 1 to 4 within the season, from the match date.

    NaN for a season missing from PHASE_ENDS. Season 12 past its last known
    boundary is NaN too, not a guess.
    """
    phase = pd.Series(np.nan, index=df.index)
    for season, ends in PHASE_ENDS.items():
        rows = df.season == season
        ends = pd.to_datetime(ends)
        phase[rows] = 1 + np.searchsorted(ends, df.date[rows], side="right")
        if len(ends) < 3:
            phase[rows & (df.date >= ends[-1])] = np.nan
    return phase


def load(min_elo=None, max_elo=None, seasons=None, phases=None,
         include_censored=True, db_path=DB_PATH):
    """Load matches as a DataFrame.

    Columns of interest:

        minutes   completion time, ONLY for matches somebody actually finished
        censored  True if the match ended in a forfeit, so nobody finished
        cutoff    for censored matches, when the match ended, in minutes.
                  The true completion time is unknown but longer than this.
        phase     1 to 4 within the season, see PHASE_ENDS

    seasons and phases are lists, e.g. seasons=[10, 11, 12], phases=[1] for
    only phase 1 of each. Elo is only comparable within a phase.

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
    df["phase"] = add_phase(df)

    if min_elo is not None:
        df = df[df.elo_mean >= min_elo]
    if max_elo is not None:
        df = df[df.elo_mean <= max_elo]
    if seasons is not None:
        df = df[df.season.isin(seasons)]
    if phases is not None:
        df = df[df.phase.isin(phases)]
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


def elo_controlled_effect(df, by="overworld", bin_width=100, min_per_bin=15,
                          min_total=100):
    """How much time a seed feature costs, holding player skill fixed.

    Do not compare seed types by taking a plain group mean. The game hands out
    seed types by rank: buried treasure only exists at 1200+, ruined portal
    only above roughly 600, villages are 55% of seeds at the bottom and 20% at
    the top. So a seed type partly encodes skill, and skill drives run time.
    A plain group mean measures both and reports it as one number.

    This compares each run only against other runs in the same `bin_width`
    slice of elo, so the skill difference cancels out.

    Returns a DataFrame indexed by the levels of `by`:

        effect_pct  percent slower (+) or faster (-) than a typical run at the
                    same elo
        ci95        half width of a 95% interval. If it is wider than
                    effect_pct, you have not measured anything.
        n           runs backing the estimate
        elo_from    the elo range this level actually occurs in. Levels with
        elo_to      different ranges are NOT comparable to each other, they are
                    each only comparable to their own elo neighbours.

    Censored rows are dropped, since a forfeit has no completion time. That is
    itself a bias, bad seeds get quit on, so read these as effects among runs
    that finished.
    """
    d = df[df.minutes.notna()].copy()
    if len(d) < min_total:
        raise ValueError(f"only {len(d)} finished runs, need at least {min_total}")

    d["elo_bin"] = (d.elo_mean // bin_width) * bin_width
    # each run as a ratio to the average run at its own skill level
    d["rel"] = d.minutes / d.groupby("elo_bin")["minutes"].transform("mean")

    rows = []
    for level, g in d.groupby(by):
        # only trust elo bins where this level has real support, otherwise a
        # single run in a sparse bin swings the whole estimate
        counts = g.groupby("elo_bin").size()
        usable = counts[counts >= min_per_bin].index
        g = g[g.elo_bin.isin(usable)]
        if len(g) < min_total:
            continue

        rows.append({
            by: level,
            "effect_pct": 100 * (g.rel.mean() - 1),
            "ci95": 100 * 1.96 * g.rel.std() / np.sqrt(len(g)),
            "n": len(g),
            "elo_from": int(usable.min()),
            "elo_to": int(usable.max() + bin_width - 1),
        })

    if not rows:
        raise ValueError(f"no level of {by!r} had enough support to estimate")

    return (pd.DataFrame(rows)
              .set_index(by)
              .sort_values("effect_pct"))


def print_effect(df, by="overworld", **kwargs):
    """elo_controlled_effect, formatted for reading in a terminal."""
    t = elo_controlled_effect(df, by=by, **kwargs)
    print(f"\n{by.upper()}, holding elo fixed")
    print(f"{'':<20}{'effect':>9}{'95% ci':>12}{'n':>7}   available at")
    # itertuples, not iterrows: iterrows casts the whole row to one dtype and
    # would turn the integer elo bounds into 600.0
    for r in t.itertuples():
        flat = "" if abs(r.effect_pct) > r.ci95 else "   (not distinguishable from zero)"
        print(f"{str(r.Index):<20}{r.effect_pct:>+8.1f}%{'  +/- ' + format(r.ci95, '.1f'):>12}"
              f"{r.n:>7,}   {r.elo_from}-{r.elo_to}{flat}")


if __name__ == "__main__":
    df = load()
    print(f"{len(df):,} ranked matches")
    print(f"  finished: {(~df.censored).sum():,}")
    print(f"  censored (forfeit, true time unknown): {df.censored.sum():,}")
    print(f"  elo range: {df.elo_mean.min():.0f} to {df.elo_mean.max():.0f}")
    print(f"  seed variation tags kept: {variation_features(df).shape[1]}")

    print_effect(df, "overworld")
    print_effect(df, "nether")
