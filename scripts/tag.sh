#!/usr/bin/env bash
# Quick release: bump pyproject.toml version, commit, tag, push.
#
# Usage:
#   ./scripts/tag.sh patch    # 0.6.9 → 0.6.10
#   ./scripts/tag.sh minor    # 0.6.9 → 0.7.0
#   ./scripts/tag.sh major    # 0.6.9 → 1.0.0
#   ./scripts/tag.sh 0.7.0    # explicit version
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PART="${1:-patch}"
PYPROJECT="pyproject.toml"

# Read current version from pyproject.toml
CURRENT=$(grep -m1 '^version' "$PYPROJECT" | sed 's/version = "\(.*\)"/\1/')
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"

# Determine new version
case "$PART" in
  patch) NEW="$MAJOR.$MINOR.$((PATCH + 1))" ;;
  minor) NEW="$MAJOR.$((MINOR + 1)).0" ;;
  major) NEW="$((MAJOR + 1)).0.0" ;;
  [0-9]*) NEW="$PART" ;;  # explicit version
  *) echo "Usage: $0 {patch|minor|major|X.Y.Z}"; exit 1 ;;
esac

TAG="v$NEW"

# Check tag doesn't already exist
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Error: tag $TAG already exists"
  exit 1
fi

echo "$CURRENT → $NEW ($TAG)"

# Bump pyproject.toml
sed -i "s/^version = \".*\"/version = \"$NEW\"/" "$PYPROJECT"

# Also bump logos_cli/__init__.py if it has __version__
INIT="logos_cli/__init__.py"
if [ -f "$INIT" ] && grep -q '__version__' "$INIT"; then
  sed -i "s/__version__ = \".*\"/__version__ = \"$NEW\"/" "$INIT"
  git add "$INIT"
fi

git add "$PYPROJECT"
git commit -m "chore: bump version to $NEW"
git tag "$TAG"
git push origin main "$TAG"

echo "Released $TAG"
