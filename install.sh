#!/bin/sh
# Install or upgrade dockctl on macOS:  curl -LsSf https://raw.githubusercontent.com/rahimnathwani/dockctl/main/install.sh | sh
# Installs uv if missing, installs dockctl as a uv tool, then runs the guided `dockctl setup`.
# Extra arguments go to `dockctl setup` (e.g. `sh -s -- --yes`).
set -eu

SOURCE="https://github.com/rahimnathwani/dockctl/archive/refs/heads/main.tar.gz"  # a tarball, so git isn't needed

if [ "$(uname -s)" != "Darwin" ]; then
    echo "dockctl only supports macOS." >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv (Python package manager, https://docs.astral.sh/uv/)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

echo "==> Installing dockctl"
uv tool install --force --reinstall "dockctl @ $SOURCE"
uv tool update-shell >/dev/null 2>&1 || true  # adds ~/.local/bin to PATH for new shells

if ! xcode-select -p >/dev/null 2>&1; then
    echo "note: Xcode Command Line Tools aren't installed. dockctl works without them, but keys that type"
    echo "      text or press hotkeys are safer with them: run 'xcode-select --install', then 'dockctl setup'."
fi

echo "==> Setting up"
if [ -r /dev/tty ] && [ -t 1 ]; then
    dockctl setup "$@" </dev/tty  # under `curl | sh` stdin is the script, so ask questions on the terminal
else
    dockctl setup "$@"
fi
