#!/usr/bin/env bash
# Run the working-tree repomap CLI against the large `services` repo, with timing.
# Usage: scripts/services.sh [repomap args...]   e.g. --map-tokens 4096, --chat-files path
# Override the target with SERVICES_ROOT=/path.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="${SERVICES_ROOT:-$HOME/dev/ls/services}"

[ -d "$root" ] || { echo "services repo not found at $root (set SERVICES_ROOT)" >&2; exit 1; }

time uv run --project "$here" repomap "$root" --root "$root" "$@"
