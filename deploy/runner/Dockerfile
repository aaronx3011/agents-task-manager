# Self-hosted GitHub Actions runner, containerized.
# The container joins the host Docker daemon through the mounted socket
# (builds/images) and uses host networking to reach VPN peers (SSH deploys).
# DOCKER_GID must match the host's /var/run/docker.sock group id.

ARG RUNNER_VERSION=2.338.0
FROM docker:27-cli AS docker-cli

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
  && apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git openssh-client rsync jq libicu70 libkrb5-3 zlib1g \
  && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

ARG RUNNER_VERSION
RUN curl -fsSL -o /tmp/runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
  && mkdir /opt/actions-runner \
  && tar xzf /tmp/runner.tar.gz -C /opt/actions-runner \
  && rm /tmp/runner.tar.gz \
  && /opt/actions-runner/bin/installdependencies.sh \
  && rm -rf /var/lib/apt/lists/*

ARG DOCKER_GID=999
RUN groupadd -g "${DOCKER_GID}" docker \
  && useradd -m -s /bin/bash runner \
  && usermod -aG docker runner \
  && mkdir -p /work \
  && chown -R runner:runner /opt/actions-runner /work
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER runner
WORKDIR /home/runner
ENTRYPOINT ["/entrypoint.sh"]
