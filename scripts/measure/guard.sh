#!/usr/bin/env bash
# Run one check. If it fails without having delivered an alert, deliver one.
#
#   bash scripts/measure/guard.sh <check-name> <command...>
#
# Every watch mails when it finds something. None of them can mail when it
# crashes: an ImportError, a missing secret read before notify is imported, a
# runner killed mid-step. The workflow's catch-all fires only when NO step
# mailed, so on a morning when the nightly watchdog has already mailed, a
# crashed provider check would go unreported. This decides per step, by
# counting the sent-mail log before and after, exactly as daily.sh's watch()
# does, and passes the command's own exit code through unchanged.
set -uo pipefail
name="$1"; shift
log="${ALERTS_SENT:-alerts.sent}"
touch "$log"
before=$(wc -l < "$log" | tr -d ' ')
"$@"
code=$?
after=$(wc -l < "$log" | tr -d ' ')
if [ "$code" != 0 ] && { [ "$code" = 3 ] || [ "$after" -le "$before" ]; }; then
  echo "  guard: $name exited $code and delivered no alert; sending one"
  python scripts/measure/notify.py --check "$name" \
    "exited $code without delivering an alert; open the run for the traceback" || true
fi
exit "$code"
