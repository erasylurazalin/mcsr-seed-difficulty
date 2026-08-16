# MCSR Ranked seed difficulty

Every MCSR Ranked match drops two players into the **same seed**. Same buried
treasure, same bastion, same fortress biome, same end towers. So when one seed
produces slower runs than another, it is the seed doing it, not the players.

That is a controlled experiment running a few thousand times a day, and the API
hands you the results. This project tries to answer:

- How much time does a given seed actually cost?
- Which seed features matter, and which ones are folklore?

Later on, the same data plus per-match event timelines should support a live win
probability model, the speedrunning version of a chess eval bar.

## Where the data comes from

The public [MCSR Ranked API](https://docs.mcsrranked.com/). The match list
endpoint returns 100 matches per request and already includes the seed, both
players with their elo at the time, and the result, so the collector never has
to fetch matches one by one.

History goes back to season 5 (June 2024). Match ids currently run from about
1,000,000 to 12,450,000, so there are roughly 11 million matches available.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Collecting

```sh
.venv/bin/python collect.py backfill --pages 100   # 10k matches, ~2.5 min
.venv/bin/python collect.py update                 # anything new since last run
```

Both stop cleanly on Ctrl-C and pick up where they left off. Progress is the
contents of the database, not a state file, so there is nothing to get out of
sync.

Rate limit is 500 requests per 10 minutes. The collector waits 1.5s between
requests, which is a bit under that. Roughly 240k matches per hour.

## What lands in the database

`data/matches.db`, SQLite, two tables.

**matches**, one row per match. Seed fields are the interesting ones:

| field | example |
|---|---|
| `overworld` | `BURIED_TREASURE`, `SHIPWRECK`, `VILLAGE`, `DESERT_TEMPLE`, `RUINED_PORTAL`, `JUNGLE_TEMPLE` |
| `nether` | `HOUSING`, `BRIDGE`, `STABLES`, `TREASURE` |
| `end_towers` | `[88, 82, 103, 94]` |
| `variations` | `["bastion:triple:1", "biome:fortress:crimson_forest", "end_spawn:buried:47", ...]` |
| `result_time` | winning time in milliseconds |
| `forfeited` | whether someone quit instead of finishing |

**match_players**, one row per player per match, with `elo_rate` as it was
*before* the match and `elo_change` as what the match did to it.

The full API response is kept in `matches.raw`. Parsing can be redone, a
re-download of 11 million matches cannot.

## Match types

`match_type` is not documented, so I checked it against the data:

| type | what it looks like | usable? |
|---|---|---|
| 2 | always has elo changes | yes, this is ranked 1v1 |
| 1 | no elo changes, mostly placement matches and bot opponents | no |
| 3 | no elo changes, sometimes only one player, so private or solo runs | no |
| 4 | rare, no elo changes, not identified yet | no |

The collector asks the server for `type=2` directly, which it supports. That is
2.25x fewer requests per usable match than downloading everything.

## What `result_time` means, which is not what it looks like

`result_time` is filled in whether or not anybody finished the run.

- `forfeited = 0`: somebody completed. `result_time` is their finish time.
- `forfeited = 1`: nobody completed. `result_time` is the moment the match
  ended, because the loser quit. The winner did not finish.

I checked this against the detail endpoint. Forfeited matches come back with an
empty `completions` array, so there is genuinely no completion time in them.

Reading a forfeit time as a completion invents runs that never happened, and it
invents fast ones, because quitting early is exactly when people quit. Match
12448652 is a 1117 elo pair with `result_time` of 5:45. That is not a 1117 elo
player finding a god seed, it is an opponent giving up after five minutes.

`dataset.py` handles this: `minutes` is only populated for real completions, and
forfeits get a `cutoff` instead, marked `censored`.

55% of ranked matches end in a forfeit, so this is most of the data, not an
edge case.

## What not to filter

Filtering on anything the seed could have *caused* biases the answer. Filtering
on things that were already true before the seed was handed out does not.

| filter | safe? | why |
|---|---|---|
| match type | yes | decided before the match, and unranked has no elo at all |
| elo | yes, but see below | both players' elo is fixed before the seed is drawn |
| run time | **no** | this is the thing being predicted |
| "looks like an outlier" | **no** | same problem, dressed up |
| forfeited | **no, not really** | forfeiting is a reaction to the seed |

Dropping forfeits is the one everybody does anyway, including this project for
now, and it is genuinely wrong: bad seeds get quit on, so dropping them throws
away bad seeds specifically and makes every seed look easier than it is.
Survival analysis is the honest fix. First pass drops them and measures how bad
it is.

## Seed type is assigned by elo, so it is confounded with skill

This is the thing to know before doing anything else with this data.

Within a match, both players get the same seed, so a match is a clean comparison
between two runners. Across matches it is not clean at all: the game hands out
different seed types depending on rank. From the
[wiki](https://wiki.mcsrranked.com/), and my own 25k matches reproduce it:

| elo | Village | Shipwreck | Desert Temple | Ruined Portal | Buried Treasure |
|---|---|---|---|---|---|
| 0-599 | 55% | 15% | 30% | 0% | 0% |
| 600-1199 | 30% | 25% | 25% | 20% | 0% |
| 1200+ | 20% | 20% | 20% | 20% | 20% |

Buried treasure does not exist below 1200. Ruined portal barely exists below
600. So seed type partly *encodes* skill, and skill drives run time. Comparing
seed types without holding elo fixed measures both at once.

How much does this matter? A lot. Comparing runs only against other runs in the
same 100 point elo bin:

| overworld | naive, 600-1199 | elo-controlled, 600-1199 | elo-controlled, 1200+ |
|---|---|---|---|
| RUINED_PORTAL | -5.3% | **-3.4%** | **-3.6%** |
| VILLAGE | +2.1% | +0.1% | +1.3% |
| DESERT_TEMPLE | +1.7% | +1.0% | +0.6% |
| SHIPWRECK | +0.5% | +2.0% | +1.6% |
| BURIED_TREASURE | not available | not available | +0.2% |

An earlier version of this analysis pooled everything under 1200 and reported
ruined portal at -9.2% below 1200 versus -3.4% above, and called it an
interaction: good seeds help weak players more. That was wrong. Below 1200,
ruined portal was mostly acting as a marker for being above 600 elo, where it
occurs 18.5% of the time versus 0.6% below. Control for elo properly and the
gap disappears: -3.4% against -3.6%.

The real result is duller and true. **The seed effect is roughly constant across
skill.** Ruined portal saves about 3.5% wherever you look. Shipwreck costs about
2%. Nothing here interacts with rank.

Rules that follow:

- Bin on elo before comparing seed types, or restrict to 1200+ where the
  distribution is uniform and assignment really is random.
- Never compare a seed type across brackets where its availability differs.
  Buried treasure has no low elo comparison and never will.

## Should the low elo end be dropped?

No, but for a plainer reason than I first thought. Low elo runs are noisier and
forfeit far more, but once elo is controlled they agree with the high end rather
than contradicting it, which is a useful thing to be able to show.

| elo | matches | forfeit rate | mean time | coefficient of variation |
|---|---|---|---|---|
| under 900 | 10,509 | 74.7% | 25.2 min | 0.34 |
| 900-1199 | 6,374 | 49.3% | 17.2 min | 0.22 |
| 1200-1499 | 4,719 | 40.3% | 14.2 min | 0.20 |
| 1500-1799 | 2,150 | 30.2% | 12.0 min | 0.18 |
| 1800-2099 | 895 | 20.4% | 10.6 min | 0.19 |
| 2100+ | 379 | 19.3% | 9.4 min | 0.14 |

`dataset.load(min_elo=...)` exists for slicing at analysis time. The collector
does not filter on elo, and should not.

## Status

Collector works. `dataset.py` builds the modelling table. No model yet.

## Open questions

- What is match type 4?
- `variations` is a variable length list of tags, 72 of them appear at least 50
  times. Multi-hot for now. Some carry a number (`end_spawn:buried:47`) that is
  probably worth pulling out as its own feature.
- Each match yields one time, the winner's, which is the minimum of two runs.
  So the outcome depends on both players, not one. Not sure yet whether to model
  it as a minimum or just control for both elos and move on.
- Seed types are badly unbalanced. JUNGLE_TEMPLE showed up 3 times in 700.
