"""First model: how much of a run time does the seed actually explain?

Two models, because they answer different questions.

A gradient boosting model answers "how well can this be predicted at all",
which is the honest way to size the seed effect: fit elo alone, then fit elo
plus seed, and the gap between them is what the seed is worth. If that gap is
small, the seed is a small effect, and no amount of feature engineering will
change that.

A linear model on log(time) answers "what does each feature cost", because on
a log scale a coefficient reads directly as a percentage. This is the elo
controlled comparison from dataset.py done properly: instead of binning elo
and comparing within bins, elo goes in as a covariate and every seed
coefficient is already conditional on it.

Both are fitted on finished runs only. Forfeits have no completion time. That
is a real bias and it points one way, since bad seeds are the ones people quit
on, so everything here understates how much a bad seed costs. Survival
analysis is the fix and it is not in this file yet.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GroupShuffleSplit

import dataset

SEED = 0


def features(df, use_elo=True, use_seed=True):
    """Build the design matrix.

    Elo is divided by 100 so its coefficient reads as "per 100 elo" and so the
    ridge penalty does not effectively ignore it next to the 0/1 dummies.
    """
    parts = []

    if use_elo:
        parts.append(pd.DataFrame({
            "elo_mean": df.elo_mean / 100,
            "elo_gap": df.elo_gap / 100,
        }, index=df.index))

    if use_seed:
        parts.append(pd.get_dummies(df.overworld, prefix="ow"))
        parts.append(pd.get_dummies(df.nether, prefix="nether"))
        parts.append(pd.DataFrame({
            "tower_min": df.end_towers.map(lambda t: min(t) if t else np.nan),
            "tower_mean": df.end_towers.map(lambda t: np.mean(t) if t else np.nan),
        }, index=df.index))
        parts.append(dataset.variation_features(df, min_count=200))

    X = pd.concat(parts, axis=1).astype(float)
    # a handful of matches have no end towers recorded. the tree model would
    # take the NaN, the linear one would not, so fill once here and keep the
    # two models on identical inputs.
    return X.fillna(X.median())


def split(df, test_size=0.2):
    """Train/test split grouped by seed.

    The same seed shows up in about 1.5 matches on average. A plain random
    split would put some of those matches in train and their twins in test,
    and the test score would be measuring memorisation of specific seeds
    rather than anything general about seed features.
    """
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=SEED)
    train_idx, test_idx = next(splitter.split(df, groups=df.seed_id))
    return df.iloc[train_idx], df.iloc[test_idx]


def evaluate(name, pred_log, y_log_true, y_true):
    """Score a model. Errors are reported in minutes, on the original scale."""
    pred = np.exp(pred_log)
    return {
        "model": name,
        "mae_min": mean_absolute_error(y_true, pred),
        "r2_log": r2_score(y_log_true, pred_log),
    }


def run(df):
    train, test = split(df)
    y_train, y_test = np.log(train.minutes), np.log(test.minutes)

    rows = []

    # baseline: ignore everything, predict the same time for every match.
    # every number below has to beat this to have earned its existence.
    rows.append(evaluate("always predict the average",
                         np.full(len(test), y_train.mean()), y_test, test.minutes))

    trees = {}
    for name, kw in [("elo only", dict(use_seed=False)),
                     ("seed only", dict(use_elo=False)),
                     ("elo + seed", {})]:
        Xtr, Xte = features(train, **kw), features(test, **kw)
        Xte = Xte.reindex(columns=Xtr.columns, fill_value=0)
        m = HistGradientBoostingRegressor(random_state=SEED, max_iter=300)
        m.fit(Xtr, y_train)
        trees[name] = m
        rows.append(evaluate(name, m.predict(Xte), y_test, test.minutes))

    results = pd.DataFrame(rows).set_index("model")

    # the linear model is here for its coefficients, not its score
    Xtr, Xte = features(train), features(test)
    Xte = Xte.reindex(columns=Xtr.columns, fill_value=0)
    ridge = Ridge(alpha=1.0).fit(Xtr, y_train)
    results.loc["elo + seed, linear"] = evaluate(
        "elo + seed, linear", ridge.predict(Xte), y_test, test.minutes)

    coefs = pd.Series(ridge.coef_, index=Xtr.columns)

    # Individual coefficients are not readable on their own, because several
    # tags are near duplicates of an overworld dummy. type:structure:lava and
    # type:structure:completable occur ONLY on ruined portal seeds and between
    # them cover all but 3 of 49,000 of them, so that column is effectively the
    # ruined portal column repeated. Ridge splits one real effect across the
    # three arbitrarily and each piece can even come out the wrong sign.
    #
    # What survives that is the sum. For each match, add up every seed
    # coefficient that applies to it, then average within a seed type. That
    # total is what the model actually believes the seed is worth, and it is
    # comparable to the binned estimate in dataset.py.
    seed_cols = [c for c in Xtr.columns if c not in ("elo_mean", "elo_gap")]
    contribution = features(df)[seed_cols] @ coefs[seed_cols]
    totals = pd.DataFrame({
        "effect_pct": 100 * (np.exp(contribution.groupby(df.overworld).mean()
                                    - contribution.mean()) - 1),
        "n": df.groupby("overworld").size(),
    }).sort_values("effect_pct")

    return results, coefs, totals


def print_results(results, coefs, totals, top=10):
    print("\nHOW WELL CAN A RUN TIME BE PREDICTED")
    print(f"{'':<24}{'mean abs error':>16}{'r2 (log scale)':>16}")
    for r in results.itertuples():
        print(f"{str(r.Index):<24}{r.mae_min:>13.2f} min{r.r2_log:>16.3f}")

    elo = coefs.get("elo_mean", 0)
    print(f"\nelo: {100 * elo:+.1f}% run time per 100 elo, so about "
          f"{100 * (np.exp(elo * 5) - 1):+.0f}% over a 500 elo climb")

    print("\nWHAT THE MODEL THINKS EACH SEED TYPE IS WORTH")
    print("total of all seed features, elo held fixed")
    for r in totals.itertuples():
        print(f"  {str(r.Index):<20}{r.effect_pct:>+7.1f}%{r.n:>10,}")

    seed = coefs[coefs.index.str.contains(":")]
    ranked = seed.sort_values()
    print(f"\nINDIVIDUAL TAGS (read with care, see the note in run())")
    print("  fastest")
    for tag, v in ranked.head(top).items():
        print(f"    {tag:<44}{100 * v:>+7.1f}%")
    print("  slowest")
    for tag, v in ranked.tail(top).items():
        print(f"    {tag:<44}{100 * v:>+7.1f}%")


if __name__ == "__main__":
    df = dataset.load(include_censored=False)
    print(f"{len(df):,} finished runs")

    results, coefs, totals = run(df)
    print_results(results, coefs, totals)
