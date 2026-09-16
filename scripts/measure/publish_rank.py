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


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    try:
        rank = json.loads(src.read_text())
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
    if (current.get("v") == rank.get("v")
            and current.get("vantage") == "residential" and mine != "residential"):
        print(f"keeping today's residential verdict; not overwriting it from {mine}")
        return 0

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
