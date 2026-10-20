#!/usr/bin/env bash
# One-time host bootstrap for the development deployment target (10.8.0.11).
# Idempotent: installs only what is missing; never removes packages, data or
# existing processes. Requires sudo.
set -euo pipefail

if [[ ${SUDO_USER:-} != "" || $(id -u) -eq 0 ]]; then SUDO=(); else SUDO=(sudo); fi

echo "==> Checking Docker"
if docker --version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "    Docker and Compose already installed."
else
  echo "    Installing Docker Engine + Compose plugin (Ubuntu apt)."
  "${SUDO[@]}" apt-get update -qq
  "${SUDO[@]}" apt-get install -y -qq docker.io docker-compose-v2 ca-certificates curl git
  "${SUDO[@]}" systemctl enable --now docker
fi

if id -nG "$USER" | grep -qw docker; then
  echo "==> Docker group: already a member"
else
  echo "==> Adding $(whoami) to the docker group (applies to new sessions)."
  "${SUDO[@]}" usermod -aG docker "$(whoami)"
fi

echo "==> Docker: $(docker --version)"
docker compose version
echo "Bootstrap complete."
