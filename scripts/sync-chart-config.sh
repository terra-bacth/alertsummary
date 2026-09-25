#!/usr/bin/env bash
# Copy the operator-owned config files from config/ into the Helm chart.
#
# Helm can only package files that live inside the chart directory, so the chart carries copies
# of settings/llm/clusters (the defaults its values override), the allow-list, runbooks, PromQL
# library and metric catalogues. config/ is the source of truth; run this after editing any of
# them. tests/test_config.py fails when the copies drift.
#
# Only the files named below are copied — never keys or anything else that lands in config/.
#
#   scripts/sync-chart-config.sh          # sync
#   scripts/sync-chart-config.sh --check  # exit 1 if anything is out of date (CI)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/config"
dst="$root/deploy/helm/tsagent/files"
check=false
[[ "${1:-}" == "--check" ]] && check=true

sync() {  # sync <source-file> <dest-file>
  if $check; then
    if ! cmp -s "$1" "$2" 2>/dev/null; then echo "out of date: ${2#"$root"/}"; drift=1; fi
  else
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
  fi
}

drift=0
for f in settings.yaml llm.yaml clusters.yaml; do sync "$src/$f" "$dst/config/$f"; done
sync "$src/ssh_allowlist.yaml" "$dst/ssh_allowlist.yaml"
sync "$src/promql_library.yaml" "$dst/promql_library.yaml"
for f in "$src"/runbooks/*.yaml; do sync "$f" "$dst/runbooks/$(basename "$f")"; done
shopt -s nullglob
for f in "$src"/metrics/*.txt; do sync "$f" "$dst/metrics/$(basename "$f")"; done

if $check; then
  [[ $drift -eq 0 ]] && echo "chart files are in sync with config/"
  exit $drift
fi
echo "synced config/ -> deploy/helm/tsagent/files/"
