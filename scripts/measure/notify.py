"""Send one alert to a person, and say whether it arrived.

Two channels, and only one of them counts as delivered.

MAIL is the one a person reads. Between 18 and 24 September every watch in
this directory fired correctly: the CI dead-man switch said "no residential
run has cleared the unverified providers" six days running, the airing check
said ONE PIECE 1179 had aired and its page was a 404, and Sentry answered 200
to every one of them. Nobody acted, because nobody reads Sentry. An alert that
lands somewhere nobody looks is not an alert, so this returns True only when
the mail was accepted.

SENTRY stays as the searchable record and as the place the context dict is
kept whole. It is posted as a raw envelope, not through the SDK, because
capture_message() returns an event id on a 429 too: on 2026-09-16 the quota
was exhausted and every watch printed "reported to Sentry" while nothing
reached anyone.

    from notify import alert
    ok = alert("3 provider(s) play nothing: a, b, c", {"providers": {...}})

Subject is "[KakashiAnime] <check>: <message>", so an inbox that receives
several sites' alerts sorts by product and says what broke without opening
the mail. <check> is the calling script's name unless the caller passes one.

Mail needs RESEND_API_KEY, ALERT_EMAIL and EMAIL_FROM. A missing one is a
failure, printed loudly, never a silent no-op: the run's exit code then says
"broken, and nobody was told", which is the worse of the two outcomes and is
meant to read differently.

Resend sits behind Cloudflare and answers 403 to urllib's default User-Agent.
Three retries with backoff, because one 5xx from a mail API must not turn a
real finding into silence.

    python scripts/measure/notify.py --test     # one mail to ALERT_EMAIL
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
from pathlib import Path

PRODUCT = "KakashiAnime"
UA = "kakashianime-watch/1 (+https://kakashianime.me)"
# Every mail that was accepted is appended here, one subject per line. The
# workflow's final step mails "the run failed and no check said why" only when
# this file is empty, so a crash before any alert() still reaches a person and
# a check that already mailed does not get a second, vaguer mail behind it.
SENT_LOG = Path(os.environ.get("ALERTS_SENT", "alerts.sent"))


def _check_name(check: str | None) -> str:
    if check:
        return check
    stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    return stem.replace("_", "-") or "watch"


def _render(context: dict | None) -> str:
    """Context as lines a person can read on a phone, never a JSON blob."""
    if not context:
        return ""
    out = []
    for k, v in context.items():
        if isinstance(v, dict):
            out.append(f"{k}:")
            out += [f"  {kk}: {vv}" for kk, vv in v.items()]
        elif isinstance(v, (list, tuple)):
            out.append(f"{k}:")
            out += [f"  - {x}" for x in v]
        else:
            out.append(f"{k}: {v}")
    return "\n".join(out)


def _mail(subject: str, text: str) -> bool:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("ALERT_EMAIL", "").strip()
    sender = os.environ.get("EMAIL_FROM", "").strip()
    missing = [n for n, v in (("RESEND_API_KEY", key), ("ALERT_EMAIL", to),
                              ("EMAIL_FROM", sender)) if not v]
    if missing:
        print(f"  notify: mail NOT sent, {', '.join(missing)} not set; "
              f"nobody reads Sentry, so nobody will hear about this", file=sys.stderr)
        return False
    body = json.dumps({"from": sender, "to": [to], "subject": subject,
                       "text": text}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status in (200, 201):
                    # A subject can carry provider names, and an Actions log in
                    # the public CI repo is world-readable. Name the check only.
                    if os.environ.get("GITHUB_ACTIONS") == "true":
                        print("  notify: mail accepted")
                    else:
                        print(f"  notify: mail accepted, subject: {subject}")
                    try:
                        with SENT_LOG.open("a") as f:
                            f.write(subject + "\n")
                    except OSError:
                        pass
                    return True
                print(f"  notify: mail HTTP {r.status}, NOT delivered", file=sys.stderr)
                return False
        except urllib.error.HTTPError as ex:
            detail = ex.read()[:200].decode(errors="replace")
            # 4xx is our fault and will not change on retry: bad key, an
            # unverified sender domain, a rejected address. Say which.
            if 400 <= ex.code < 500 and ex.code != 429:
                print(f"  notify: mail HTTP {ex.code}, NOT delivered: {detail}",
                      file=sys.stderr)
                return False
            print(f"  notify: mail HTTP {ex.code} (attempt {attempt + 1}/3): {detail}",
                  file=sys.stderr)
        except Exception as ex:
            print(f"  notify: mail {type(ex).__name__} (attempt {attempt + 1}/3)",
                  file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    print("  notify: mail NOT delivered after 3 attempts", file=sys.stderr)
    return False


def _sentry(message: str, context: dict | None, level: str) -> bool:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        print("  notify: SENTRY_DSN not set, no record kept", file=sys.stderr)
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
            print(f"  notify: Sentry {r.status}{'' if ok else ' (not recorded)'}")
            return ok
    except urllib.error.HTTPError as ex:
        detail = ex.read()[:160].decode(errors="replace")
        print(f"  notify: Sentry HTTP {ex.code}, not recorded: {detail}", file=sys.stderr)
        return False
    except Exception as ex:
        print(f"  notify: Sentry {type(ex).__name__}, not recorded", file=sys.stderr)
        return False


def alert(message: str, context: dict | None = None, level: str = "error",
          check: str | None = None) -> bool:
    """True only if a person will see this, which means the mail was accepted.

    Sentry is posted too, as the record, and its result does not change the
    return value: six days of Sentry 200s reached nobody.
    """
    # Older callers prefixed the product themselves. The subject carries it
    # once, in brackets, so strip a leading "KakashiAnime: " rather than
    # print it twice.
    message = re.sub(rf"^{PRODUCT}:\s*", "", message.strip())
    name = _check_name(check)
    subject = f"[{PRODUCT}] {name}: {message.splitlines()[0][:140]}"
    text = (f"{message}\n\n{_render(context)}\n\n"
            f"check: {name}\nlevel: {level}\n"
            f"host: {os.environ.get('GITHUB_REPOSITORY') or 'laptop'}"
            + (f"\nrun: {os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
               if os.environ.get("GITHUB_RUN_ID") else ""))
    _sentry(f"{PRODUCT}: {message}", context, level)
    return _mail(subject, text)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="send one alert from the shell")
    ap.add_argument("message", nargs="*")
    ap.add_argument("--check", default="ci-run",
                    help="the name in the subject; say which check spoke")
    ap.add_argument("--test", action="store_true",
                    help="send a harmless mail to prove the path end to end")
    a = ap.parse_args()
    msg = " ".join(a.message) or ("mail path self-test, nothing is wrong" if a.test
                                  else "a scheduled run failed; open the run")
    ok = alert(msg, {"sent_from": os.environ.get("GITHUB_REPOSITORY") or os.uname().nodename},
               level="info" if a.test else "error",
               check="self-test" if a.test else a.check)
    sys.exit(0 if ok else 1)
