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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--audio", default="sub")
    ap.add_argument("--rank", default=None,
                    help="write the public rank file: dead slots only, no names")
    a = ap.parse_args()

    provs = providers()
    if not provs:
        print("no providers configured (set PROVIDERS_JSON)"); sys.exit(2)
    score = {n: 0 for n, _, _, _ in provs}
    slot_of = {n: s for n, _, _, s in provs}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True,
                               args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        ctx.on("page", lambda p: p.close() if p != page else None)
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
                score[name] += ok
                print(f"   {name:16s} {'PLAYS' if ok else 'dead '}")
        b.close()

    n = len(TITLES)
    print(f"\n  provider health, {n} popular titles, audio={a.audio}")
    for name, hits in sorted(score.items(), key=lambda kv: -kv[1]):
        flag = "   <-- DEAD, must not lead" if hits == 0 else ""
        print(f"    {name:16s} {hits}/{n}{flag}")
    if a.json:
        Path(a.json).write_text(json.dumps(score, indent=1))
        print(f"\n  saved -> {a.json}")
    dead = [k for k, v in score.items() if v == 0]
    if a.rank:
        # Slots, never names: this file is served from the site's own domain.
        import datetime
        Path(a.rank).write_text(json.dumps({
            "v": datetime.datetime.utcnow().strftime("%Y%m%d"),
            "titles": n,
            "dead": sorted(slot_of[k] for k in dead),
            "score": {str(slot_of[k]): v for k, v in score.items()},
        }))
        print(f"  rank -> {a.rank}  ({len(dead)} dead slot(s))")
    if dead:
        print(f"\n  {len(dead)} provider(s) play nothing: {', '.join(dead)}")
        delivered = report(dead, score, n)
        # 1: dead providers, someone was told. 3: dead providers AND the
        # alert did not land, which is the worse of the two and must read
        # differently in a run list.
        sys.exit(1 if delivered else 3)


if __name__ == "__main__":
    main()
