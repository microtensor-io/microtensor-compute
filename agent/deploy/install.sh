#!/usr/bin/env bash
set -euo pipefail

TARGET="${RIG_AGENT_DIR:-/opt/rig-agent}"
SERVER_URL="${RIG_SERVER_URL:-https://api.microtensor.cloud}"
RAW="${RIG_AGENT_RAW:-https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/agent/deploy}"
SYSBOX_VERSION="0.6.6"
SYSBOX_URL="https://downloads.nestybox.com/sysbox/releases/v${SYSBOX_VERSION}/sysbox-ce_${SYSBOX_VERSION}-0.linux_amd64.deb"
SYSBOX_SHA256="87cfa5cad97dc5dc1a243d6d88be1393be75b93a517dc1580ecd8a2801c2777a"
PROBE_IMAGE="ubuntu:22.04"
QUOTA_IMAGE="alpine"
WORK="$(mktemp -d)"
APT_LOG="${WORK}/apt.log"

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

apt_run() {
  if ! DEBIAN_FRONTEND=noninteractive apt-get "$@" >>"$APT_LOG" 2>&1; then
    tail -n 40 "$APT_LOG" >&2
    fail "apt-get $* failed (full log in ${APT_LOG})"
  fi
}

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

kernel_remediation() {
  cat >&2 <<'TEXT'
Kernel 5.19 or newer is required: overlayfs gained ID-mapped mounts in 5.19, and without them
sysbox falls back to shiftfs and the NVIDIA hook fails, so GPUs do not work in sysbox containers.
  Ubuntu 22.04:  apt-get install -y linux-generic-hwe-22.04 && reboot
  Ubuntu 20.04:  tops out at 5.15, upgrade the distribution (do-release-upgrade)
Before changing the kernel stop every rental and check `dkms status`: a driver installed from a
.run file will not load after the reboot.
TEXT
}

idmapped_remediation() {
  cat >&2 <<'TEXT'
sysbox-mgr reports "Overlayfs on ID-mapped mounts supported by kernel: no" for this boot.
  On kernel 5.19 or newer this means the docker data-root (/var/lib/docker) sits on a filesystem
  without ID-mapped mount support (ZFS, some btrfs setups) or sysbox-mgr is misconfigured.
  Move the docker data-root to ext4 or xfs, then: systemctl restart sysbox docker
  On an older kernel upgrade the kernel first (see the kernel note above).
Check what sysbox decided with: journalctl -u sysbox-mgr -b --no-pager | grep -i "ID-mapped"
TEXT
}

sysbox_diagnostic() {
  cat >&2 <<'TEXT'
The sysbox GPU probe failed. Collect these before asking for help:
  docker version --format '{{.Server.Version}}'
  docker info --format '{{json .Runtimes}}'
  journalctl -u sysbox-mgr -b --no-pager | tail -n 40
  journalctl -u sysbox-fs -b --no-pager | tail -n 20
  cat /etc/docker/daemon.json
  nvidia-ctk --version && cat /etc/nvidia-container-runtime/config.toml
TEXT
}

[ "$(id -u)" -eq 0 ] || fail "run as root: curl -fsSL ${RAW}/install.sh | sudo bash"
[ "$(uname -m)" = "x86_64" ] || fail "x86_64 is required (sysbox does not support $(uname -m))"
[ -f /etc/os-release ] || fail "unsupported operating system (no /etc/os-release)"
. /etc/os-release
case "${ID:-}" in
  ubuntu)
    case "${VERSION_ID:-}" in
      22.04|24.04) ;;
      *)
        [ "${RIG_FORCE:-0}" = "1" ] || fail "Ubuntu 22.04 or 24.04 is required, found Ubuntu ${VERSION_ID:-unknown} (set RIG_FORCE=1 to try anyway)"
        warn "untested Ubuntu release ${VERSION_ID:-unknown}, continuing because RIG_FORCE=1"
        ;;
    esac
    ;;
  debian)
    [ "${RIG_FORCE:-0}" = "1" ] || fail "Ubuntu 22.04 or 24.04 is required; Debian ${VERSION_ID:-} is untested (set RIG_FORCE=1 to try anyway)"
    warn "Debian ${VERSION_ID:-} is untested, continuing because RIG_FORCE=1"
    ;;
  *) fail "Ubuntu or a Debian derivative is required, found ${ID:-unknown}" ;;
esac

KERNEL="$(uname -r)"
KMAJOR="${KERNEL%%.*}"
KMINOR="$(echo "$KERNEL" | cut -d. -f2 | tr -dc '0-9')"
KERNEL_OK=1
if [ "$KMAJOR" -lt 5 ] || { [ "$KMAJOR" -eq 5 ] && [ "${KMINOR:-0}" -lt 19 ]; }; then
  KERNEL_OK=0
fi

