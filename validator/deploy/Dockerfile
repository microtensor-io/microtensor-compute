FROM python:3.12-slim

ARG CHALLENGE_LIBRARY=challenge/README.md

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CV_CHALLENGE_LIBRARY=/usr/lib/libmtverify.so \
    CV_AGENT_LIBRARY=/usr/lib/libmtchallenge.so \
    CV_STATE_DIR=/var/lib/compute-validator

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

COPY ${CHALLENGE_LIBRARY} /tmp/challenge-lib
RUN if [ "$(head -c 4 /tmp/challenge-lib | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then \
        install -m 0644 /tmp/challenge-lib /usr/lib/libmtchallenge.so; \
    fi \
    && rm -f /tmp/challenge-lib

USER validator
VOLUME ["/var/lib/compute-validator"]
CMD ["compute-validator", "run"]
