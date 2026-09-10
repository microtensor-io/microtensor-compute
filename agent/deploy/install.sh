#!/usr/bin/env bash
set -euo pipefail

TARGET="${RIG_AGENT_DIR:-/opt/rig-agent}"
SERVER_URL="${RIG_SERVER_URL:-https://api.microtensor.cloud}"
RAW="${RIG_AGENT_RAW:-https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/agent/deploy}"
SYSBOX_MIN_VERSION="0.6.6"
IMAGE="ghcr.io/microtensor-io/rig-agent"
PROBE_IMAGE="ubuntu:22.04"
QUOTA_IMAGE="alpine"
WORK="$(mktemp -d)"
LOG="${WORK}/install.log"
MISSING=0

say() { printf '==> %s\n' "$*"; }
ok() { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*" >&2; }
missing() { printf '  [MISSING] %s\n' "$*" >&2; MISSING=1; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

requirements_text() {
  cat >&2 <<'TEXT'

This host does not meet the rig requirements. The agent installer sets nothing up for you;
bring the machine to the requirements and run it again.

  Operating system   Ubuntu 22.04 or 24.04, x86_64
  Kernel             5.19 or newer (ID-mapped mounts; sysbox falls back to shiftfs below that
                     and the NVIDIA hook fails)
  NVIDIA driver      installed and loaded (nvidia-smi works)
  Docker             docker-ce with the compose plugin, daemon answering
  NVIDIA toolkit     nvidia-container-toolkit, the "nvidia" runtime registered with docker
  Sysbox             sysbox-ce 0.6.6 or newer, the "sysbox-runc" runtime registered with docker,
                     and `journalctl -u sysbox-mgr -b` reporting ID-mapped mounts supported: yes
  Storage quotas     docker able to enforce --storage-opt size (overlay2 on xfs with pquota);
                     without it the rig is not eligible for rental
  Network            public IPv4 without carrier grade NAT, SSH reachable from validators
  Tools              curl and jq on the PATH

Requirements and remediation: https://www.microtensor.cloud/docs/compute/guides/rig-owner
TEXT
}

say "checking the host against the rig requirements"

[ "$(id -u)" -eq 0 ] || fail "run as root: curl -fsSL ${RAW}/install.sh | sudo bash"

if [ "$(uname -m)" = "x86_64" ]; then ok "architecture x86_64"; else missing "architecture $(uname -m); x86_64 is required"; fi

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu)
      case "${VERSION_ID:-}" in
        22.04|24.04) ok "Ubuntu ${VERSION_ID}" ;;
        *) if [ "${RIG_FORCE:-0}" = "1" ]; then warn "Ubuntu ${VERSION_ID:-unknown} is untested (RIG_FORCE=1)"; else missing "Ubuntu ${VERSION_ID:-unknown}; 22.04 or 24.04 is required (RIG_FORCE=1 to try anyway)"; fi ;;
      esac ;;
    *) if [ "${RIG_FORCE:-0}" = "1" ]; then warn "${ID:-unknown} is untested (RIG_FORCE=1)"; else missing "operating system ${ID:-unknown}; Ubuntu 22.04 or 24.04 is required (RIG_FORCE=1 to try anyway)"; fi ;;
  esac
else
  missing "no /etc/os-release; Ubuntu 22.04 or 24.04 is required"
fi

KERNEL="$(uname -r)"
KMAJOR="${KERNEL%%.*}"
KMINOR="$(echo "$KERNEL" | cut -d. -f2 | tr -dc '0-9')"
if [ "$KMAJOR" -gt 5 ] || { [ "$KMAJOR" -eq 5 ] && [ "${KMINOR:-0}" -ge 19 ]; }; then
  ok "kernel ${KERNEL}"
else
  missing "kernel ${KERNEL}; 5.19 or newer is required (Ubuntu 22.04: apt-get install -y linux-generic-hwe-22.04 && reboot; check dkms status first)"
fi

for tool in curl jq; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool present"; else missing "$tool is not installed (apt-get install -y $tool)"; fi
done

if command -v nvidia-smi >/dev/null 2>&1 && [ -d /proc/driver/nvidia ]; then
  ok "NVIDIA driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
else
  missing "NVIDIA driver not installed or not loaded (nvidia-smi and /proc/driver/nvidia)"
fi

DOCKER_OK=0
if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  DOCKER_OK=1
  ok "docker daemon $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
else
  missing "docker is not installed or the daemon is not answering (docker-ce from https://docs.docker.com/engine/install/ubuntu/)"
fi