command -v nvidia-smi >/dev/null 2>&1 || fail "the NVIDIA driver is not installed (nvidia-smi missing); install it first and reboot"
[ -d /proc/driver/nvidia ] || fail "the NVIDIA kernel module is not loaded (/proc/driver/nvidia missing)"

if [ "$KERNEL_OK" -eq 0 ]; then
  if command -v docker >/dev/null 2>&1 && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q sysbox-runc \
     && docker run --rm --runtime=sysbox-runc --gpus all "$PROBE_IMAGE" nvidia-smi -L >/dev/null 2>&1; then
    warn "kernel ${KERNEL} is below 5.19 but sysbox with GPUs already works on this host; continuing"
  else
    kernel_remediation
    fail "kernel 5.19 or newer is required, found ${KERNEL}"
  fi
fi

if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  RENTALS="$(docker ps -q --filter label=mt.job | wc -l | tr -d ' ')"
  [ "$RENTALS" -eq 0 ] || fail "${RENTALS} rental container(s) are running; wait for them to finish or stop them before installing"
  if [ -f "${TARGET}/docker-compose.yml" ]; then
    say "stopping the existing agent stack"
    (cd "$TARGET" && docker compose down --remove-orphans >/dev/null 2>&1) || true
  fi
fi

say "installing prerequisites"
apt_run update
apt_run install -y --no-install-recommends ca-certificates curl gnupg jq lsb-release

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  say "installing docker-ce from Docker's repository"
  install -m 0755 -d /etc/apt/keyrings
  DOCKER_KEY="${WORK}/docker.asc"
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o "$DOCKER_KEY"
  install -m 0644 "$DOCKER_KEY" /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt_run update
  apt_run install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker >/dev/null 2>&1 || true
fi

if ! command -v nvidia-ctk >/dev/null 2>&1; then
  say "installing nvidia-container-toolkit from NVIDIA's repository"
  NVIDIA_KEY="${WORK}/nvidia.gpg"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$NVIDIA_KEY"
  gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg <"$NVIDIA_KEY"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt_run update
  apt_run install -y nvidia-container-toolkit
fi

INSTALLED_SYSBOX="$(sysbox-runc --version 2>/dev/null | awk '/version:/ {print $3}' || true)"
if [ "$INSTALLED_SYSBOX" != "$SYSBOX_VERSION" ]; then
  say "installing sysbox-ce ${SYSBOX_VERSION}"
  RUNNING="$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${RUNNING:-0}" -ne 0 ]; then
    docker ps --format '  {{.Names}}  {{.Image}}' >&2
    fail "sysbox installs only with zero running containers; stop the containers listed above and run the installer again"
  fi
  STOPPED="$(docker ps -aq 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${STOPPED:-0}" -ne 0 ]; then
    say "removing ${STOPPED} stopped container(s) so sysbox can install"
    docker rm -f "$(docker ps -aq)" >/dev/null 2>&1 || true
  fi
  SYSBOX_DEB="${WORK}/sysbox-ce.deb"
  curl -fsSL "$SYSBOX_URL" -o "$SYSBOX_DEB"
  echo "${SYSBOX_SHA256}  ${SYSBOX_DEB}" | sha256sum -c - >/dev/null || fail "sysbox-ce download does not match the pinned sha256"
  apt_run install -y "$SYSBOX_DEB"
fi

DAEMON_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
[ -n "$DAEMON_VERSION" ] || fail "the docker daemon is not answering; check systemctl status docker"
say "docker daemon ${DAEMON_VERSION}"

say "merging /etc/docker/daemon.json"
DAEMON_JSON=/etc/docker/daemon.json
if [ -f "$DAEMON_JSON" ]; then
  cp -a "$DAEMON_JSON" "${DAEMON_JSON}.bak.$(date +%s)"
  jq -e . "$DAEMON_JSON" >/dev/null 2>&1 || fail "${DAEMON_JSON} is not valid JSON; fix it (a backup was taken) and run the installer again"
else
  install -d -m 0755 /etc/docker
  echo '{}' > "$DAEMON_JSON"
fi
TIME_NS='.'
if version_ge "$DAEMON_VERSION" "29.5"; then
  TIME_NS='.features["time-namespaces"] = false'
