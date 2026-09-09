FROM python:3.12-slim

ARG CHALLENGE_LIBRARY=challenge/README.md

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_DRIVER_CAPABILITIES=all \
    RIG_PYTHON=/usr/local/bin/python3 \
    RIG_CHALLENGE_LIBRARY=/usr/lib/libmtchallenge.so

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server curl jq ca-certificates gnupg procps sudo \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 rig \
    && install -d -m 0700 -o rig -g rig /home/rig/.ssh \
    && touch /home/rig/.ssh/authorized_keys \
    && chown rig:rig /home/rig/.ssh/authorized_keys \
    && chmod 0600 /home/rig/.ssh/authorized_keys \
    && echo "rig ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/rig \
    && chmod 0440 /etc/sudoers.d/rig \
    && rm -f /etc/ssh/ssh_host_* \
    && install -d -m 0755 /run/sshd /var/lib/rig-agent /var/lib/rig-agent-logs \
    && install -d -m 0755 -o rig -g rig /opt/mt-agent \
    && ln -sf /usr/local/bin/python3 /usr/bin/python3

COPY agent/deploy/sshd_config /etc/ssh/sshd_config.d/rig.conf
COPY agent/deploy/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod 0755 /usr/local/bin/entrypoint && chmod 0644 /etc/ssh/sshd_config.d/rig.conf

WORKDIR /app
COPY pyproject.toml ./
COPY protocol ./protocol
COPY agent ./agent
RUN pip install ".[agent]"

COPY ${CHALLENGE_LIBRARY} /tmp/challenge-lib
RUN if [ "$(head -c 4 /tmp/challenge-lib | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; then \
        install -m 0644 /tmp/challenge-lib /usr/lib/libmtchallenge.so; \
    fi \
    && rm -f /tmp/challenge-lib

EXPOSE 8800 22
ENTRYPOINT ["entrypoint"]
CMD ["run", "agent"]
