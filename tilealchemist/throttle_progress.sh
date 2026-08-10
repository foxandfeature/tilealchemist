#!/usr/bin/env bash
# Passes stdin to stdout, throttling lines that match REGEX to at most one
# per INTERVAL seconds. Built for progress lines a noisy tool rewrites in
# place with \r (convert those to \n before piping in here, e.g. with
# `stdbuf -oL tr '\r' '\n'`; see the comment at its call site for why
# stdbuf matters). Only the most recently seen matching line is kept and
# printed once its turn comes up, not every one in between.
#
# A pending line is flushed (printed) in three cases, so nothing matching
# is ever silently dropped: once INTERVAL seconds pass with no new input at
# all (so a stalled source still shows its last known state), right before
# a non-matching line is passed through, and at EOF.
#
# Usage: some_noisy_command | throttle_progress.sh <interval_seconds> <regex>
#
# No `set -e`: `read -t`'s non-zero exit on timeout/EOF is an expected,
# handled outcome here, not an error to abort on.
set -u

interval="$1"
regex="$2"

last_print=0
pending=""

flush() {
  [[ -n "$pending" ]] && echo "$pending"
  last_print=$(date +%s)
  pending=""
}

while true; do
  IFS= read -r -t "$interval" line
  status=$?

  # A matching line only prints once its turn comes up (checked below).
  # Every other outcome, meaning a non-matching line, a timeout, or EOF,
  # flushes unconditionally, since none of those are "still within the
  # throttle window" for whatever's pending.
  if (( status == 0 )) && [[ "$line" =~ $regex ]]; then
    pending="$line"
    (( $(date +%s) - last_print >= interval )) && flush
    continue
  fi

  flush
  (( status == 0 )) && echo "$line"
  (( status == 0 || status > 128 )) || break # real failure/EOF, not a timeout
done
