"""Do our providers still play? Run this before every deploy.

Measured 2026-09-05: the top-ranked provider played 6/6 popular titles, so
it became Server 1 on every page. One day later it played 0/4. It had
repurposed itself into a file host, and our URL still returned a 200 shell
that failed at runtime, so nothing short of watching the <video> element
could see it. Every visitor was landing on a dead first server.

That is the failure this script exists to catch. It loads each provider in
an iframe on a foreign origin, clicks once, and reads the frame's video:
playing means currentTime > 0.5, or readyState >= 3 with a real videoWidth.
An HTTP check cannot substitute, because every provider here answers 200
with a shell for titles it does not have.

    python scripts/measure/provider_health.py            # popular titles
    python scripts/measure/provider_health.py --json out.json
"""
from __future__ import annotations
import argparse, json, os, random, sys
from pathlib import Path
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]


def providers() -> list[tuple[str, str, str]]:
    """(name, url template, id kind) for each provider under test.

    Read from PROVIDERS_JSON when set, so this file can live in a public
    repository without naming a single host. Public repositories get
    unmetered Actions minutes, but a public list of the exact players this
    site embeds is provenance we do not publish anywhere else, so the
    harness goes public and the list stays a secret.

    Falls back to the catalogue's own table when running inside the private
    repo, which is what a developer wants locally.
    """
    raw = os.environ.get("PROVIDERS_JSON")
    if raw:
        return [(p["name"], p["tmpl"], p["key"], p.get("slot", i))
                for i, p in enumerate(json.loads(raw))]
    sys.path.insert(0, str(ROOT))
    from scrapers.universal import UNIVERSAL      # noqa: E402
    return [(p["name"], p["tmpl"], p["key"], i) for i, p in enumerate(UNIVERSAL)]

# Popular, long-running, and varied in era: a provider that has any library
# at all should carry these. A failure here is the provider, not the title.
# Three, not five. Each check sits through 17 seconds of deliberate waits, so
# 13 providers x 5 titles is 65 checks and over 50 minutes: both CI runs were
# cancelled mid-flight and the second one only got through two titles. Three
# still separates "this provider is down" from "this provider lacks this
# title", which is the only thing the count is for, and it finishes.
TITLES = [("Attack on Titan", 16498, 16498), ("Mob Psycho 100 II", 37510, 101338),
          ("Death Note", 1535, 1535)]


