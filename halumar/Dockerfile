# Base image: Alpine with S6 overlay v3 and Bashio, multi-arch (amd64,
# aarch64). Pinned by tag and digest of the multi-arch index; Dependabot
# bumps both.
# 20.2.0 = Alpine 3.23 (supported until late 2027) with Python 3.12, the
# version every pin in requirements.txt has a musl wheel for. 21.x (Alpine
# 3.24) ships Python 3.14: pydantic-core 2.27.2 (pinned by pydantic 2.10.4)
# has no cp314 wheel, so that bump needs a pydantic update first.
FROM ghcr.io/hassio-addons/base:20.2.0@sha256:49e402637a700b405f84c16f2ccc5a000bb7fcc9a4f9fa1ef619042f9c26643c

# Python for the hub. The real pins are the exact versions in requirements.txt,
# installed as musl wheels only; apk pins only what those wheels depend on:
# - python3~3.12: the wheel ABI (cp312). An Alpine branch keeps its Python minor
#   for its whole life (3.23: 3.12); a new minor only comes with a new base
#   image, which is pinned by digest above and checked by tests/test_image_base.py.
# - py3-pip~25: only installs the wheels at build time; any 25.x will do.
# libffi is not listed: python3 itself depends on it (so:libffi.so.8), and the
# cffi wheel links its own statically. Alpine keeps only the newest build of a package per
# branch, so pins at patch level ("~3.5", "=x.y.z-rN") break with the next
# security fix of the branch.
# Do NOT install openssl: it conflicts with the base image's pinned version; the
# cryptography wheel ships its own statically linked OpenSSL.
# hadolint ignore=DL3018
RUN apk add --no-cache \
    python3~3.12 \
    py3-pip~25

# Copy requirements and install Python packages (wheels only, no compilation)
COPY rootfs/opt/hub/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Copy root filesystem (includes pre-built frontend in /opt/hub/static/)
COPY rootfs /

# Build arguments
ARG BUILD_ARCH
ARG BUILD_VERSION

# Labels (the Supervisor and the builder action set io.hass.version/arch too)
LABEL \
    io.hass.name="Halumar" \
    io.hass.description="Gives every device in your home its own, limited view of Home Assistant" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Halumar Hub" \
    org.opencontainers.image.description="Gives every device in your home its own, limited view of Home Assistant" \
    org.opencontainers.image.source="https://github.com/Halumar/hub" \
    org.opencontainers.image.url="https://halumar.com" \
    org.opencontainers.image.documentation="https://github.com/Halumar/hub/blob/main/halumar/DOCS.md" \
    org.opencontainers.image.vendor="xonatec" \
    org.opencontainers.image.authors="xonatec" \
    org.opencontainers.image.licenses="AGPL-3.0-only" \
    org.opencontainers.image.version="${BUILD_VERSION}"
