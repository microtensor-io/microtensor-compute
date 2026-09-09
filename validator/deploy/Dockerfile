FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential make openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home validator \
    && mkdir -p /var/lib/compute-validator \
    && chown validator:validator /var/lib/compute-validator

WORKDIR /app
COPY pyproject.toml ./
COPY protocol ./protocol
COPY challenge ./challenge
COPY validator ./validator

RUN make -C challenge validator \
    && install -m 0644 challenge/build/libmtverify.so /usr/lib/libmtverify.so \
    && pip install ".[validator]"

USER validator
VOLUME ["/var/lib/compute-validator"]
CMD ["compute-validator", "run"]
