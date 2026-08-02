#!/bin/sh
set -eu

RELEASE_URL="${WATSON_RELEASE_URL:-https://github.com/shaheedazaad/watson/releases/latest/download/watson-source.tar.gz}"
if [ "$(uname -s)" = "Darwin" ]; then
  INSTALL_ROOT="$HOME/Library/Application Support/Watson/app"
else
  INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/watson-app"
fi

if ! command -v pixi >/dev/null 2>&1; then
  curl -fsSL https://pixi.sh/install.sh | sh
  export PATH="$HOME/.pixi/bin:$PATH"
fi

STAGING_ROOT="$(mktemp -d)"
trap 'rm -rf "$STAGING_ROOT"' EXIT
curl -fsSL "$RELEASE_URL" -o "$STAGING_ROOT/watson.tar.gz"
mkdir -p "$STAGING_ROOT/source"
tar -xzf "$STAGING_ROOT/watson.tar.gz" --strip-components=1 -C "$STAGING_ROOT/source"
INSTALL_PARENT="$(dirname "$INSTALL_ROOT")"
PREVIOUS_ROOT="$STAGING_ROOT/previous"
mkdir -p "$INSTALL_PARENT"
if [ -d "$INSTALL_ROOT" ]; then
  mv "$INSTALL_ROOT" "$PREVIOUS_ROOT"
fi
if ! mv "$STAGING_ROOT/source" "$INSTALL_ROOT"; then
  if [ -d "$PREVIOUS_ROOT" ]; then mv "$PREVIOUS_ROOT" "$INSTALL_ROOT"; fi
  exit 1
fi

if ! pixi install --manifest-path "$INSTALL_ROOT/pixi.toml" --locked; then
  rm -rf "$INSTALL_ROOT"
  if [ -d "$PREVIOUS_ROOT" ]; then mv "$PREVIOUS_ROOT" "$INSTALL_ROOT"; fi
  exit 1
fi
mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_ROOT/scripts/watson-launcher" "$HOME/.local/bin/watson"
printf 'Watson installed. Run: watson\n'
