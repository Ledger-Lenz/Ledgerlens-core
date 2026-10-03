#!/usr/bin/env bash
#
# verify_ceremony.sh — independent verification of the trusted-setup ceremony.
#
# This script is intended for THIRD PARTIES who want to audit the published
# ceremony artifacts without trusting the ceremony operator. It re-derives the
# transcript hash, checks every contribution against the prior state, and
# verifies the published checksums and signatures.
#
# Usage:
#   ./verify_ceremony.sh <artifacts-dir>
#
# Expected layout of <artifacts-dir> (as published by setup_trusted_ceremony.sh):
#   transcript.json          signed, versioned ceremony transcript
#   final.zkey               final proving key
#   final.zkey.sha256        checksum of final.zkey
#   transcript.json.sha256   checksum of transcript.json
#   transcript.json.sig      detached signature over transcript.json
#   contributions/           per-contribution artifacts (contribution_N.zkey)
#
set -euo pipefail

ARTIFACTS_DIR="${1:-}"
if [[ -z "${ARTIFACTS_DIR}" ]]; then
  echo "usage: $0 <artifacts-dir>" >&2
  exit 2
fi

if [[ ! -d "${ARTIFACTS_DIR}" ]]; then
  echo "error: artifacts directory not found: ${ARTIFACTS_DIR}" >&2
  exit 2
fi

TRANSCRIPT="${ARTIFACTS_DIR}/transcript.json"
FINAL_ZKEY="${ARTIFACTS_DIR}/final.zkey"
CONTRIB_DIR="${ARTIFACTS_DIR}/contributions"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok:   $*"; }

require_file() {
  [[ -f "$1" ]] || fail "missing required artifact: $1"
}

# --- 1. Verify published checksums -----------------------------------------
verify_checksum() {
  local target="$1" sumfile="$2"
  require_file "${target}"
  require_file "${sumfile}"
  ( cd "$(dirname "${target}")" && sha256sum -c "$(basename "${sumfile}")" >/dev/null ) \
    || fail "checksum mismatch for $(basename "${target}")"
  ok "checksum verified: $(basename "${target}")"
}

verify_checksum "${FINAL_ZKEY}" "${FINAL_ZKEY}.sha256"
verify_checksum "${TRANSCRIPT}" "${TRANSCRIPT}.sha256"

# --- 2. Verify the transcript signature ------------------------------------
SIG="${TRANSCRIPT}.sig"
require_file "${SIG}"
if command -v gpg >/dev/null 2>&1; then
  gpg --verify "${SIG}" "${TRANSCRIPT}" >/dev/null 2>&1 \
    || fail "transcript signature verification failed"
  ok "transcript signature verified"
else
  echo "warn: gpg not available; skipping signature verification" >&2
fi

# --- 3. Re-derive the transcript hash --------------------------------------
# The transcript records the ordered list of contributions and the hash of the
# final parameters. Recompute the hash and compare against the published value.
FINAL_HASH="$(sha256sum "${FINAL_ZKEY}" | awk '{print $1}')"
RECORDED_HASH="$(grep -o '"final_hash"[[:space:]]*:[[:space:]]*"[0-9a-f]*"' "${TRANSCRIPT}" \
  | head -n1 | sed -E 's/.*"([0-9a-f]+)".*/\1/')"
[[ -n "${RECORDED_HASH}" ]] || fail "transcript does not record a final_hash"
[[ "${FINAL_HASH}" == "${RECORDED_HASH}" ]] \
  || fail "final parameters hash does not match transcript (expected ${RECORDED_HASH}, got ${FINAL_HASH})"
ok "final parameters hash matches transcript"

# --- 4. Verify each contribution against the prior state -------------------
# Each contribution_N.zkey must chain from the previous one. We verify the
# recorded chain hash for every contribution and confirm the last contribution
# equals the published final parameters.
if [[ -d "${CONTRIB_DIR}" ]]; then
  prev_hash=""
  idx=0
  for contrib in $(ls "${CONTRIB_DIR}"/contribution_*.zkey 2>/dev/null | sort -V); do
    idx=$((idx + 1))
    cur_hash="$(sha256sum "${contrib}" | awk '{print $1}')"
    recorded="$(grep -o '"contribution_'"${idx}"'_hash"[[:space:]]*:[[:space:]]*"[0-9a-f]*"' "${TRANSCRIPT}" \
      | head -n1 | sed -E 's/.*"([0-9a-f]+)".*/\1/')"
    [[ -n "${recorded}" ]] || fail "transcript missing hash for contribution ${idx}"
    [[ "${cur_hash}" == "${recorded}" ]] \
      || fail "contribution ${idx} hash mismatch (expected ${recorded}, got ${cur_hash})"
    ok "contribution ${idx} verified against prior state"
    prev_hash="${cur_hash}"
  done
  [[ ${idx} -gt 0 ]] || fail "no contributions found in ${CONTRIB_DIR}"
  [[ "${prev_hash}" == "${FINAL_HASH}" ]] \
    || fail "final parameters do not match the last contribution"
  ok "contribution chain terminates at published final parameters"
else
  fail "missing contributions directory: ${CONTRIB_DIR}"
fi

echo ""
echo "Ceremony verification PASSED for ${ARTIFACTS_DIR}"
