FROM python:3.12.12-slim-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The default uses normal system trust. The build CA argument also permits
# testing behind a managed proxy with a temporary BuildKit secret mount.
ARG PREDBAT_BUILD_CA=/etc/ssl/certs/ca-certificates.crt
RUN apt-get -o "Acquire::https::CaInfo=${PREDBAT_BUILD_CA}" update \
    && apt-get -o "Acquire::https::CaInfo=${PREDBAT_BUILD_CA}" install -y --no-install-recommends \
        build-essential ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PREDBAT_APPS_FILE=/config/apps.yaml \
    PREDBAT_KERNEL_REQUIRED=1 \
    PYTHONPATH=/opt/predbat-source/apps/predbat \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/config/cache/matplotlib \
    XDG_CACHE_HOME=/config/cache

COPY source.json /addon/source.json
RUN read -r predbat_repository predbat_ref < <(python -c \
        'import json, re; d=json.load(open("/addon/source.json")); assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", d["repository"]); assert re.fullmatch(r"[0-9a-f]{40}", d["ref"]); print(d["repository"], d["ref"])') \
    && test -n "$predbat_repository" && test -n "$predbat_ref" \
    && git init --quiet /opt/predbat-source \
    && GIT_SSL_CAINFO="$PREDBAT_BUILD_CA" git -C /opt/predbat-source fetch --quiet --depth 1 \
        "https://github.com/${predbat_repository}.git" "$predbat_ref" \
    && git -C /opt/predbat-source checkout --quiet --detach FETCH_HEAD \
    && test "$(git -C /opt/predbat-source rev-parse HEAD)" = "$predbat_ref" \
    && PIP_CERT="$PREDBAT_BUILD_CA" pip install --no-cache-dir \
        -r /opt/predbat-source/requirements.txt Jinja2 pvlib

COPY prepare_image.py launcher.py apps.yaml /addon/
RUN bash /opt/predbat-source/apps/predbat/build_kernel.sh \
    && python /addon/prepare_image.py \
    && chmod -R a+rX /opt/predbat-source /addon \
    && chmod -R a-w /opt/predbat-source /addon

ARG BUILD_ARCH
ARG BUILD_VERSION=0.1.0
LABEL io.hass.name="Predbat Enphase Local - Testing" \
    io.hass.description="Predbat with local Enphase battery control" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/chinedu40/batpred" \
    org.opencontainers.image.version="${BUILD_VERSION}"

WORKDIR /config
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/addon/launcher.py"]
