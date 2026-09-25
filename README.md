# MCSR Ranked seed difficulty

Every MCSR Ranked match drops two players into the **same seed**, so when one seed
produces slower runs than another, it's the seed doing it, not the players. That's a
controlled experiment running a few thousand times a day, and the public API hands
out the results.

The question: how much time does a seed actually cost, and which seed features
matter?

## The answer

Not much. Player skill (elo) explains almost all of the run time. The seed is real,
but it moves the average by 5% at most.

```
                          mean abs error  r2 (log scale)
always predict the average       4.89 min          -0.000
elo only                         2.97 min           0.646
elo + seed                       2.93 min           0.656
```

Knowing elo takes the error from 4.89 to 2.97 minutes. Adding every seed feature
buys about 2 more seconds. Elo itself is worth about -7% run time per 100 points.

Seed type effect, holding elo fixed:

```
RUINED_PORTAL     -4.4%      HOUSING    -1.7%
BURIED_TREASURE   -0.1%      BRIDGE     -1.5%
VILLAGE           +0.8%      TREASURE   +1.3%
DESERT_TEMPLE     +1.5%      STABLES    +2.0%
SHIPWRECK         +1.7%
```

These numbers come from an earlier pull of 523k ranked matches. The dataset is
being rebuilt as season 10 onward, and they'll be rerun when that finishes.

## Traps in the data

- **A forfeit isn't a finish.** `result_time` is filled in either way, and more than
  half of ranked matches end in a forfeit. Reading those as completions invents fast
  runs that never happened.
- **Seed type is assigned by elo.** Low-rated players never get buried treasure and
  rarely get ruined portal, so comparing seed types without holding elo fixed
  measures skill. Everything here is compared within 100-point elo bins.
- **Same seed, two matches.** The train/test split is grouped by seed, otherwise a
  seed lands in train and its twin in test and the score measures memorisation.

The full analysis, including what not to filter and why the individual model
coefficients can't be read, is in [docs/analysis.md](docs/analysis.md).

## Running it

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python collect.py backfill --season 10   # a whole past season, hours
.venv/bin/python collect.py update                 # anything new since last run
.venv/bin/python model.py
```

Data comes from the [MCSR Ranked API](https://docs.mcsrranked.com/) into
`data/matches.db` (SQLite). The rate limit is 500 requests per 10 minutes. Without a
`season` parameter the API stops paging partway back and returns an empty page that
looks like the end of history, so past seasons have to be asked for by number.

`dataset.py` builds the modelling table and does the elo-binned comparisons.
`model.py` fits Ridge and gradient boosting on it.

## Not done yet

- Forfeits are dropped, which makes bad seeds look easier than they are. Survival
  analysis is the fix.
- End spawn depth should be one numeric feature, not 29 binary columns.

The same features, served as a PyTorch model over HTTP:
[mcsr-seed-serving](https://github.com/erasylurazalin/mcsr-seed-serving).
