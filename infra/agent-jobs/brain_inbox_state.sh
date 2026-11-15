#!/bin/sh
# Monitor script for the nightly consolidation: prints a stable fingerprint of the inbox.
# Unchanged output since the last run means there is nothing new, so no model call is made.
cd /opt/data/brain/inbox 2>/dev/null || exit 0
for f in $(ls *.md 2>/dev/null | sort); do printf '%s %s\n' "$(cksum < "$f" | cut -d' ' -f1)" "$f"; done
