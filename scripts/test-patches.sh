#!/usr/bin/env bash
# Verify patches/apply-hermes-patches.py applies cleanly against the
# pinned Hermes SHA in the Dockerfile. Runs locally (no Docker
# required) so CI or a pre-push hook can catch patch rot early.
#
# Steps:
#   1. Read the pinned SHA from the Dockerfile.
#   2. Clone Hermes at that SHA into a tempdir.
#   3. Run the patch script against it.
#   4. Re-run to confirm idempotency.
#   5. Grep the touched files for the expected markers.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"

SHA="$(grep -E '^ARG HERMES_GIT_REF=' "${REPO_ROOT}/Dockerfile" | head -n1 | sed -E 's/^ARG HERMES_GIT_REF=//')"
if [[ -z "${SHA}" ]]; then
  echo "FAIL: could not read HERMES_GIT_REF from Dockerfile" >&2
  exit 1
fi
echo "Pinned SHA: ${SHA}"

TMP="$(mktemp -d -t hermes-patch-test.XXXXXX)"
trap 'rm -rf "${TMP}"' EXIT

echo "Cloning Hermes into ${TMP}..."
git clone --quiet --filter=blob:none --no-checkout https://github.com/NousResearch/hermes-agent.git "${TMP}/hermes-agent"
(cd "${TMP}/hermes-agent" && git checkout --quiet "${SHA}")

echo
echo "--- First apply ---"
HERMES_SRC_DIR="${TMP}/hermes-agent" python3 "${REPO_ROOT}/patches/apply-hermes-patches.py"

echo
echo "--- Second apply (idempotency check) ---"
HERMES_SRC_DIR="${TMP}/hermes-agent" python3 "${REPO_ROOT}/patches/apply-hermes-patches.py"

echo
echo "--- Marker grep ---"
expect() {
  local pattern="$1"
  local file="$2"
  if grep -qF -- "${pattern}" "${TMP}/hermes-agent/${file}"; then
    echo "  OK   ${file}: ${pattern}"
  else
    echo "  FAIL ${file}: missing '${pattern}'" >&2
    exit 1
  fi
}

# slack-strict-mention
expect 'SLACK_STRICT_MENTION' 'gateway/config.py'
expect '_slack_strict_mention' 'gateway/platforms/slack.py'

# send-message-edit-action
expect '"edit"' 'tools/send_message_tool.py'
expect 'message_id' 'tools/send_message_tool.py'
expect '_handle_edit' 'tools/send_message_tool.py'
expect '_edit_slack' 'tools/send_message_tool.py'
expect 'chat.update' 'tools/send_message_tool.py'

echo
echo "--- Syntax check patched files ---"
python3 -c "import ast; ast.parse(open('${TMP}/hermes-agent/tools/send_message_tool.py').read()); print('  OK   tools/send_message_tool.py')"
python3 -c "import ast; ast.parse(open('${TMP}/hermes-agent/gateway/config.py').read()); print('  OK   gateway/config.py')"
python3 -c "import ast; ast.parse(open('${TMP}/hermes-agent/gateway/platforms/slack.py').read()); print('  OK   gateway/platforms/slack.py')"

echo
echo "All patches applied cleanly against ${SHA}."
