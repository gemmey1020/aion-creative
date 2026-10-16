#!/usr/bin/env bash
set -euo pipefail

for dependency in uv curl tar; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "$dependency" >&2
    exit 1
  fi
done

runtime_root="$HOME/.local/share/ion-creative"
space_probe="$runtime_root"
while [[ ! -e "$space_probe" ]]; do
  parent="$(dirname "$space_probe")"
  if [[ "$parent" == "$space_probe" ]]; then
    printf 'Could not find an existing parent for %s\n' "$runtime_root" >&2
    exit 1
  fi
  space_probe="$parent"
done

available_kib="$(df -Pk "$space_probe" | awk 'NR == 2 {print $4}')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || (( available_kib < 4194304 )); then
  printf 'At least 4 GiB must be free on the runtime filesystem (available: %s KiB)\n' \
    "${available_kib:-unknown}" >&2
  exit 1
fi

uv run ion runtime install --all
uv run ion doctor
