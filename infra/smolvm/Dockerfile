ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN apk add --no-cache bash git ripgrep build-base sqlite sqlite-dev \
    && adduser -D -h /workspace researcher \
    && mkdir -p /workspace \
    && chown researcher:researcher /workspace

WORKDIR /workspace
USER researcher
CMD ["sleep", "infinity"]