if [ "$DOCKER_OK" -eq 1 ]; then
  if docker compose version >/dev/null 2>&1; then ok "docker compose plugin"; else missing "docker compose plugin (docker-compose-plugin)"; fi
  RUNTIMES="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || echo '{}')"
  if printf '%s' "$RUNTIMES" | grep -q '"nvidia"'; then
    ok "nvidia runtime registered"
  else
    missing "nvidia runtime not registered with docker (install nvidia-container-toolkit, then nvidia-ctk runtime configure --runtime=docker && systemctl restart docker)"
  fi
  if printf '%s' "$RUNTIMES" | grep -q 'sysbox-runc'; then
    SYSBOX_VERSION="$(sysbox-runc --version 2>/dev/null | awk '/version:/ {print $NF; exit}' || true)"
    if [ -n "$SYSBOX_VERSION" ] && version_ge "$SYSBOX_VERSION" "$SYSBOX_MIN_VERSION"; then
      ok "sysbox-runc ${SYSBOX_VERSION}"
    else
      missing "sysbox-runc ${SYSBOX_VERSION:-unknown}; ${SYSBOX_MIN_VERSION} or newer is required (https://github.com/nestybox/sysbox/releases)"
    fi
    IDMAP_LINE="$(journalctl -u sysbox-mgr -b --no-pager 2>/dev/null | grep -Eio 'ID-mapped mounts supported by kernel: (yes|no)' | tail -n 1 || true)"
    case "$IDMAP_LINE" in
      *yes) ok "sysbox ${IDMAP_LINE}" ;;
      *no) missing "sysbox reports no ID-mapped mount support this boot: the docker data-root must be on ext4 or xfs and the kernel 5.19 or newer" ;;
      *) warn "could not read the sysbox-mgr ID-mapped mount line from the journal; the validator will check it in its own session" ;;
    esac
  else
    missing "sysbox-runc runtime not registered with docker (install sysbox-ce ${SYSBOX_MIN_VERSION} and register it in /etc/docker/daemon.json)"
  fi
  RENTALS="$(docker ps -q --filter label=mt.job 2>/dev/null | wc -l | tr -d ' ')"
  [ "${RENTALS:-0}" -eq 0 ] || fail "${RENTALS} pool container(s) are running; wait for them to finish before reinstalling the agent"
fi

if [ "$MISSING" -ne 0 ]; then
  requirements_text
  fail "requirements not met; nothing was changed"
fi

say "verifying sysbox with GPUs"
if ! docker run --rm --runtime=sysbox-runc --gpus all "$PROBE_IMAGE" nvidia-smi -L >"${WORK}/probe.log" 2>&1; then
  tail -n 20 "${WORK}/probe.log" >&2
  cat >&2 <<'TEXT'
docker run --runtime=sysbox-runc --gpus all failed. Collect these before asking for help:
  docker version --format '{{.Server.Version}}'
  docker info --format '{{json .Runtimes}}'
  journalctl -u sysbox-mgr -b --no-pager | tail -n 40
  cat /etc/docker/daemon.json
  nvidia-ctk --version && cat /etc/nvidia-container-runtime/config.toml
TEXT
  fail "GPUs do not work inside sysbox containers on this host"
fi
ok "sysbox GPU probe: $(head -n 1 "${WORK}/probe.log")"

if docker run --rm --storage-opt size=1g "$QUOTA_IMAGE" true >/dev/null 2>&1; then
  ok "per-container storage quotas enforceable"
else
  warn "docker cannot enforce --storage-opt size (overlay2 needs xfs with pquota); the rig will not be eligible for rental until this is fixed"
fi

say "fetching the agent image"
DIGEST="${RIG_AGENT_IMAGE_SHA256:-}"
if [ -z "$DIGEST" ]; then
  DIGEST="$(curl -fsS --max-time 30 "${SERVER_URL}/v1/pool/agent-release" | jq -r '.release.digest // empty' 2>/dev/null || true)"
fi
if [ -n "$DIGEST" ]; then
  echo "$DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "authorised digest is malformed: ${DIGEST}"
  ok "validator signed release ${DIGEST}"
else
  warn "no validator signed release is published yet; starting from the latest published image, the agent moves to the first signed release on its own"
  docker pull "${IMAGE}:latest" >>"$LOG" 2>&1 || { tail -n 20 "$LOG" >&2; fail "could not pull ${IMAGE}:latest"; }
  DIGEST="$(docker image inspect --format '{{index .RepoDigests 0}}' "${IMAGE}:latest" 2>/dev/null | sed 's/.*@//')"
  echo "$DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "could not read the digest of ${IMAGE}:latest"
  ok "latest published image ${DIGEST}"
fi

if [ -f "${TARGET}/docker-compose.yml" ]; then
  say "stopping the existing agent stack"
  (cd "$TARGET" && docker compose down --remove-orphans >/dev/null 2>&1) || true
fi

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
docker compose --env-file .env pull >>"$LOG" 2>&1 || { tail -n 20 "$LOG" >&2; fail "could not pull the agent image ${DIGEST}"; }
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
