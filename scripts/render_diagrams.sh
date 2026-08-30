#!/usr/bin/env bash
set -euo pipefail

# Renderer contract: Node 24.x and the exact Mermaid CLI/config below.
NODE_EXPECTED_MAJOR="24"
NODE_VERSION_EXPECTATION="24.x"
MERMAID_CLI_VERSION="11.12.0"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAM_DIR="${ROOT_DIR}/docs/images"
CONFIG_FILE="${ROOT_DIR}/scripts/mermaid-config.json"

node_version="$(node --version)"
node_major="${node_version#v}"
node_major="${node_major%%.*}"
if [[ "${node_major}" != "${NODE_EXPECTED_MAJOR}" ]]; then
  echo "Expected Node ${NODE_VERSION_EXPECTATION}; found ${node_version}." >&2
  exit 1
fi

render_into() {
  local output_dir="$1"
  mkdir -p "${output_dir}"
  local source output
  for source in "${DIAGRAM_DIR}"/*.mmd; do
    output="${output_dir}/$(basename "${source%.mmd}").svg"
    npx --yes "@mermaid-js/mermaid-cli@${MERMAID_CLI_VERSION}" \
      --configFile "${CONFIG_FILE}" \
      --input "${source}" \
      --output "${output}" \
      --backgroundColor white
  done
}

if [[ "${1:-}" == "--verify" ]]; then
  first_dir="$(mktemp -d /tmp/recallops-mermaid-first.XXXXXX)"
  second_dir="$(mktemp -d /tmp/recallops-mermaid-second.XXXXXX)"
  trap 'rm -rf "${first_dir}" "${second_dir}"' EXIT
  render_into "${first_dir}"
  render_into "${second_dir}"
  for first_svg in "${first_dir}"/*.svg; do
    basename_svg="$(basename "${first_svg}")"
    cmp -s "${first_svg}" "${second_dir}/${basename_svg}"
    cmp -s "${first_svg}" "${DIAGRAM_DIR}/${basename_svg}"
    shasum -a 256 "${first_svg}"
  done
  echo "Stable double-render verified for $(find "${first_dir}" -maxdepth 1 -name '*.svg' | wc -l | tr -d ' ') diagrams (Node ${node_version}, Mermaid CLI ${MERMAID_CLI_VERSION})."
  echo "Committed SVGs match fresh render."
  exit 0
fi

render_into "${DIAGRAM_DIR}"
echo "Rendered $(find "${DIAGRAM_DIR}" -maxdepth 1 -name '*.mmd' | wc -l | tr -d ' ') diagrams (Node ${node_version}, Mermaid CLI ${MERMAID_CLI_VERSION})."