def overrides() -> dict:
    """What a person measured by hand. Empty when the file is absent.

    publish_rank.py already applies this file to the published verdict. It is
    read here too so the run's own output cannot read as a demotion the
    pipeline is not going to make.
    """
    f = ROOT / "data" / "provider_overrides.json"
    try:
        return json.loads(f.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as ex:
        # A corrupt override file must not silently read as "no human has
        # ever checked anything", which would let this demote a server a
        # person vouched for.
        raise SystemExit(f"  {f} is unreadable ({type(ex).__name__}); refusing "
                         f"to judge providers without it")


def sample_cases(catalog: Path, n: int, audio: str, depth: int,
                 seed: int) -> list[tuple[str, list[tuple[str, str]]]]:
    """Real episode URLs, in the order the page actually renders them.

    The template check above asks "does this provider have a library". It
    cannot ask "does it have THIS episode", and that is where every failure
    Arun reported on 2026-09-21 lived: three providers were ranked and live
    while returning a missing-file page for Frieren episode 7, because the
    same template resolves perfectly for Attack on Titan. A per-title failure
    is invisible to a per-provider check, so all three led pages while broken.

    Episodes are drawn popularity-weighted. A provider that breaks on a title
    nobody opens is a different incident from one that breaks on the front
    page, and only the second is worth waking someone for.

    `depth` is how far down the button list to test. A visitor does not try
    thirteen servers, they try the first and give up after two or three, so
    testing the top few of a lot of episodes measures the real experience
    better than testing all thirteen of three titles for the same minutes.
    """
    rows = json.loads(catalog.read_text())
    # The page's own ordering functions, not a copy of them. A reimplementation
    # here would drift the day someone changes PROVIDER_RANK and this would
    # confidently measure a button order no visitor ever sees.
    sys.path.insert(0, str(ROOT / "site"))
    from render import dropped_language, from_source, rank      # noqa: E402

    pool = [r for r in rows if (r.get("popularity") or 0) > 0 and r.get("embeds")]
    if not pool:
        raise SystemExit(f"  no rows carry both popularity and embeds in {catalog}")
    rng = random.Random(seed)
    cases: list[tuple[str, list[tuple[str, str]]]] = []
    seen: set = set()
    # With replacement, then deduped. Ten times the draws fills n distinct
    # episodes comfortably and, unlike a while loop, cannot spin forever when
    # the popular end of the catalogue is thinner than n.
    for r in rng.choices(pool, weights=[r["popularity"] for r in pool], k=n * 10):
        if len(cases) >= n:
            break
        if r.get("key") in seen:
            continue
        keep = [em for em in r["embeds"]
                if not dropped_language(em.get("provider", ""))]
        ordered = sorted((em for em in keep if not from_source(em)),
                         key=lambda em: rank(em, r.get("playable")))
        urls: list[tuple[str, str]] = []
        for em in ordered:
            prov = em.get("provider", "")
            # Generated players only. A crawled host has no slot in the rank
            # file and naming one anywhere is the one rule with no exceptions.
            if not em.get("generated") or not prov.endswith(f"-{audio}"):
                continue
            urls.append((prov.rpartition("-")[0], em["embed_url"]))
            if len(urls) >= depth:
                break
        if not urls:
            continue
        seen.add(r.get("key"))
        cases.append((f"{r.get('series') or r.get('title')} ep {r.get('episode')}",
                      urls))
    if not cases:
        raise SystemExit(f"  sampled {n * 10} rows and none carried a generated "
                         f"{audio} player; nothing to measure")
    return cases


def playing(frame) -> bool:
    kids = [frame, *frame.child_frames]
    kids += [c for f in frame.child_frames for c in f.child_frames]
    for g in kids:
        try:
            v = g.evaluate("(()=>{const v=document.querySelector('video');"
                           "return v?[v.readyState,v.currentTime,v.videoWidth]:null})()")
            if v:
                return bool(v[1] > 0.5 or (v[0] >= 3 and v[2] > 0))
        except Exception:
            pass
    return False


# A frame that never got a page, or got a bot wall instead of a player, says
# nothing about the provider. From GitHub's runners one provider's five servers
# scored 0/3 while a residential run the same hour had them at 2-3/3, because
# Cloudflare-fronted hosts challenge datacenter IPs. Counting that as dead
# would publish a rank that demotes five working servers for every visitor.
WALLED_STATUS = (401, 403, 429, 503)


def blocked(url: str, seen: dict) -> bool:
    """Did this provider serve a wall instead of a player?

    Judged on the response status, not on the DOM. The first version read
    document.title out of the frame, which is the one thing a bot wall is
    built not to let you do: the run went from 11m40s to over 36 minutes,
    close enough to the 50 minute limit to start cancelling. `evaluate` also
    takes no timeout in this Playwright version, so the obvious fix would
    have thrown on every call and, through the except, marked every provider
    blocked and demoted nothing ever again.

    A status is recorded by a listener as the response arrives, so reading it
    costs nothing and cannot hang. 403 and 503 are what Cloudflare answers a
    datacenter IP; no response at all means the frame never loaded.
    """
    status = seen.get(url)
    return status is None or status in WALLED_STATUS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--audio", default="sub")
    ap.add_argument("--rank", default=None,
                    help="write the public rank file: dead slots only, no names")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="test N real episode URLs from the catalogue instead "
                         "of templates. Catches a provider that is broken on "
                         "one title while fine on another, which a template "
                         "check cannot see. Residential only: CI has no "
                         "catalogue")
    ap.add_argument("--depth", type=int, default=3, metavar="K",
                    help="with --sample, how many buttons down to test. A "
                         "visitor tries the first two or three, not thirteen")
    ap.add_argument("--catalog", default=str(ROOT / "data" / "site_catalog.json"))
    ap.add_argument("--seed", type=int, default=None,
                    help="fix the episode draw so a failure can be re-run")
    ap.add_argument("--min-watchable", type=float, default=0.9,
                    help="with --sample, fail when fewer than this fraction of "
                         "sampled episodes play on ANY of their top buttons")
    ap.add_argument("--vantage", default="ci",
                    help="where this ran. 'residential' is what visitors see and "
                         "outranks 'ci', where Cloudflare walls the runner")
    a = ap.parse_args()

    provs = providers()
    if not provs:
        print("no providers configured (set PROVIDERS_JSON)"); sys.exit(2)
    score = {n: 0 for n, _, _, _ in provs}
    walls = {n: 0 for n, _, _, _ in provs}
    tries = {n: 0 for n, _, _, _ in provs}
    slot_of = {n: s for n, _, _, s in provs}
    # The public CI repo's logs are world-readable. Its whole design keeps the
    # provider table in a secret so the repo names no host, and until
    # 2026-09-24 this printed every name to that log every morning anyway,
    # which published the list the secret exists to hide. In Actions, a
    # provider is its slot number; the mail, which is private, carries names.
    public_log = os.environ.get("GITHUB_ACTIONS") == "true"
    shown = (lambda n: f"slot {slot_of[n]}") if public_log else (lambda n: n)  # noqa: E731

    if a.sample and a.rank:
        # Refuse rather than return quietly having written no rank file. The
        # caller asked for a verdict this mode is not allowed to produce, and
        # a missing rank file is exactly what a working day looks like to
        # publish_rank.
        print("  --sample writes no rank file: a per-episode sample cannot "
              "demote a slot on one observation. Run without --sample for "
              "the provider verdict.")
        sys.exit(2)
    if a.sample:
        cat = Path(a.catalog)
        if not cat.exists():
            # Loud, not a quiet fall back to templates. Silently measuring a
            # weaker thing than the one that was asked for is how a check ends
            # up green for a week while the question it answers has changed.
            print(f"  --sample needs the catalogue and {cat} is not here.")
            print("  This mode is for the residential run; CI has no catalogue.")
            sys.exit(2)
        seed = a.seed if a.seed is not None else random.randrange(1 << 30)
        cases = sample_cases(cat, a.sample, a.audio, a.depth, seed)
        print(f"  {len(cases)} episode(s) sampled popularity-weighted, "
              f"top {a.depth} button(s) each, seed={seed}")
    else:
        cases = [(t, [(nm, tm.format(id=mal if ky == "mal" else ani, ep=1,
                                     audio=a.audio))
                      for nm, tm, ky, _ in provs])
                 for t, mal, ani in TITLES]
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True,
                               args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        ctx.on("page", lambda p: p.close() if p != page else None)
        # Recorded as responses arrive. Reading a status later cannot hang;
        # asking the frame cost 25 minutes a run.
        seen_status: dict = {}
        ctx.on("response", lambda r: seen_status.__setitem__(r.url, r.status))
        watchable, lead_ok, nothing_played = 0, 0, []
        for case, urls in cases:
            print(f"== {case}")
            played_here = []
            for name, url in urls:
                page.set_content(
                    f'<iframe src="{url}" width=900 height=506 allow=autoplay '
                    f'referrerpolicy="no-referrer"></iframe>', wait_until="commit")
                page.wait_for_timeout(9000)
                f = page.frames[1] if len(page.frames) > 1 else None
                if f:
                    try:
                        f.frame_element().click(timeout=1500, force=True)
                    except Exception:
                        pass
                page.wait_for_timeout(8000)
                for p in ctx.pages:
                    if p != page:
                        p.close()
                ok = bool(f) and playing(f)
                wall = (not ok) and blocked(url, seen_status)
                score[name] += ok
                walls[name] += wall
                tries[name] += 1
                if ok:
                    played_here.append(name)
                print(f"   {shown(name):16s} {'PLAYS' if ok else ('blocked' if wall else 'dead ')}")
            # The two numbers a visitor would recognise. "Did the first button
            # work" and, when it did not, "was anything they would plausibly
            # try next any better".
            lead_ok += bool(played_here) and urls[0][0] in played_here
            watchable += bool(played_here)
            if not played_here:
                nothing_played.append(case)
        b.close()

    n = len(cases)
    kind = f"{n} sampled episode(s)" if a.sample else f"{n} popular titles"
    print(f"\n  provider health, {kind}, audio={a.audio}")
    # Dead means: played nothing AND at least one attempt actually reached the
    # provider. A provider that walled every attempt is unverified from this
    # vantage point and is neither demoted nor cleared. Counted against its own
    # attempts, because under --sample a provider is only offered on the
    # episodes where it ranked into the top buttons.
    unverified = [k for k in score
                  if tries[k] and score[k] == 0 and walls[k] == tries[k]]
    # A provider a person has watched outranks this check in both directions,
    # and the reason is measured, not theoretical: slot 10 scores 0 here on
    # the exact URL Arun watched to the end on 2026-09-20. Headless and
    # headful, blank origin and the site's own origin, referrer policy
    # matching the page: no <video> ever appears, so something about the way
    # it loads is outside what this harness can see. Printing DEAD next to it
    # invites someone to demote a server that works for every visitor.
    vouched = {k for k in score
               if str(slot_of[k]) in (overrides().get("working") or {})}
    for name, hits in sorted(score.items(), key=lambda kv: (-kv[1], kv[0])):
        if not tries[name]:
            continue
        flag = ("   <-- unverified: blocked from here" if name in unverified
                else "   <-- a person watched this; the check cannot see it"
                if hits == 0 and name in vouched
                else "   <-- DEAD, must not lead" if hits == 0 else "")
        print(f"    {shown(name):16s} {hits}/{tries[name]}{flag}")
    if a.json and not a.sample:
        Path(a.json).write_text(json.dumps(score, indent=1))
        print(f"\n  saved -> {a.json}")
    if a.sample:
        # The question this mode exists to answer, and the one Arun actually
        # asked: could a visitor watch the episode they opened. Provider pass
        # rates above are the diagnosis; this is the symptom.
        pct = 100.0 * watchable / n
        print(f"\n  first button played on {lead_ok}/{n} episode(s)")
        print(f"  something played on   {watchable}/{n} ({pct:.0f}%)")
        for c in nothing_played:
            print(f"  NOTHING PLAYED: {c}")
        if a.json:
            Path(a.json).write_text(json.dumps(
                {"sampled": n, "depth": a.depth, "seed": seed,
                 "lead_ok": lead_ok, "watchable": watchable,
                 "nothing_played": nothing_played,
                 "score": {k: f"{v}/{tries[k]}" for k, v in score.items()
                           if tries[k]}}, indent=1))
            print(f"  saved -> {a.json}")
        # No rank file from this mode. A provider that misses one episode is
        # not dead, and writing a slot verdict from a per-title sample would
        # demote a working host on the strength of one bad title.
        if watchable >= a.min_watchable * n:
            return
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify import alert
        delivered = alert(
            f"KakashiAnime: {n - watchable} of {n} sampled episodes play on "
            f"none of their top {a.depth} servers",
            {"episodes": nothing_played[:20],
             "first_button_ok": f"{lead_ok}/{n}",
             "providers": {k: f"{v}/{tries[k]}" for k, v in score.items()
                           if tries[k]}})
        sys.exit(1 if delivered else 3)

    dead = [k for k, v in score.items() if tries[k] and v == 0
            and k not in unverified and k not in vouched]
    if a.rank:
        # Slots, never names: this file is served from the site's own domain.
        import datetime
        Path(a.rank).write_text(json.dumps({
            "v": datetime.datetime.utcnow().strftime("%Y%m%d"),
            "vantage": a.vantage,
            "titles": n,
            "dead": sorted(slot_of[k] for k in dead),
            "unverified": sorted(slot_of[k] for k in unverified),
            "score": {str(slot_of[k]): v for k, v in score.items()},
        }))
        # Slot -> name, for the alert mail only. publish_rank strips this
        # before the file goes live; the served rank.txt names nothing.
        Path(a.rank).with_suffix(".names.json").write_text(
            json.dumps({str(s): k for k, s in slot_of.items()}))
        print(f"  rank -> {a.rank}  ({len(dead)} dead, {len(unverified)} unverified slot(s))")
    if dead:
        print(f"\n  {len(dead)} provider(s) play nothing: {', '.join(map(shown, dead))}")
    # No alert and no failure exit from here. The same three providers were
    # dead every day for a week and this exited 1 every day, which made the
    # CI job permanently red and the daily mail permanently ignorable. Dead
    # providers are the INPUT to publish_rank, which demotes them on the live
    # pages and alerts only when the set changes. This step fails only when
    # it could not judge at all, which is an exception or exit 2 above.
    if a.rank and not Path(a.rank).exists():
        sys.exit(1)


if __name__ == "__main__":
    main()
