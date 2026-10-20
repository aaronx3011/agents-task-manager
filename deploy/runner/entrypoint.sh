#!/bin/bash
# Register on first boot (token required), then run. Registration persists in
# the mounted volume, so container restarts/recreations reuse it.
set -euo pipefail
cd /opt/actions-runner

if [ ! -f .runner ]; then
  : "${RUNNER_URL:?Set RUNNER_URL}"
  : "${RUNNER_TOKEN:?Set RUNNER_TOKEN (registration token, valid 1 hour)}"
  ./config.sh \
    --url "$RUNNER_URL" \
    --token "$RUNNER_TOKEN" \
    --name "${RUNNER_NAME:-dev-runner-6}" \
    --labels "${RUNNER_LABELS:-dev-runner}" \
    --work /work \
    --unattended --replace
fi

exec ./run.sh
