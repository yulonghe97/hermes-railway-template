FROM python:3.11-slim AS builder

# Pinned Hermes SHA. Our vendor patches (patches/apply-hermes-patches.py)
# are anchor-matched against this specific tree — bumping the pin means
# re-checking that every patch's anchor still exists upstream. Consumers
# can override at build time (`docker build --build-arg HERMES_GIT_REF=...`)
# but the default is the version we test against.
ARG HERMES_GIT_REF=ff9752410a8dba62f1b246aeed9142893c75b4ba

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
# `git clone --branch` won't accept a bare SHA, so fetch-then-checkout.
# `--filter=blob:none` keeps the clone small without the hard depth=1
# limit that would prevent a non-tip SHA from being resolvable.
RUN git clone --filter=blob:none --no-checkout --recurse-submodules https://github.com/NousResearch/hermes-agent.git \
  && cd hermes-agent \
  && git checkout "${HERMES_GIT_REF}" \
  && git submodule update --init --recursive

# Apply vendor patches against the Hermes source before install. Each
# patch is idempotent and guarded by a marker check — remove a patch
# once it lands in an upstream Hermes release this template's
# HERMES_GIT_REF resolves to. See patches/apply-hermes-patches.py.
COPY patches /tmp/patches
RUN python3 /tmp/patches/apply-hermes-patches.py && rm -rf /tmp/patches

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
