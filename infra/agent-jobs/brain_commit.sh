#!/bin/sh
# Hourly, no model involved: commit whatever changed in the brain. Silent unless git fails.
cd /opt/data/brain || { echo "brain volume missing"; exit 0; }
git add -A >/dev/null 2>&1
if ! git diff --cached --quiet; then
  n=$(git diff --cached --name-only | wc -l)
  git -c user.name=agent -c user.email=agent@logos.local \
    commit -q -m "Brain update: $n file(s), $(date -u +%Y-%m-%dT%H:%MZ)" || echo "brain commit failed"
fi
