"""Send one alert to a person, and say whether it arrived.

Two channels, and only one of them counts as delivered.

MAIL is the one a person reads. Between 18 and 24 September every watch in
this directory fired correctly: the CI dead-man switch said "no residential
run has cleared the unverified providers" six days running, the airing check
said ONE PIECE 1179 had aired and its page was a 404, and Sentry answered 200
to every one of them. Nobody acted, because nobody reads Sentry. An alert that
lands somewhere nobody looks is not an alert, so this returns True only when
the mail was accepted.

THE RECORD is alerts.jsonl beside ALERT_STATE (logs/ on the Dell): every
alert, held or mailed, one JSON line with its context dict whole, for grep and
jq. It replaced a Sentry post on 2026-11-17, when Arun moved every site off
Sentry. Sentry had only ever held this record here (the site has no Worker
and no browser SDK), and on 2026-09-16 its quota ran out while every watch
printed "reported to Sentry".

    from notify import alert
    ok = alert("3 provider(s) play nothing: a, b, c", {"providers": {...}})

Subject is "[<product>] <check>: <message>", so an inbox that receives
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
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The product name and site come from the environment, not from this file,
# because this is the one script the PUBLIC CI repository keeps: if the
# private checkout fails, the step that says so must not live inside it. A
# file published there that named the site would tie that account to it.
# Locally they come from .env; in CI from the NOTIFY_* secrets, which GitHub
# also masks wherever a log prints them.
PRODUCT = os.environ.get("NOTIFY_PRODUCT", "").strip() or "site"
_SITE = os.environ.get("NOTIFY_SITE", "").strip()
UA = f"site-watch/1 (+{_SITE})" if _SITE else "site-watch/1"
# Every mail that was accepted is appended here, one subject per line. The
# workflow's final step mails "the run failed and no check said why" only when
# this file is empty, so a crash before any alert() still reaches a person and
# a check that already mailed does not get a second, vaguer mail behind it.
SENT_LOG = Path(os.environ.get("ALERTS_SENT", "alerts.sent"))


def _log_sent(line: str) -> None:
    try:
        with SENT_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


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
              f"the record alone reaches nobody, so nobody will hear about this", file=sys.stderr)
        return False
    body = json.dumps({"from": sender, "to": [to], "subject": subject,
                       "text": text}).encode()
    # RESEND_URL only so tests/test_alert_repeat.py can stand in for the API.
    req = urllib.request.Request(
        os.environ.get("RESEND_URL", "https://api.resend.com/emails"), data=body, method="POST",
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
                    _log_sent(subject)
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


def _record(message: str, context: dict | None, level: str, check: str) -> None:
    path = STATE.parent / "alerts.jsonl"
    line = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "product": PRODUCT, "check": check, "level": level,
            "message": message, "context": context or {}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except OSError as ex:
        print(f"  notify: record not kept in {path}: {ex}", file=sys.stderr)


# Findings already mailed, so a standing problem is not mailed every morning.
# A peer session named this as the top noise risk on 2026-09-27, with the
# evidence: the site had been 403 since the 26th and would have mailed daily
# until someone re-enabled R2, which is how the channel went unread in the
# first place. In CI this file is carried between runs by actions/cache; it is
# deliberately NOT kept in R2, because the outage that most needs suppressing
# is R2 being down, and a store that shares fate with the fault is no store.
STATE = Path(os.environ.get("ALERT_STATE", "logs/alert-state.json"))
# A standing problem is said again this often, so it is never forgotten.
ESCALATE_DAYS = 7


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        # No state is "nothing has been mailed yet", which errs toward
        # mailing. Erring the other way would silence a real first alert.
        return {}


def _save(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    except OSError as ex:
        print(f"  notify: could not record alert state ({type(ex).__name__}); "
              f"this finding will mail again next run", file=sys.stderr)


def _fingerprint(check: str, message: str) -> str:
    """What makes two findings 'the same problem' across runs.

    Digits are removed before hashing, so "all 7 URLs" and "all 20 URLs" are
    one standing outage rather than two. The check name is included, so the
    same sentence from two different checks is two problems.
    """
    core = re.sub(r"\d+", "#", message)[:160]
    return hashlib.sha256(f"{check}|{core}".encode()).hexdigest()[:16]


def resolved(check: str, note: str = "") -> bool:
    """Call when a check passes. Mails once if it had an open finding.

    Without this, a cleared problem is silent, and the next occurrence looks
    like a continuation rather than a new event.
    """
    state = _load()
    open_now = {k: v for k, v in state.items() if v.get("check") == check}
    if not open_now:
        return False
    first = min(v.get("first", "") for v in open_now.values())
    # Every open finding clears below, so name them all: on 20 Oct one mail
    # closed a disk stop and a build failure but named only the disk stop.
    msgs = [v.get("message", "")[:100] for v in sorted(open_now.values(), key=lambda v: v.get("first", ""))]
    was = msgs[0] + (f" (and {len(msgs) - 1} more)" if len(msgs) > 1 else "")
    sent = _mail(f"[{PRODUCT}] {check}: RESOLVED, {was}",
                 f"This cleared on its own or was fixed.\n\n"
                 f"first reported: {first}\nwhat it was:\n" + "\n".join(f"- {m}" for m in msgs) + f"\n{note}")
    # Clear only once the mail landed. Clearing first lost the 14 Oct
    # ci-watchdog resolution to a run without the mail secrets; kept open,
    # the next pass with them sends it.
    if sent:
        for k in open_now:
            state.pop(k, None)
        _save(state)
    return sent


def alert(message: str, context: dict | None = None, level: str = "error",
          check: str | None = None, every: bool = False) -> bool:
    """True only if a person will see this, which means the mail was accepted.

    Every call is also appended to the record (alerts.jsonl), held or not;
    that does not change the return value: a record reaches nobody.

    every=True skips the 7-day hold, for a finding that means the site has
    stopped updating: a build raising NameError on every run from 3 to 4 Nov
    left the site frozen for 43 h and mailed once.
    """
    # Older callers prefixed the product themselves. The subject carries it
    # once, in brackets, so strip a leading "<product>: " rather than
    # print it twice.
    message = re.sub(rf"^{PRODUCT}:\s*", "", message.strip())
    name = _check_name(check)
    # First sentence only. A subject carrying the whole finding plus its
    # remedy is unreadable in an inbox list, and the remedy is in the body
    # two lines down.
    head = message.splitlines()[0]
    first = head.split(". ")[0]
    subject = f"[{PRODUCT}] {name}: {(first if len(first) >= 25 else head)[:110]}"
    text = (f"{message}\n\n{_render(context)}\n\n"
            f"check: {name}\nlevel: {level}\n"
            f"host: {os.environ.get('GITHUB_REPOSITORY') or 'laptop'}"
            + (f"\nrun: {os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
               if os.environ.get("GITHUB_RUN_ID") else ""))
    # The record always gets it: a record with gaps is worse than a noisy one.
    # Only the mail is rate-limited.
    _record(message, context, level, name)

    state = _load()
    fp = _fingerprint(name, message)
    now = dt.datetime.now(dt.timezone.utc)
    prior = state.get(fp)
    if prior:
        try:
            age = (now - dt.datetime.fromisoformat(prior["last_mailed"])).days
        except (KeyError, ValueError):
            age = ESCALATE_DAYS
        if age < ESCALATE_DAYS and not every:
            print(f"  notify: same finding as {prior['last_mailed'][:10]}, "
                  f"not mailing again until day {ESCALATE_DAYS} "
                  f"(seen {prior.get('count', 1) + 1} times)")
            prior["count"] = prior.get("count", 1) + 1
            prior["last_seen"] = now.isoformat(timespec="seconds")
            _save(state)
            # True: a person was told about this, on the day it started. The
            # caller's exit code means "broken and someone knows", which is
            # still true. Logged, marked held: daily.sh's watch() and CI's
            # guard.sh read "a line appeared" as "a person knows", and without
            # one the 21 Oct nightly called a held repeat undelivered, exit 3.
            _log_sent(f"(held, mailed {prior['last_mailed'][:10]}) {subject}")
            return True
        text = (f"STILL BROKEN, first seen {prior.get('first', '?')[:16]}, "
                f"mailed {prior.get('count', 1)} time(s) before.\n\n" + text)
        subject = f"[{PRODUCT}] {name}: STILL, {message.splitlines()[0][:120]}"

    ok = _mail(subject, text)
    if ok:
        state[fp] = {"check": name, "message": message.splitlines()[0][:200],
                     "first": (prior or {}).get("first", now.isoformat(timespec="seconds")),
                     "last_mailed": now.isoformat(timespec="seconds"),
                     "count": (prior or {}).get("count", 0) + 1}
        _save(state)
    return ok


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="send one alert from the shell")
    ap.add_argument("message", nargs="*")
    ap.add_argument("--check", default="ci-run",
                    help="the name in the subject; say which check spoke")
    ap.add_argument("--test", action="store_true",
                    help="send a harmless mail to prove the path end to end")
    ap.add_argument("--every", action="store_true",
                    help="mail on every call until --resolved, no 7-day hold")
    ap.add_argument("--resolved", action="store_true",
                    help="say --check is clear again; mails only if it was open")
    a = ap.parse_args()
    if a.resolved:
        resolved(a.check, " ".join(a.message))
        sys.exit(0)
    msg = " ".join(a.message) or ("mail path self-test, nothing is wrong" if a.test
                                  else "a scheduled run failed; open the run")
    ok = alert(msg, {"sent_from": os.environ.get("GITHUB_REPOSITORY") or os.uname().nodename},
               level="info" if a.test else "error",
               check="self-test" if a.test else a.check, every=a.every)
    sys.exit(0 if ok else 1)