fi
MERGED="$(jq \
  '.runtimes["sysbox-runc"] = {"path": "/usr/bin/sysbox-runc"}
   | .runtimes["nvidia"] = {"path": "nvidia-container-runtime", "runtimeArgs": []}
   | .features = ((.features // {}) + {"cdi": false})' "$DAEMON_JSON" | jq "$TIME_NS")"
[ -n "$MERGED" ] || fail "jq produced an empty daemon.json; nothing was changed"
printf '%s\n' "$MERGED" > "${DAEMON_JSON}.new"
jq -e . "${DAEMON_JSON}.new" >/dev/null || fail "merged daemon.json is not valid JSON; nothing was changed"
mv "${DAEMON_JSON}.new" "$DAEMON_JSON"
if version_ge "$DAEMON_VERSION" "29.2"; then
  systemctl disable --now nvidia-cdi-refresh.path nvidia-cdi-refresh.service >/dev/null 2>&1 || true
  rm -f /etc/cdi/nvidia.yaml /var/run/cdi/nvidia.yaml
fi

say "restarting docker"
systemctl restart docker
READY=0
for _ in $(seq 1 30); do
  if docker ps >/dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
[ "$READY" -eq 1 ] || fail "docker did not come back after the restart; check journalctl -u docker"
systemctl restart sysbox >/dev/null 2>&1 || true
sleep 2

IDMAP_LINE="$(journalctl -u sysbox-mgr -b --no-pager 2>/dev/null | grep -Eio 'ID-mapped mounts supported by kernel: (yes|no)' | tail -n 1 || true)"
case "$IDMAP_LINE" in
  *yes) say "sysbox: ${IDMAP_LINE}" ;;
  *no)
    idmapped_remediation
    [ "$KERNEL_OK" -eq 1 ] || kernel_remediation
    fail "sysbox reports no ID-mapped mount support on this boot"
    ;;
  *) warn "could not find the sysbox-mgr ID-mapped mount line in the journal for this boot; the validator will check it in its own session" ;;
esac

say "verifying sysbox with GPUs"
if ! docker run --rm --runtime=sysbox-runc --gpus all "$PROBE_IMAGE" nvidia-smi -L >"${WORK}/probe.log" 2>&1; then
  tail -n 20 "${WORK}/probe.log" >&2
  sysbox_diagnostic
  fail "docker run --runtime=sysbox-runc --gpus all failed"
fi
say "sysbox GPU probe: $(head -n 1 "${WORK}/probe.log")"

say "probing per-container storage quotas"
if docker run --rm --storage-opt size=1g "$QUOTA_IMAGE" true >/dev/null 2>&1; then
  say "storage quota supported"
else
  warn "docker cannot enforce --storage-opt size (overlay2 needs xfs with pquota); the rig will not be eligible for rental until this is fixed"
fi

say "fetching the authorised agent release"
DIGEST="${RIG_AGENT_IMAGE_SHA256:-}"
if [ -z "$DIGEST" ]; then
  DIGEST="$(curl -fsS --max-time 30 "${SERVER_URL}/v1/pool/agent-release" | jq -r '.release.digest // empty' || true)"
fi
[ -n "$DIGEST" ] || fail "no authorised agent release is published yet; set RIG_AGENT_IMAGE_SHA256=sha256:... and run again"
echo "$DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "authorised digest is malformed: ${DIGEST}"

mkdir -p "$TARGET"
curl -fsSL "${RAW}/docker-compose.yml" -o "${TARGET}/docker-compose.yml.new"
grep -q 'ghcr.io/microtensor-io/rig-agent@${AGENT_IMAGE_SHA256}' "${TARGET}/docker-compose.yml.new" || fail "downloaded compose file does not pin the image by digest"
mv "${TARGET}/docker-compose.yml.new" "${TARGET}/docker-compose.yml"

ENV_FILE="${TARGET}/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENV
RIG_SERVER_URL=${SERVER_URL}
AGENT_IMAGE_SHA256=${DIGEST}
EXTERNAL_PORT=${RIG_EXTERNAL_PORT:-8800}
INTERNAL_PORT=${RIG_INTERNAL_PORT:-8800}
SSH_PORT=${RIG_SSH_PORT:-2200}
RIG_PORT_RANGE=${RIG_PORT_RANGE:-40000-40100}
RIG_PUBLIC_ADDRESS=${RIG_PUBLIC_ADDRESS:-}
RIG_LABEL=${RIG_LABEL:-$(hostname)}
RIG_INSTALL_DIR=${TARGET}
ENV
else
  grep -v '^AGENT_IMAGE_SHA256=' "$ENV_FILE" > "${ENV_FILE}.new" || true
  echo "AGENT_IMAGE_SHA256=${DIGEST}" >> "${ENV_FILE}.new"
  grep -q '^RIG_INSTALL_DIR=' "${ENV_FILE}.new" || echo "RIG_INSTALL_DIR=${TARGET}" >> "${ENV_FILE}.new"
  mv "${ENV_FILE}.new" "$ENV_FILE"
fi
chmod 0600 "$ENV_FILE"

say "starting the agent in ${TARGET}"
cd "$TARGET"
docker compose --env-file .env pull >>"$APT_LOG" 2>&1 || { tail -n 20 "$APT_LOG" >&2; fail "could not pull the agent image ${DIGEST}"; }
docker compose --env-file .env up -d

cat <<TEXT

The agent is running. Read the registration code with:
  cd ${TARGET} && docker compose logs agent
Enter the code on the portal under Compute Pool > Add rig. When the portal asks which hotkey
claims the rig, approve it from this machine:
  cd ${TARGET} && docker compose exec agent rig-agent approve <hotkey>
Status at any time:
  cd ${TARGET} && docker compose exec agent rig-agent status
TEXT
