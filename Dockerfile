FROM python:3.11-slim AS builder

# Pinned Hermes SHA on our fork (yulonghe97/hermes-agent, branch
# `hellyeah/base`). The fork carries our vendor patches as real commits
# on top of a known-good upstream SHA — bumping this pin means rebasing
# `hellyeah/patches` onto a new base in the fork first. Consumers can
# override at build time (`docker build --build-arg HERMES_GIT_REF=...`).
ARG HERMES_GIT_REF=9a000fe742f914ae3846f00330d3155fcf403241

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
# `git clone --branch` won't accept a bare SHA, so fetch-then-checkout.
# `--filter=blob:none` keeps the clone small without the hard depth=1
# limit that would prevent a non-tip SHA from being resolvable.
RUN git clone --filter=blob:none --no-checkout --recurse-submodules https://github.com/yulonghe97/hermes-agent.git \
  && cd hermes-agent \
  && git checkout "${HERMES_GIT_REF}" \
  && git submodule update --init --recursive

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -e "/opt/hermes-agent[messaging,cron,cli,pty,mcp]"


FROM python:3.11-slim

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    openssh-client \
    tini \
  && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:${PATH}" \
  PYTHONUNBUFFERED=1 \
  HERMES_HOME=/data/.hermes \
  HOME=/data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hermes-agent /opt/hermes-agent

WORKDIR /app
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN chmod +x /app/scripts/entrypoint.sh

ENTRYPOINT ["tini", "--"]
CMD ["/app/scripts/entrypoint.sh"]
