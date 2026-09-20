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
import argparse, json, os, sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def report(dead: list[str], score: dict, n: int) -> None:
    """Send dead providers to Sentry, if a DSN is configured.

    No DSN is not an error: this script's job is to fail the build, and it
    does that with an exit code whether or not anyone is listening. Sentry
    only adds the "who do we tell at 3am" part.

    One event per run, not per provider, so a total outage pages once instead
    of thirteen times.
    """
    if not dead:
        return True
    # notify.alert posts the envelope itself and returns whether Sentry took
    # it. The SDK's capture_message hands back an event id on a 429 too, and
    # that is how four dead providers were "reported" to an exhausted quota.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from notify import alert
    return alert(f"KakashiAnime: {len(dead)} provider(s) play nothing: {', '.join(dead)}",
                 {"providers": {k: f"{v}/{n}" for k, v in score.items()}})

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
    ap.add_argument("--vantage", default="ci",
                    help="where this ran. 'residential' is what visitors see and "
                         "outranks 'ci', where Cloudflare walls the runner")
    a = ap.parse_args()

    provs = providers()
    if not provs:
        print("no providers configured (set PROVIDERS_JSON)"); sys.exit(2)
    score = {n: 0 for n, _, _, _ in provs}
    walls = {n: 0 for n, _, _, _ in provs}
    slot_of = {n: s for n, _, _, s in provs}
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
        for title, mal, ani in TITLES:
            print(f"== {title}")
            for name, tmpl, key, _slot in provs:
                url = tmpl.format(id=mal if key == "mal" else ani, ep=1, audio=a.audio)
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
                print(f"   {name:16s} {'PLAYS' if ok else ('blocked' if wall else 'dead ')}")
        b.close()

    n = len(TITLES)
    print(f"\n  provider health, {n} popular titles, audio={a.audio}")
    # Dead means: played nothing AND at least one attempt actually reached
    # the provider. A provider that walled every attempt is unverified from
    # this vantage point and is neither demoted nor cleared.
    unverified = [k for k in score if score[k] == 0 and walls[k] == n]
    for name, hits in sorted(score.items(), key=lambda kv: -kv[1]):
        flag = ("   <-- unverified: blocked from here" if name in unverified
                else "   <-- DEAD, must not lead" if hits == 0 else "")
        print(f"    {name:16s} {hits}/{n}{flag}")
    if a.json:
        Path(a.json).write_text(json.dumps(score, indent=1))
        print(f"\n  saved -> {a.json}")
    dead = [k for k, v in score.items() if v == 0 and k not in unverified]
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
        print(f"  rank -> {a.rank}  ({len(dead)} dead, {len(unverified)} unverified slot(s))")
    if dead:
        print(f"\n  {len(dead)} provider(s) play nothing: {', '.join(dead)}")
        delivered = report(dead, score, n)
        # 1: dead providers, someone was told. 3: dead providers AND the
        # alert did not land, which is the worse of the two and must read
        # differently in a run list.
        sys.exit(1 if delivered else 3)


if __name__ == "__main__":
    main()
