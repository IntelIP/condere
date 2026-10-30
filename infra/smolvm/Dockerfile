ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY infra/smolvm/requirements.txt /tmp/condere-requirements.txt
COPY infra/smolvm/graphify.txt /tmp/condere-graphify.txt
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       bash git ripgrep build-essential sqlite3 libsqlite3-dev fonts-dejavu-core ca-certificates \
    && uv venv /opt/condere-tools --python /usr/local/bin/python \
    && uv pip sync --python /opt/condere-tools/bin/python --require-hashes /tmp/condere-requirements.txt \
    && uv pip install --python /opt/condere-tools/bin/python --no-deps --require-hashes -r /tmp/condere-graphify.txt \
    && useradd -m -d /workspace -s /bin/bash researcher \
    && mkdir -p /workspace \
    && chown researcher:researcher /workspace \
    && rm -rf /var/lib/apt/lists/* /root/.cache/uv

WORKDIR /workspace
ENV PATH="/opt/condere-tools/bin:${PATH}"
USER researcher
CMD ["sleep", "infinity"]
