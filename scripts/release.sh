#!/bin/sh
set -eu

usage() {
  printf 'Usage: %s [vMAJOR.MINOR.PATCH]\n' "$0" >&2
  printf 'With no argument, the version is read from the project manifests.\n' >&2
}

fail() {
  printf 'Release stopped: %s\n' "$1" >&2
  exit 1
}

if [ "$#" -gt 1 ]; then
  usage
  exit 2
fi

REQUESTED_TAG="${1:-}"

SCRIPT_DIR="$(CDPATH= cd -P "$(dirname "$0")" && pwd)"
REPOSITORY_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPOSITORY_ROOT"

command -v git >/dev/null 2>&1 || fail "git is required"
command -v pixi >/dev/null 2>&1 || fail "Pixi is required (install it from https://pixi.sh)"

CURRENT_BRANCH="$(git branch --show-current)"
[ "$CURRENT_BRANCH" = "main" ] || fail "switch to the main branch before releasing"
[ -z "$(git status --porcelain)" ] || fail "commit or stash all working-tree changes first"

printf 'Installing the locked environment...\n'
pixi install --locked

PACKAGE_VERSION="$(pixi run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
WORKSPACE_VERSION="$(pixi run python -c 'import tomllib; print(tomllib.load(open("pixi.toml", "rb"))["workspace"]["version"])')"
[ "$PACKAGE_VERSION" = "$WORKSPACE_VERSION" ] || fail "pyproject.toml is version $PACKAGE_VERSION but pixi.toml is version $WORKSPACE_VERSION"

if [ -n "$REQUESTED_TAG" ]; then
  RELEASE_TAG="$REQUESTED_TAG"
else
  RELEASE_TAG="v$PACKAGE_VERSION"
fi
if ! printf '%s\n' "$RELEASE_TAG" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'; then
  usage
  fail "the release version must use the form MAJOR.MINOR.PATCH"
fi

EXPECTED_VERSION="${RELEASE_TAG#v}"
[ "$PACKAGE_VERSION" = "$EXPECTED_VERSION" ] || fail "pyproject.toml is version $PACKAGE_VERSION, not $EXPECTED_VERSION"

if git rev-parse --quiet --verify "refs/tags/$RELEASE_TAG" >/dev/null; then
  fail "tag $RELEASE_TAG already exists locally"
fi

printf 'Running release checks...\n'
pixi run test
pixi run verify-assets
[ -z "$(git status --porcelain)" ] || fail "release checks changed the working tree; review and commit those changes"

printf 'Pushing main...\n'
git push origin main

printf 'Creating and pushing %s...\n' "$RELEASE_TAG"
git tag -a "$RELEASE_TAG" -m "Watson $RELEASE_TAG"
if ! git push origin "$RELEASE_TAG"; then
  fail "the tag was created locally but could not be pushed; resolve the Git error and run: git push origin $RELEASE_TAG"
fi

printf '\nRelease started. Follow it at:\n'
printf 'https://github.com/shaheedazaad/watson/actions/workflows/release.yml\n'
