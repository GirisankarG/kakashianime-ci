"""Put the day's provider rank where every page can read it.

The health check decides which providers are dead today. This puts that verdict
at `health/rank.txt` in the site bucket, and player.js reads it at load and
moves dead slots to the back of the server list. That is the whole loop that
lets a provider die overnight without leading on 265,000 pages until someone
rebuilds: the pages do not change, the small file does.

The file carries slot numbers only. It is fetched from the site's own domain,
and no provider name may appear anywhere a visitor can see.

    python scripts/measure/publish_rank.py rank.json

Credentials come from R2_* environment variables, the same names .env uses, so
this runs unchanged from a laptop and from the public CI repo's secrets. It
refuses an empty or malformed file rather than publishing it: a bad rank file
is worse than none, since none leaves the pages exactly as built.

Cache-Control is short. Cloudflare otherwise serves the old body for hours
after the key is overwritten; player.js also adds the date as a query string,
so a stale edge copy under the bare URL costs nothing.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import boto3

# .txt, not .json. The edge layer in front of the bucket serves .txt, .css,
# .js and .xml and answers 404 to any .json key, measured 2026-09-16 with a
# fresh probe of each. The body is still JSON; fetch().json() does not care
# what the extension says.
KEY = "health/rank.txt"
# Slots a person tested by hand and found working. Automation does not get to
# overrule them: on 2026-09-20 a CI run published slot 10 as dead and it was
# demoted on every page, while Arun had just played it through to the end.
# A headless browser on a datacenter IP is not what a viewer sees.
OVERRIDES = Path(__file__).resolve().parents[2] / "data" / "provider_overrides.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    try:
        rank = json.loads(src.read_text())
        # Slot -> provider name, written beside the rank file by the health check.
        # Used for the alert mail only and removed before publishing: rank.txt is
        # served from the site's own domain and must name no provider.
        names_file = Path(src).with_suffix(".names.json")
        rank["names"] = json.loads(names_file.read_text()) if names_file.exists() else {}
        assert isinstance(rank.get("dead"), list), "no dead list"
        assert isinstance(rank.get("score"), dict) and rank["score"], "no score map"
        assert all(str(k).isdigit() for k in rank["score"]), "score keys must be slots"
    except Exception as ex:
        print(f"refusing to publish {src}: {ex}")
        return 1

    env = {k: os.environ.get(k) for k in
           ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"missing {', '.join(missing)}")
        return 1
    bucket = os.environ.get("R2_BUCKET", "anime")

    s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT"],
                      aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")

    # Vantage decides, not arrival order. A CI run is walled by Cloudflare on
    # some providers and a residential run is not, so a CI verdict must never
    # land on top of the same day's residential one. Without this the file is
    # simply whoever published last: a manual CI dispatch this afternoon
    # replaced a residential verdict and moved a provider back to unverified.
    mine = rank.get("vantage", "ci")
    try:
        current = json.loads(s3.get_object(Bucket=bucket, Key=KEY)["Body"].read())
    except Exception:
        current = {}

    # One failing run is weak evidence, and acting on it alone is how a
    # working provider got demoted site-wide. A slot is published dead only
    # once it has failed twice running; the first failure is recorded as
    # pending and demotes nobody. Recovery stays immediate: one pass and it
    # leaves both lists.
    fresh = set(rank.get("dead", []))
    before = set(current.get("dead", [])) | set(current.get("pending", []))
    held = sorted(fresh - before)
    confirmed = set(fresh & before)

    # "Unverified" means this run could not reach the provider at all, and the
    # health check promises such a slot is "neither demoted nor cleared". This
    # file used to clear it anyway: a slot the laptop found dead, walled from
    # CI the next morning, simply fell out of the dead list and went back to
    # leading pages on no evidence. Carry the previous verdict forward instead.
    unverified = set(rank.get("unverified", []))
    carried = sorted(set(current.get("dead", [])) & unverified)
    confirmed |= set(carried)

    # And a human verdict outranks both.
    try:
        ov = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
        never = {int(k) for k, v in (ov.get("working") or {}).items()}
    except Exception:
        never = set()
    vetoed = sorted(s for s in confirmed if s in never)
    confirmed -= never

    # And the same evidence in the other direction. The check never demotes a
    # provider it cannot reach, so one that is walled from CI and broken in a
    # real browser would sit near the top forever on nobody's say-so.
    try:
        broken = {int(k) for k in (ov.get("broken") or {})}
    except Exception:
        broken = set()
    added = sorted(broken - confirmed)
    confirmed = sorted(confirmed | broken)

    rank["dead"] = confirmed
    rank["pending"] = held
    if held:
        print(f"  {len(held)} slot(s) failed once and are held, not demoted: {held}")
    if carried:
        print(f"  {len(carried)} slot(s) unreachable from here, previous verdict kept: {carried}")
    if vetoed:
        print(f"  {len(vetoed)} slot(s) failed but a person found them working: {vetoed}")
    if added:
        print(f"  {len(added)} slot(s) demoted because a person found them broken: {added}")
    if (current.get("v") == rank.get("v")
            and current.get("vantage") == "residential" and mine != "residential"):
        # Before the alert, not after: this run publishes nothing, so any
        # "change" it computed is a difference of vantage, not of the site.
        print(f"keeping today's residential verdict; not overwriting it from {mine}")
        return 0

    # The alert belongs here, not in the health check, because only this step
    # knows what CHANGED on the live pages. The check found the same three dead
    # providers every day for a week and exited 1 every day, which made the CI
    # job permanently red and its mail permanently ignorable. Steady state is
    # not news. A slot newly demoted site-wide, or one back, is.
    was_dead = set(current.get("dead", []))
    now_dead = set(confirmed)
    newly_dead = sorted(now_dead - was_dead)
    # "Back" needs evidence of playing, so a slot this run could not reach is
    # never reported as recovered.
    recovered = sorted(was_dead - now_dead - unverified)
    if newly_dead or recovered:
        names = {str(k): v for k, v in (rank.get("names") or {}).items()}
        label = lambda s: names.get(str(s), f"slot {s}")   # noqa: E731
        parts = []
        if newly_dead:
            parts.append(f"{len(newly_dead)} server(s) newly demoted on every page: "
                         + ", ".join(label(s) for s in newly_dead))
        if recovered:
            parts.append(f"{len(recovered)} server(s) playing again: "
                         + ", ".join(label(s) for s in recovered))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify import alert
        alert("; ".join(parts),
              {"newly_demoted": [label(s) for s in newly_dead],
               "playing_again": [label(s) for s in recovered],
               "still_demoted": [label(s) for s in sorted(now_dead - set(newly_dead))],
               "failed_once_held": [label(s) for s in held],
               "judged_from": mine,
               "what_to_do": "nothing if expected; a newly demoted server that "
                             "plays for you belongs in data/provider_overrides.json"},
              level="error" if newly_dead else "info",
              check="provider-rank")

    rank.pop("names", None)          # never published; see the load above
    body = json.dumps(rank, separators=(",", ":")).encode()
    s3.put_object(Bucket=bucket, Key=KEY, Body=body,
                  ContentType="application/json; charset=utf-8",
                  CacheControl="public, max-age=600")
    head = s3.head_object(Bucket=bucket, Key=KEY)
    ok = head["ContentLength"] == len(body)
    print(f"{'published' if ok else 'SIZE MISMATCH'} {KEY}: {len(body)} bytes, "
          f"{len(rank['dead'])} dead slot(s) of {len(rank['score'])}, "
          f"v={rank.get('v')}, vantage={mine}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
