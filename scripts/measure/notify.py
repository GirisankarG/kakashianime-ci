"""Send one alert and say whether it arrived.

sentry_sdk.capture_message() returns an event id whether or not Sentry took
the event. On 2026-09-16 the org's quota was exhausted, ingest answered 429,
and every watch in this directory printed "reported to Sentry" while nothing
reached anyone. A monitor whose alert step fails silently is a monitor that
does not exist, so this posts the envelope itself and returns the HTTP status.

    from notify import alert
    ok = alert("3 providers dead", {"providers": {...}})   # True only on 200

Callers treat False as a failure of the run, not as a shrug: the exit code is
what a scheduler sees, and the log line names the status that came back.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid


def alert(message: str, context: dict | None = None, level: str = "error") -> bool:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        print("  notify: SENTRY_DSN not set, nobody will hear about this", file=sys.stderr)
        return False
    m = re.match(r"https://([^@]+)@([^/]+)/(\d+)", dsn)
    if not m:
        print("  notify: SENTRY_DSN is not a DSN", file=sys.stderr)
        return False
    key, host, project = m.groups()
    eid = uuid.uuid4().hex
    event = {"event_id": eid, "message": message, "level": level,
             "platform": "python", "timestamp": time.time(),
             "tags": {"site": "kakashianime"},
             "contexts": {"detail": context or {}}}
    body = (json.dumps({"event_id": eid}) + "\n"
            + json.dumps({"type": "event"}) + "\n"
            + json.dumps(event) + "\n").encode()
    req = urllib.request.Request(
        f"https://{host}/api/{project}/envelope/", data=body,
        headers={"Content-Type": "application/x-sentry-envelope",
                 "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_key={key}, "
                                  f"sentry_client=kakashianime-watch/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
            print(f"  notify: Sentry {r.status}{'' if ok else ' (NOT delivered)'}")
            return ok
    except urllib.error.HTTPError as ex:
        detail = ex.read()[:160].decode(errors="replace")
        print(f"  notify: Sentry HTTP {ex.code}, NOT delivered: {detail}", file=sys.stderr)
        return False
    except Exception as ex:
        print(f"  notify: {type(ex).__name__}, NOT delivered", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(0 if alert(" ".join(sys.argv[1:]) or "notify.py self-test", level="info") else 1)
