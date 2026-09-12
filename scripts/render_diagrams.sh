#!/usr/bin/env bash
set -euo pipefail

# Renderer contract: exact local Node/npm and Mermaid CLI/config below.
NODE_EXPECTED_VERSION="v24.15.0"
NPM_EXPECTED_VERSION="11.12.1"
MERMAID_CLI_VERSION="11.12.0"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIAGRAM_DIR="${ROOT_DIR}/docs/images"
CONFIG_FILE="${ROOT_DIR}/scripts/mermaid-config.json"
MMDC="${ROOT_DIR}/node_modules/.bin/mmdc"

node_version="$(node --version)"
if [[ "${node_version}" != "${NODE_EXPECTED_VERSION}" ]]; then
  echo "Expected Node ${NODE_EXPECTED_VERSION}; found ${node_version}." >&2
  exit 1
fi
npm_version="$(npm --version)"
if [[ "${npm_version}" != "${NPM_EXPECTED_VERSION}" ]]; then
  echo "Expected npm ${NPM_EXPECTED_VERSION}; found ${npm_version}." >&2
  exit 1
fi
if [[ ! -x "${MMDC}" ]]; then
  echo "Missing local Mermaid CLI. Run npm ci before rendering." >&2
  exit 1
fi
local_mermaid_version="$(node -p "require('${ROOT_DIR}/node_modules/@mermaid-js/mermaid-cli/package.json').version")"
if [[ "${local_mermaid_version}" != "${MERMAID_CLI_VERSION}" ]]; then
  echo "Expected local Mermaid CLI ${MERMAID_CLI_VERSION}; found ${local_mermaid_version}. Run npm ci." >&2
  exit 1
fi

render_into() {
  local output_dir="$1"
  mkdir -p "${output_dir}"
  local source output
  for source in "${DIAGRAM_DIR}"/*.mmd; do
    for extension in svg png; do
      output="${output_dir}/$(basename "${source%.mmd}").${extension}"
      "${MMDC}" \
      --configFile "${CONFIG_FILE}" \
      --input "${source}" \
      --output "${output}" \
      --width 1600 --scale 2 \
      --backgroundColor white
    done
  done
  uv run --project "${ROOT_DIR}" python "${ROOT_DIR}/scripts/render_presentation.py" "${output_dir}"
}

if [[ "${1:-}" == "--verify" ]]; then
  first_dir="$(mktemp -d /tmp/recallops-mermaid-first.XXXXXX)"
  second_dir="$(mktemp -d /tmp/recallops-mermaid-second.XXXXXX)"
  trap 'rm -rf "${first_dir}" "${second_dir}"' EXIT
  render_into "${first_dir}"
  render_into "${second_dir}"
  for first_svg in "${first_dir}"/*.svg "${first_dir}"/*.png; do
    basename_svg="$(basename "${first_svg}")"
    cmp -s "${first_svg}" "${second_dir}/${basename_svg}"
    cmp -s "${first_svg}" "${DIAGRAM_DIR}/${basename_svg}"
    shasum -a 256 "${first_svg}"
  done
  echo "Stable double-render verified for $(find "${first_dir}" -maxdepth 1 -name '*.svg' | wc -l | tr -d ' ') diagrams (Node ${node_version}, npm ${npm_version}, Mermaid CLI ${MERMAID_CLI_VERSION})."
  echo "Committed SVGs match fresh render."
  exit 0
fi

render_into "${DIAGRAM_DIR}"
echo "Rendered $(find "${DIAGRAM_DIR}" -maxdepth 1 -name '*.mmd' | wc -l | tr -d ' ') diagrams (Node ${node_version}, npm ${npm_version}, Mermaid CLI ${MERMAID_CLI_VERSION})."
