#!/bin/bash
# macondo_check.sh — track Macondo review status for project 906 / ship 7520
#
# Modes:
#   daily      : 2 requests, instant. Detects a status flip + decision message.
#   queue      : ~1500 requests over ~10 min. Approximates queue size, your
#                rank, the review front, and 7-day reviewer throughput.
#                Appends a line to ~/.macondo_tracker/history.csv each run.
#   panel A B  : scan project IDs A..B only (debug / quick peek).
#
# Schedule (crontab -e):
#   0 9 * * *  ~/Desktop/mcondo/macondo_check.sh daily
#   0 10 * * 6 ~/Desktop/mcondo/macondo_check.sh queue
#
# Notes: stays under the API rate limit (20 req/5s) via 0.3s spacing.
# 404s are normal (deleted projects). The panel misses ships on old
# projects, so queue_n undercounts by roughly 10-15%.

set -u
BASE="https://macondo.hackclub.com/api"
PID=906
MYSHIP=7520
CURL=/usr/bin/curl
SLEEP=/bin/sleep
JQ=jq
OUT="$HOME/.macondo_tracker"
mkdir -p "$OUT"

if [ "${1:-daily}" = "daily" ]; then
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  proj=$($CURL -s "$BASE/projects/$PID")
  status=$(echo "$proj" | $JQ -r '.activeShip.status // "unknown"')
  hours=$(echo "$proj" | $JQ -r '.public_total_hours // "?"')
  ship=$($CURL -s "$BASE/projects/$PID/ships" | $JQ -c '.[0]')
  reviewed=$(echo "$ship" | $JQ -r '.reviewed_at // "null"')
  echo "$ts status=$status hours=$hours reviewed_at=$reviewed" | tee -a "$OUT/daily.log"
  if [ "$reviewed" != "null" ]; then
    echo "*** DECISION REACHED ***"
    echo "$ship" | $JQ '{status, reviewed_at, fruitRewarded, decision_user_message}'
  fi
  exit 0
fi

# ---- queue mode: choose scan window ----
if [ "${1:-}" = "panel" ]; then
  lo=$2; hi=$3
else
  # Climb 500-ID rungs until 3 consecutive rungs are dead -> live frontier.
  hi=15000; cand=15500
  while :; do
    code=$($CURL -s -o /dev/null -w "%{http_code}" "$BASE/projects/$cand/ships")
    [ "$code" = "200" ] && hi=$cand
    [ $((cand - hi)) -ge 1500 ] && break
    cand=$((cand + 500))
  done
  lo=$((hi - 1500)); [ "$lo" -lt 15000 ] && lo=15000
  echo "scanning projects $lo..$hi" >&2
fi

nd="$OUT/panel_$(date +%Y%m%d).ndjson"
: > "$nd"
pid=$lo
while [ "$pid" -le "$hi" ]; do
  resp=$($CURL -s "$BASE/projects/$pid/ships")
  case "$resp" in
    ""|"[]"|*error*) : ;;
    *) echo "$resp" | $JQ -c --argjson pid "$pid" \
         'map({pid:$pid,id,status,created_at,reviewed_at}) | .[]' >> "$nd" 2>/dev/null ;;
  esac
  $SLEEP 0.3
  pid=$((pid + 1))
done

# Always include our own project's ships (906 < scan window start).
$CURL -s "$BASE/projects/$PID/ships" \
  | $JQ -c --argjson pid "$PID" \
      'map({pid:$pid,id,status,created_at,reviewed_at}) | .[]' >> "$nd" 2>/dev/null

now=$(date -u +%s)

# Summary for humans
$JQ -s --argjson now "$now" --argjson my "$MYSHIP" '
  def d: sub("\\.[0-9]+";"") | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime;
  ([.[] | select(.status=="under_review")] | sort_by(.created_at) | to_entries) as $q
  | {
      statuses: (group_by(.status) | map({key: .[0].status, value: length}) | from_entries),
      queue_n: ($q | length),
      front_date: ($q | map(.value.created_at) | min | .[0:10]),
      oldest_wait_days: ($q | map(.value.created_at|d) | min | (($now - .)/86400) | floor),
      mine: ([$q[] | select(.value.id==$my)]
             | if length==0 then {in_queue: false}
               else .[0] | {in_queue: true, rank_by_age: (.key+1),
                     age_days: (($now - (.value.created_at|d))/86400 | floor)} end),
      decisions_last_7d: ([.[] | select(.reviewed_at != null
                              and (($now - (.reviewed_at|d)) < 604800))] | length)
    }' "$nd" | tee "$OUT/queue_$(date +%Y%m%d).json"

# One CSV line: date,queue_n,front_date,rank,decisions_7d
$JQ -s --argjson now "$now" --argjson my "$MYSHIP" '
  def d: sub("\\.[0-9]+";"") | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime;
  ([.[] | select(.status=="under_review")] | sort_by(.created_at) | to_entries) as $q
  | [ ($now | strftime("%Y-%m-%d")),
      ($q|length),
      ($q | map(.value.created_at) | min | .[0:10]),
      ([$q[] | select(.value.id==$my)] | if length==0 then "-" else .[0].key+1 end),
      ([.[] | select(.reviewed_at != null and (($now - (.reviewed_at|d)) < 604800))] | length) ]
  | @csv' "$nd" >> "$OUT/history.csv"
echo "history: $OUT/history.csv"
