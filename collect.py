"""Download MCSR Ranked matches into a local SQLite database.

The match list endpoint returns 100 matches per request and already contains
the seed, both players with their elo, and the result. That is everything the
seed difficulty model needs, so this never touches the per-match endpoint.

Two modes:

    python collect.py backfill --pages 200   walk backwards into history
    python collect.py update                 pick up matches since last run

Both are safe to stop with Ctrl-C and safe to run again. Progress lives in the
database itself, not in a state file.
"""

import argparse
import calendar
import sys
import time

import requests

import db

API = "https://api.mcsrranked.com"
PAGE_SIZE = 100

# The documented limit is 500 requests per 10 minutes, which is one request
# every 1.2 seconds. Going a little slower than allowed costs nothing and
# keeps us clearly on the right side of it.
SECONDS_BETWEEN_REQUESTS = 1.5

session = requests.Session()
session.headers["User-Agent"] = "mcsr-seed-difficulty/0.1 (learning project)"


def get_page(before=None, after=None, match_type=None, season=None):
    """Fetch up to 100 matches, newest first. Retries on rate limit."""
    params = {"count": PAGE_SIZE}
    if before is not None:
        params["before"] = before
    if after is not None:
        params["after"] = after
    if match_type is not None:
        params["type"] = match_type
    if season is not None:
        params["season"] = season

    for attempt in range(5):
        try:
            r = session.get(f"{API}/matches", params=params, timeout=30)
        except requests.RequestException as e:
            wait = 5 * (attempt + 1)
            print(f"  network error ({e}), retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  rate limited, backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"API said: {body}")
        return body["data"]

    raise RuntimeError("gave up after 5 attempts")


def backfill(conn, pages, match_type=2, until=None, season=None):
    """Walk backwards from the oldest match we have (or from now, if empty).

    match_type=2 asks the server for ranked matches only. This is a filter on
    which *game* we are studying, not on anything the seed could have caused:
    unranked matches carry no elo at all, so they can never be used here.
    Filtering on elo or on run time would be a different and much worse idea,
    see the "What not to filter" section of the README.

    `until` (unix seconds) stops the walk once it reaches matches older than
    that, so "all of season 10" is a date instead of a guessed page count.

    Without `season`, the API only pages back a few months, then returns
    nothing, which looks exactly like the end of history. Older seasons are
    only reachable by asking for them by number. With `season`, the walk
    starts below the oldest match stored for that season, or at the season's
    newest match if there is none yet, and ends when the season runs out.
    """
    if season is None:
        lo, _ = db.bounds(conn)
    else:
        lo = conn.execute("SELECT MIN(id) FROM matches WHERE season = ?",
                          (season,)).fetchone()[0]
    cursor = lo  # None when nothing is stored yet, which means "start at newest"
    total_new = 0
    started = time.time()

    page = 0
    while pages is None or page < pages:
        page += 1
        matches = get_page(before=cursor, match_type=match_type, season=season)
        if not matches:
            print("reached the start of the season" if season else
                  "reached the end of available history")
            break

        if until is not None:
            matches = [m for m in matches if m["date"] >= until]
            if not matches:
                print("reached the --until date")
                break

        new = db.insert_matches(conn, matches)
        total_new += new
        cursor = matches[-1]["id"]

        elapsed = time.time() - started
        day = time.strftime("%Y-%m-%d", time.gmtime(matches[-1]["date"]))
        print(
            f"page {page}/{pages or '?'}  back to {day}  "
            f"ids {matches[0]['id']}..{matches[-1]['id']}  "
            f"+{new} new  "
            f"total {db.count(conn):,}  "
            f"({elapsed/60:.1f} min)"
        )
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    return total_new


def update(conn, match_type=2):
    """Fetch everything newer than the newest match we already have.

    The API only pages newest first, even with `after`, so this has to walk
    down from now until it meets data we already have. After a few weeks away
    that walk takes hours. If it were stopped halfway, the matches it did get
    would become the new "newest", and the next run would stop at them and
    never fill the hole underneath. So the floor and the cursor are saved in
    the database, and an interrupted walk carries on where it stopped.

    The walk can also cross a season boundary. Without a `season` parameter
    the API stops at the start of the current season and returns an empty
    page, which looks exactly like being caught up. That once left two weeks
    of season 11 silently missing. So an empty page before reaching stored
    data means "step into the previous season", never "done".
    """
    floor = db.get_state(conn, "update_floor")
    cursor = db.get_state(conn, "update_cursor")
    season = db.get_state(conn, "update_season")
    last_season = db.get_state(conn, "update_last_season")
    resumed = floor is not None

    if resumed:
        print(f"resuming an unfinished update, filling down to id {floor}")
    else:
        _, floor = db.bounds(conn)
        if floor is None:
            print("database is empty, run backfill first")
            return 0
        db.set_state(conn, "update_floor", floor)

    total_new = 0
    stepped = False
    while True:
        matches = get_page(before=cursor, match_type=match_type, season=season)
        if not matches:
            if stepped or last_season is None:
                # two empty pages in a row, something else is going on.
                # keep the state so the next run resumes right here
                print("the API returned nothing before reaching stored data, "
                      "stopping without marking the update as done")
                return total_new
            season = last_season - 1
            stepped = True
            db.set_state(conn, "update_season", season)
            print(f"  reached the start of season {last_season}, "
                  f"carrying on in season {season}")
            continue
        stepped = False

        # stop once the page runs past what we already stored
        fresh = [m for m in matches if m["id"] > floor]
        total_new += db.insert_matches(conn, fresh)

        if len(fresh) < len(matches):
            break  # caught up

        cursor = matches[-1]["id"]
        last_season = matches[-1]["season"]
        db.set_state(conn, "update_cursor", cursor)
        db.set_state(conn, "update_last_season", last_season)
        day = time.strftime("%Y-%m-%d", time.gmtime(matches[-1]["date"]))
        print(f"  back to {day}, id {cursor}  +{len(fresh)} (total new {total_new:,})")
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    db.clear_state(conn)
    print(f"up to date, {total_new:,} new matches, {db.count(conn):,} stored")

    # a resumed walk started below the present, so pick up what came in since
    if resumed:
        total_new += update(conn, match_type)
    return total_new


def date_arg(s):
    """YYYY-MM-DD, read as midnight UTC, to unix seconds."""
    return calendar.timegm(time.strptime(s, "%Y-%m-%d"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_back = sub.add_parser("backfill", help="walk backwards into history")
    p_back.add_argument(
        "--pages", type=int, default=None,
        help="pages of 100 matches to fetch (default 100, or no limit with --until)",
    )
    p_back.add_argument(
        "--season", type=int, default=None,
        help="walk back through this season only, e.g. 10. Needed for anything "
             "older than a few months, see backfill()",
    )
    p_back.add_argument(
        "--until", type=date_arg, default=None,
        help="stop at matches older than this UTC date, e.g. 2026-01-02",
    )
    p_back.add_argument(
        "--type", type=int, default=2, dest="match_type",
        help="match type to ask the server for (default 2, ranked). "
             "Pass 0 for everything, which is 2.25x slower per usable match.",
    )
    sub.add_parser("update", help="fetch matches newer than the last run")

    args = parser.parse_args()
    conn = db.connect()

    try:
        if args.mode == "backfill":
            pages = args.pages or (None if args.until or args.season else 100)
            n = backfill(conn, pages, args.match_type or None, args.until, args.season)
            print(f"\ndone, {n} new matches, {db.count(conn):,} stored total")
        else:
            update(conn)
    except KeyboardInterrupt:
        print(f"\nstopped, {db.count(conn):,} matches stored, safe to resume")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
