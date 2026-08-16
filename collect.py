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


def get_page(before=None, after=None, match_type=None):
    """Fetch up to 100 matches, newest first. Retries on rate limit."""
    params = {"count": PAGE_SIZE}
    if before is not None:
        params["before"] = before
    if after is not None:
        params["after"] = after
    if match_type is not None:
        params["type"] = match_type

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


def backfill(conn, pages, match_type=2):
    """Walk backwards from the oldest match we have (or from now, if empty).

    match_type=2 asks the server for ranked matches only. This is a filter on
    which *game* we are studying, not on anything the seed could have caused:
    unranked matches carry no elo at all, so they can never be used here.
    Filtering on elo or on run time would be a different and much worse idea,
    see the "What not to filter" section of the README.
    """
    lo, _ = db.bounds(conn)
    cursor = lo  # None on an empty database, which means "start at newest"
    total_new = 0
    started = time.time()

    for page in range(1, pages + 1):
        matches = get_page(before=cursor, match_type=match_type)
        if not matches:
            print("reached the end of available history")
            break

        new = db.insert_matches(conn, matches)
        total_new += new
        cursor = matches[-1]["id"]

        elapsed = time.time() - started
        print(
            f"page {page}/{pages}  "
            f"ids {matches[0]['id']}..{matches[-1]['id']}  "
            f"+{new} new  "
            f"total {db.count(conn):,}  "
            f"({elapsed/60:.1f} min)"
        )
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    return total_new


def update(conn):
    """Fetch everything newer than the newest match we already have."""
    _, hi = db.bounds(conn)
    if hi is None:
        print("database is empty, run backfill first")
        return 0

    total_new = 0
    cursor = None
    while True:
        matches = get_page(before=cursor)
        if not matches:
            break

        # stop once the page runs past what we already stored
        fresh = [m for m in matches if m["id"] > hi]
        total_new += db.insert_matches(conn, fresh)

        if len(fresh) < len(matches):
            break  # caught up

        cursor = matches[-1]["id"]
        print(f"  +{len(fresh)} (total new {total_new})")
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    print(f"up to date, {total_new} new matches, {db.count(conn):,} stored")
    return total_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    p_back = sub.add_parser("backfill", help="walk backwards into history")
    p_back.add_argument(
        "--pages", type=int, default=100,
        help="pages of 100 matches to fetch (default 100, so 10k matches)",
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
            n = backfill(conn, args.pages, args.match_type or None)
            print(f"\ndone, {n} new matches, {db.count(conn):,} stored total")
        else:
            update(conn)
    except KeyboardInterrupt:
        print(f"\nstopped, {db.count(conn):,} matches stored, safe to resume")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
