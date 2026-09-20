"""Can a visitor watch the episode that aired last night?

This is the failure Arun reported from the live site: it was showing last
week. Nothing errored. No watch went red. One Piece had aired 1178 and the
site's newest page was 1177, so anyone searching for the new episode found a
404 on the one page they wanted.

Every other check here asks whether something is broken. This one asks
whether the site is CURRENT, which is a different question and the one that
is invisible from the inside: stale data looks exactly like fresh data.

It measures from outside, with public data only:

  1. AniList, keyless, for the airing titles people actually search
  2. `nextAiringEpisode.episode - 1` is what has aired
  3. ask the live site for that episode's page

A title is only counted against us when its series page exists and the aired
episode's page does not. A title we cannot locate is skipped rather than
guessed at, because a slug we failed to derive is our problem, not a staleness
signal, and a watch that invents failures gets ignored.

    python scripts/measure/airing_lag.py
    python scripts/measure/airing_lag.py --titles 40 --max-behind 3

Exit 0 current, 1 behind, 3 behind and the alert did not land.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE = "https://kakashianime.me"
ANILIST = "https://graphql.anilist.co"
# Cloudflare answers 403 to urllib's default User-Agent.
UA = "kakashianime-watch/1 (+https://kakashianime.me)"

QUERY = """
query ($page: Int, $per: Int) {
  Page(page: $page, perPage: $per) {
    media(status: RELEASING, type: ANIME, sort: POPULARITY_DESC, isAdult: false) {
      id
      title { romaji english }
      nextAiringEpisode { episode }
    }
  }
}
"""


def slugify(s: str) -> str:
    """The builder's rule, repeated here so the watch needs nothing private."""
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")[:90]


def head(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status
    except urllib.error.HTTPError as ex:
        return ex.code
    except Exception:
        return 0


def airing(n: int) -> list[dict]:
    body = json.dumps({"query": QUERY, "variables": {"page": 1, "per": n}}).encode()
    req = urllib.request.Request(ANILIST, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["data"]["Page"]["media"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--titles", type=int, default=25)
    ap.add_argument("--max-behind", type=int, default=2,
                    help="how many titles may lag before this fails")
    ap.add_argument("--site", default=SITE)
    a = ap.parse_args()

    try:
        media = airing(a.titles)
    except Exception as ex:
        # AniList returned 403 "temporarily disabled" for four days this
        # month. That is an outage of the source, not of the site, and it
        # must not read as "the site is fine" either.
        print(f"  AniList unreachable ({type(ex).__name__}); nothing measured")
        return 0

    behind, checked, skipped = [], 0, 0
    for m in media:
        ep = (m.get("nextAiringEpisode") or {}).get("episode")
        if not ep or ep < 2:
            continue
        aired = ep - 1
        names = [m["title"].get("romaji"), m["title"].get("english")]
        slug = next((slugify(t) for t in names
                     if t and head(f"{a.site}/series/{slugify(t)}") == 200), None)
        if not slug:
            skipped += 1
            continue
        checked += 1
        code = head(f"{a.site}/watch/{slug}-episode-{aired}")
        if code != 200:
            behind.append(f"{names[0]}: episode {aired} aired, its page is {code}")
        time.sleep(0.4)

    for b in behind:
        print(f"  BEHIND: {b}")
    print(f"  {checked} airing titles checked, {len(behind)} behind, "
          f"{skipped} not locatable on the site")

    if len(behind) <= a.max_behind:
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from notify import alert
    delivered = alert(
        f"KakashiAnime: {len(behind)} of {checked} airing titles are missing "
        f"the episode that already aired",
        {"behind": behind[:20]}, level="error")
    return 1 if delivered else 3


if __name__ == "__main__":
    sys.exit(main())
