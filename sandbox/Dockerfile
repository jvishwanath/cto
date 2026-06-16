FROM python:3.12-slim

# coreutils provides `timeout`; pre-bake common libs so generated code
# doesn't need network (which is disabled at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
        coreutils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        regex \
        requests \
        pytest \
        pyyaml

WORKDIR /work
