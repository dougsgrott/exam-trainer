#!/usr/bin/env bash
# Delete WSL ":Zone.Identifier" alternate-data-stream files.
#
# Usage:
#   ./clean-zone-identifiers.sh [-n|--dry-run] [path]
#
#   path  directory to clean (default: the script's own directory)

set -euo pipefail

dry_run=0
target=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) dry_run=1; shift ;;
    -h|--help)
      sed -n '2,8p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  target="$1"; shift ;;
  esac
done

if [[ -z "$target" ]]; then
  target="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ ! -d "$target" ]]; then
  echo "not a directory: $target" >&2
  exit 1
fi

count=$(find "$target" -type f -name '*:Zone.Identifier' -printf '.' | wc -c)

if [[ "$count" -eq 0 ]]; then
  echo "No :Zone.Identifier files found under $target"
  exit 0
fi

if [[ "$dry_run" -eq 1 ]]; then
  find "$target" -type f -name '*:Zone.Identifier' -print
  echo "[dry-run] would delete $count file(s) under $target"
  exit 0
fi

find "$target" -type f -name '*:Zone.Identifier' -delete
echo "Deleted $count :Zone.Identifier file(s) under $target"
