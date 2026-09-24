"""Assert things about the LIVE site's page bodies, not its status codes.

Every failure below has a 200 in front of it, which is why this exists and why
`curl -o /dev/null -w %{http_code}` would have caught none of them:

  stylesheet hash    the HTML references style.<hash>.css and the upload
                     missed that one object. Every page renders unstyled and
                     answers 200. This is the worst one we can ship.
  player.js 404      every server button is inert. The page looks complete.
  empty servers      a build that emitted no .srv on a watch page. 200, a
                     player frame, nothing to press.
  stale rank         /health/rank.txt is fetched by every watch page to demote
                     dead providers. It went 3 days stale in September while
                     answering 200 the whole time.
  source leak        a crawled hostname in markup a visitor or a crawler can
                     read.

Bodies are never printed. A watch page body contains embed hostnames, and this
is meant to be safe to run from a public CI log, so assertions report counts
and names of checks, never the text they matched.

    python scripts/measure/live_canaries.py
    python scripts/measure/live_canaries.py --base https://kakashianime.me
    python scripts/measure/live_canaries.py --rank-max-age 3
"""
from __future__ import annotations
import argparse
import json
import re
import os
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone

# The canaries. A break in any of these means the site is broken for a visitor,
# which is a higher bar than "this page is useful". Picked so that each one
# fails for a different reason: the home page covers the shell and the
# carousel, the watch page covers playback, the series page covers navigation.
HOME = "/"
WATCH = "/watch/frieren-beyond-journeys-end-episode-3"
SERIES = "/series/frieren-beyond-journeys-end"



HOST = re.compile(r"(?<![a-z0-9.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![a-z0-9-])", re.I)


def source_matcher():
    """Crawl-source hostnames, from SOURCE_HOSTS or the private seeds file.

    This list used to be a regex in this file, twelve source brands long. The
    file is meant to run from the public CI repo, and a public file that lists
    our sources is the provenance the whole repo split exists to keep private.
    So the list is data: data/seeds.json in the private repo, the canonical
    list render.py already filters embeds on, and a SOURCE_HOSTS secret
    (comma-separated) in CI.

    Matching is render.py's rule, not a second weaker one: an exact host, or a
    first label of six or more characters that is a prefix of a known source
    brand or has one as its prefix. Sources move domains: the seeds file held
    one domain of a source while the catalogue carried embeds on two others.

    Returns a function body -> number of source hosts in it, or None when no
    list is available anywhere.
    """
    raw = os.environ.get("SOURCE_HOSTS", "").strip()
    if raw:
        hosts = [h for h in raw.split(",") if h.strip()]
    else:
        f = Path(__file__).resolve().parents[2] / "data" / "seeds.json"
        hosts = json.loads(f.read_text()) if f.exists() else []
    hosts = {h.strip().lower().removeprefix("www.") for h in hosts}
    if not hosts:
        return None
    brands = {h.split(".")[0] for h in hosts if len(h.split(".")[0]) >= 6}

    def is_source(host: str) -> bool:
        host = host.lower().removeprefix("www.")
        if host in hosts:
            return True
        label = host.split(".")[0]
        return len(label) >= 6 and any(b.startswith(label) or label.startswith(b)
                                       for b in brands)

    return lambda body: sum(1 for m in HOST.finditer(body) if is_source(m.group(1)))


SHEET = re.compile(r"style\.[a-f0-9]{6,}\.css")

fails: list[str] = []
oks: list[str] = []


