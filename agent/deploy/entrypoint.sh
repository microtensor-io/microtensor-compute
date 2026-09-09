#!/usr/bin/env bash
set -euo pipefail

ROLE="${2:-agent}"

if [ "${1:-run}" = "run" ] && [ "$ROLE" = "agent" ]; then
  rm -f /etc/ssh/ssh_host_*
  ssh-keygen -A >/dev/null
  install -d -m 0755 /run/sshd
  install -d -m 0700 -o rig -g rig /home/rig/.ssh
  touch /home/rig/.ssh/authorized_keys
  chown rig:rig /home/rig/.ssh/authorized_keys
  chmod 0600 /home/rig/.ssh/authorized_keys
  install -d -m 0755 -o rig -g rig /opt/mt-agent
  if [ -S /var/run/docker.sock ]; then
    SOCK_GID="$(stat -c %g /var/run/docker.sock)"
    if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
      groupadd -g "$SOCK_GID" docker-host >/dev/null 2>&1 || true
    fi
    usermod -aG "$(getent group "$SOCK_GID" | cut -d: -f1)" rig >/dev/null 2>&1 || true
  fi
  /usr/sbin/sshd -t
  /usr/sbin/sshd -e
fi

exec rig-agent "$@"
