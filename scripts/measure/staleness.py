"""Did the daily job actually run?

Every other watch here answers "is the site broken". None of them answers
"did anything look". Between 16 and 20 September the laptop half of the
pipeline did not run once, because its scheduler was never installed, and
nothing said a word: the airing schedule went four days stale, 110 of 120
airing titles were showing an air time in the past, and the live provider
ranking fell back to the CI verdict with six providers stuck at "unverified"
that a residential run resolves. Every individual watch was green.

This is the dead-man switch. It runs from CI, which is on a machine that is
always awake, and it checks the age of the things the laptop is supposed to
produce. A silent pipeline is now a loud one.

    python scripts/measure/staleness.py
    python scripts/measure/staleness.py --max-age 2

Exit 0 everything fresh, 1 something is stale, 3 stale and the alert did not
land.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SITE = "https://kakashianime.me"
RANK = "/health/rank.txt"
# Written by daily.sh at exit, whatever the exit. The direct answer to "did
# the laptop nightly run", where the rank's vantage is only a side effect.
NIGHTLY = "/health/nightly.txt"


# Cloudflare answers 403 to urllib's default User-Agent (its "error 1010"
# family). Measured here: the same URL is 200 to curl and 403 to a bare
# urlopen, so without this the watch would have reported the rank file
# unreachable every single day and been ignored within a week.
UA = "kakashianime-watch/1 (+https://kakashianime.me)"


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as ex:
        return ex.code, ""
    except Exception:
        return 0, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=int, default=2,
                    help="days a residential verdict may be before it is stale")
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--nightly-max-hours", type=float, default=30,
                    help="hours since the laptop nightly last finished; it runs "
                         "daily, so 30 allows one late or slow run")
    a = ap.parse_args()

    stale: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    # Asked first because it is the cause, and the rank check below is only a
    # symptom of it. 22 to 24 September: the job fired every morning and
    # macOS refused bash access to ~/Desktop before line one. A nightly that
    # cannot start never writes this file, so absence is the finding.
    status, body = fetch(f"{a.site}{NIGHTLY}?probe={now.strftime('%Y%m%d%H')}")
    if status == 404 or (status == 200 and not body):
        stale.append("the laptop nightly has never reported finishing: nothing is "
                     "refreshing airing data, building new episode pages or uploading")
    elif status != 200:
        stale.append(f"{NIGHTLY} unreachable (HTTP {status})")
    else:
        try:
            hb = json.loads(body)
            done = dt.datetime.fromisoformat(hb["finished"].replace("Z", "+00:00"))
            hours = (now - done).total_seconds() / 3600
            print(f"  nightly    : finished {hb['finished']} ({hours:.0f}h ago), "
                  f"exit {hb.get('exit')}")
            if hours > a.nightly_max_hours:
                stale.append(f"the laptop nightly last finished {hours:.0f} hours ago "
                             f"({hb['finished'][:10]}); new episodes are not being built")
            if hb.get("exit") == 3:
                # The laptop found something and could not mail it. This is
                # the only machine that can say so.
                stale.append("the laptop nightly finished with exit 3: a check there "
                             "found a problem and its mail did not go out. Read "
                             "logs/daily-stdout.log on the laptop")
        except Exception as ex:
            stale.append(f"{NIGHTLY} is not readable: {type(ex).__name__}")

    # Cache-bust: the edge holds a bare URL for hours, and asking it whether a
    # file is fresh through a cached copy answers the wrong question.
    status, body = fetch(f"{a.site}{RANK}?probe={today.strftime('%Y%m%d%H')}")
    if status != 200 or not body:
        stale.append(f"{RANK} unreachable (HTTP {status})")
    else:
        try:
            rank = json.loads(body)
            v = dt.datetime.strptime(str(rank.get("v", "")), "%Y%m%d").date()
            age = (today - v).days
            vantage = rank.get("vantage", "unknown")
            print(f"  rank.txt   : v={v} ({age}d old), vantage={vantage}, "
                  f"{len(rank.get('dead', []))} dead, "
                  f"{len(rank.get('unverified', []))} unverified")
            if age > a.max_age:
                stale.append(f"provider ranking is {age} days old")
            # A CI verdict is partial: Cloudflare walls the runner on some
            # providers, so slots sit at "unverified" until a residential run
            # clears them. Living on the CI verdict means the laptop half has
            # not run.
            if vantage != "residential" and rank.get("unverified"):
                stale.append(
                    f"ranking is the CI verdict with {len(rank['unverified'])} "
                    f"provider(s) unverified; no residential run has cleared them")
        except Exception as ex:
            stale.append(f"{RANK} is not readable: {type(ex).__name__}")

    for w in stale:
        print(f"  STALE: {w}")
    if not stale:
        print("  the nightly has finished recently and the ranking is current")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from notify import alert
    # The subject is the first finding itself, which is the cause when there
    # is one, so the inbox line says what broke without opening the mail.
    delivered = alert(
        stale[0] + (f" (+{len(stale) - 1} more)" if len(stale) > 1 else ""),
        {"findings": stale,
         "check_on_the_laptop": "tail logs/daily-stderr.log; launchctl list "
                                "me.kakashianime.daily"},
        level="error", check="nightly-watchdog")
    return 1 if delivered else 3


if __name__ == "__main__":
    sys.exit(main())