def get(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "kakashianime-canary/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def check(name: str, ok: bool, detail: str = "") -> None:
    (oks if ok else fails).append(f"{name}{(' ' + detail) if detail else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://kakashianime.me")
    ap.add_argument("--rank-max-age", type=int, default=3,
                    help="days before /health/rank.txt counts as stale")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    code, home = get(base + HOME)
    check("home 200", code == 200, f"got {code}")

    # The stylesheet the HTML actually asks for, not one we assume it asks for.
    # A hash mismatch between markup and bucket is invisible to a status check
    # and destroys every page on the site at once.
    m = SHEET.search(home)
    check("home references a hashed stylesheet", bool(m))
    if m:
        sc, _ = get(f"{base}/{m.group(0)}")
        check("that stylesheet resolves", sc == 200, f"{m.group(0)} -> {sc}")

    check("home has cards", home.count('class="card"') >= 20,
          f"{home.count('class=\"card\"')} found")
    check("home has a carousel", home.count("hslide") >= 2,
          f"{home.count('hslide')} slides")

    code, watch = get(base + WATCH)
    check("watch 200", code == 200, f"got {code}")
    check("watch has a player", 'id="player"' in watch)
    n_srv = watch.count('class="srv')
    check("watch has servers", n_srv >= 1, f"{n_srv} buttons")
    check("watch has per-track rows", "srvrow" in watch)
    check("watch loads player.js", "/player.js" in watch)
    pj, _ = get(base + "/player.js")
    check("player.js resolves", pj == 200, f"got {pj}")

    code, series = get(base + SERIES)
    check("series 200", code == 200, f"got {code}")
    check("series has an episode list", ("eplist" in series or "class=\"ep\"" in series))

    code, robots = get(base + "/robots.txt")
    check("robots 200", code == 200, f"got {code}")
    check("robots names a sitemap", "Sitemap:" in robots)
    code, sm = get(base + "/sitemap.xml")
    check("sitemap 200", code == 200, f"got {code}")
    check("sitemap has entries", "<loc>" in sm)

    # Every watch page fetches this to demote providers that died today. It
    # answers 200 forever whether or not anything still writes it, so the only
    # check worth having is on the date inside it.
    code, rank = get(base + "/health/rank.txt")
    check("rank 200", code == 200, f"got {code}")
    try:
        v = json.loads(rank).get("v", "")
        when = datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - when
        check(f"rank is under {a.rank_max_age}d old",
              age <= timedelta(days=a.rank_max_age), f"stamped {v}, {age.days}d ago")
    except Exception:
        check("rank parses with a date", False)

    # Search. Every page loads /search.js, which fetches /search-index.txt,
    # and a deploy that ships the pages without that one object gives a search
    # box that finds nothing, at HTTP 200. Keyed on what the live home page
    # actually loads, as the stylesheet check is, so it waits by itself until
    # search is deployed and holds from the first deploy that ships it.
    # .txt, not .json: the edge rule 404s .json keys.
    if 'src="/search.js"' in home:
        sj, _ = get(base + "/search.js")
        check("search.js resolves", sj == 200, f"got {sj}")
        ic, idx = get(base + "/search-index.txt")
        try:
            rows = json.loads(idx) if ic == 200 else None
            n = len(rows) if isinstance(rows, list) else -1
        except ValueError:
            n = -1
        check("search index resolves and holds the catalogue",
              ic == 200 and n > 15000,
              f"HTTP {ic}, {n if n >= 0 else 'not a JSON array'} rows (need > 15,000)")
    else:
        oks.append("search not on the live pages yet; its index check waits for it")

    # Guarded on having fetched something. An empty body matches no hostname,
    # so the unguarded version reported "no source leak" as a pass while the
    # site was down, which is a check that cannot fail when it matters most.
    bodies = [b for b in (home, watch, series) if b]
    source = source_matcher()
    if source is None:
        # Not a pass and not silence. Without the list this check cannot run,
        # and a check that cannot run must say so in the one place people look.
        check("source-leak check has a source list", False,
              "set SOURCE_HOSTS, or run where data/seeds.json exists")
    else:
        leaks = sum(1 for b in bodies if source(b))
        check("no source hostname in any canary", bool(bodies) and leaks == 0,
              f"{leaks} leaked of {len(bodies)} pages fetched")

    for o in oks:
        print(f"  ok    {o}")
    for f in fails:
        print(f"  FAIL  {f}")
    print(f"\n{len(oks)} passed, {len(fails)} failed")
    if not fails:
        return 0
    # Mailed from here, with the failing check as the subject: "[KakashiAnime]
    # live-site: that stylesheet resolves style.x.css -> 404" says what to do.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from notify import alert
    delivered = alert(fails[0] + (f" (+{len(fails) - 1} more)" if len(fails) > 1 else ""),
                      {"failed": fails, "passed": len(oks), "site": base},
                      level="error", check="live-site")
    return 1 if delivered else 3


if __name__ == "__main__":
    sys.exit(main())
