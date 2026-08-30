#!/usr/bin/env bash
set -euo pipefail

# Deliberately pinned for reproducible SVG output.
MERMAID_CLI_VERSION="11.12.0"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAM_DIR="${ROOT_DIR}/docs/images"

for source in "${DIAGRAM_DIR}"/*.mmd; do
  output="${source%.mmd}.svg"
  npx --yes "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}" \
    --input "${source}" \
    --output "${output}" \
    --backgroundColor white
done
