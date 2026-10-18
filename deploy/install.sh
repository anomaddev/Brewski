#!/usr/bin/env bash
# Install Brewski on a Raspberry Pi and start it on every boot.
#
# From a checkout: sudo ./deploy/install.sh
# Without git, from the Pi:
#   curl -fsSL https://raw.githubusercontent.com/anomaddev/Brewski/main/deploy/install.sh | sudo bash

set -euo pipefail

GITHUB_REPO=anomaddev/Brewski
GITHUB_REF=main
INSTALL_ROOT=/opt/brewski
DATA_DIR=/var/lib/brewski
VENV="${INSTALL_ROOT}/venv"
UNIT_PATH=/etc/systemd/system/brewski.service
UPDATE_UNIT_PATH=/etc/systemd/system/brewski-update.service
APPLY_UNIT_PATH=/etc/systemd/system/brewski-apply-update.service
REVISION_PATH="${INSTALL_ROOT}/revision"
PORT=8080
BOOT_UPDATE=0
APPLY=0
SERVICE_OWNER=""

# Empty when this script is piped into bash (curl | bash). A path when it is a file.
SCRIPT_PATH=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  case "${BASH_SOURCE[0]}" in
    /dev/fd/*|/proc/self/fd/*) ;;
    *)
      SCRIPT_PATH=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
      ;;
  esac
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if [[ -n "$SCRIPT_PATH" ]]; then
    exec sudo \
      "BREWSKI_USER=${BREWSKI_USER:-}" \
      "$SCRIPT_PATH" "$@"
  fi
  echo "brewski: run this installer as root. Example:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_REF}/deploy/install.sh | sudo bash" >&2
  exit 1
fi

die() {
  echo "brewski: $*" >&2
  exit 1
}

service_user() {
  local existing=""
  if [[ -n "${BREWSKI_USER:-}" ]]; then
    printf '%s\n' "$BREWSKI_USER"
    return
  fi
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    printf '%s\n' "$SUDO_USER"
    return
  fi
  if [[ -f "$UNIT_PATH" ]]; then
    existing=$(sed -n 's/^User=//p' "$UNIT_PATH" | head -n 1)
    if [[ -n "$existing" ]]; then
      printf '%s\n' "$existing"
      return
    fi
  fi
  if id pi >/dev/null 2>&1; then
    printf '%s\n' pi
    return
  fi
  die "could not choose an account. Run as sudo from that account, or set BREWSKI_USER."
}

require_version() {
  local name=$1
  local have=$2
  local need_major=$3
  local need_minor=$4
  local major minor
  IFS=. read -r major minor _ <<<"$have"
  major=${major:-0}
  minor=${minor:-0}
  if (( major < need_major || (major == need_major && minor < need_minor) )); then
    die "${name} ${have} is installed. Brewski needs ${name} ${need_major}.${need_minor} or newer."
  fi
}

checkout_root() {
  [[ -n "$SCRIPT_PATH" ]] || return 1
  local script_dir repo_root
  script_dir=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
  repo_root=$(cd "$script_dir/.." && pwd)
  [[ -f "$repo_root/pyproject.toml" && -f "$repo_root/web/package-lock.json" ]] || return 1
  printf '%s\n' "$repo_root"
}

ensure_curl() {
  command -v curl >/dev/null 2>&1 && return
  command -v apt-get >/dev/null 2>&1 || die "curl is required to download Brewski, and apt-get was not found."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update < /dev/null
  apt-get install -y ca-certificates curl < /dev/null
}

# Latest published release tag.
# Prints the tag and returns 0, returns 2 when no release exists, and returns
# 1 when GitHub cannot be read.
latest_release() {
  local payload status tag
  payload=$(mktemp)
  status=$(
    curl -sS -L --retry 2 --max-time 30 -o "$payload" -w "%{http_code}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" || true
  )
  if [[ "$status" == "404" ]]; then
    rm -f "$payload"
    return 2
  fi
  if [[ "$status" != "200" ]]; then
    rm -f "$payload"
    return 1
  fi
  tag=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])' <"$payload" 2>/dev/null || true)
  rm -f "$payload"
  [[ "$tag" =~ ^[A-Za-z0-9._+-]+$ ]] || return 1
  printf '%s\n' "$tag"
}

# Remember the installed release so a later boot can skip an unchanged one.
save_revision() {
  [[ -n "${RELEASE_TAG:-}" ]] || return 0
  printf '%s\n' "$RELEASE_TAG" >"$REVISION_PATH"
}

# Download the latest release into FETCHED_ROOT. The first argument is 1 when
# a missing release or a failed download should keep the installed version.
fetch_release() {
  local soft=$1
  if [[ "$APPLY" == "1" ]]; then
    write_phase downloading
  fi
  local rc=0 tag="" installed="" dest tarball url status repo
  ensure_curl
  tag=$(latest_release) || rc=$?
  if [[ "$rc" -eq 1 ]]; then
    if [[ "$soft" == "1" ]]; then
      echo "brewski: GitHub is unreachable. Starting the installed version."
      exit 0
    fi
    die "could not reach GitHub to read the latest release."
  fi
  if [[ "$rc" -eq 2 || -z "$tag" ]]; then
    if [[ "$soft" == "1" ]]; then
      echo "brewski: no GitHub release yet. Starting the installed version."
      exit 0
    fi
    die "no GitHub release exists yet. Publish a release, then run this installer again."
  fi
  if [[ -f "$REVISION_PATH" ]]; then
    installed=$(tr -d '[:space:]' <"$REVISION_PATH")
  fi
  if [[ "$installed" == "$tag" ]]; then
    if [[ "$soft" == "1" ]]; then
      echo "brewski: already at release ${tag}."
      exit 0
    fi
    echo "brewski: release ${tag} is already installed. Reinstalling it."
  fi

  dest=$(mktemp -d /tmp/brewski-src.XXXXXX)
  BREWSKI_SRC_TMP=$dest
  tarball=$dest/brewski.tar.gz
  url="https://api.github.com/repos/${GITHUB_REPO}/tarball/${tag}"
  echo "Downloading ${GITHUB_REPO} release ${tag}..."

  status=$(
    curl -sS -L -o "$tarball" -w "%{http_code}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "$url" || true
  )
  if [[ "$status" != "200" ]]; then
    rm -rf "$dest"
    BREWSKI_SRC_TMP=""
    if [[ "$soft" == "1" ]]; then
      echo "brewski: could not download release ${tag} (HTTP ${status}). Starting the installed version."
      exit 0
    fi
    die "could not download release ${tag} (HTTP ${status})."
  fi

  if ! tar -xzf "$tarball" -C "$dest"; then
    rm -rf "$dest"
    BREWSKI_SRC_TMP=""
    if [[ "$soft" == "1" ]]; then
      echo "brewski: the download was not a valid archive. Starting the installed version."
      exit 0
    fi
    die "the download was not a valid archive."
  fi
  rm -f "$tarball"

  shopt -s nullglob
  local dirs=("$dest"/*/)
  if [[ ${#dirs[@]} -ne 1 ]]; then
    rm -rf "$dest"
    BREWSKI_SRC_TMP=""
    die "the download did not contain a single project directory."
  fi
  repo=${dirs[0]%/}
  if [[ ! -f "$repo/deploy/install.sh" || ! -f "$repo/pyproject.toml" ]]; then
    rm -rf "$dest"
    BREWSKI_SRC_TMP=""
    die "release ${tag} has no deploy/install.sh."
  fi

  RELEASE_TAG=$tag
  FETCHED_ROOT=$repo
}

install_packages() {
  command -v apt-get >/dev/null 2>&1 || die "apt-get was not found. This script installs Brewski on Raspberry Pi OS."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update < /dev/null
  apt-get install -y python3 python3-venv python3-pip nodejs npm ca-certificates i2c-tools < /dev/null
}

enable_i2c() {
  if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 || true
  fi
}

add_device_groups() {
  local user=$1
  local group
  for group in i2c gpio; do
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$user" || true
    fi
  done
}

write_sudoers() {
  local user=$1
  local systemctl
  systemctl=$(command -v systemctl)
  [[ -n "$systemctl" ]] || die "systemctl was not found."
  cat > /etc/sudoers.d/brewski <<EOF
${user} ALL=(root) NOPASSWD: ${systemctl} start --no-block brewski-apply-update.service
EOF
  chmod 440 /etc/sudoers.d/brewski
  visudo -cf /etc/sudoers.d/brewski >/dev/null
}

write_phase() {
  local phase=$1
  local error=${2:-}
  local file=${BREWSKI_STATUS_FILE:-${DATA_DIR}/update-status.json}
  PHASE="$phase" ERROR="$error" FILE="$file" OWNER="$SERVICE_OWNER" python3 - <<'PY' || true
import json, os
path = os.environ["FILE"]
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)
doc = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            doc = loaded
    except Exception:
        doc = {}
doc["phase"] = os.environ["PHASE"]
doc["error"] = os.environ["ERROR"] or None
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(doc, handle)
os.replace(temporary, path)
os.chmod(path, 0o644)
owner = os.environ.get("OWNER") or ""
if ":" in owner:
    import shutil
    user, group = owner.split(":", 1)
    try:
        shutil.chown(path, user, group)
    except Exception:
        pass
PY
}

build_dashboard() {
  local root=$1
  if [[ "$APPLY" == "1" ]]; then
    write_phase building
  fi
  [[ -f "$root/web/package-lock.json" ]] || die "web/package-lock.json is missing."
  command -v node >/dev/null 2>&1 || die "Node.js did not install a node command."
  local node_version
  node_version=$(node -p 'process.versions.node')
  require_version "Node.js" "$node_version" 18 0
  npm ci --prefix "$root/web" < /dev/null
  npm run build --prefix "$root/web" < /dev/null
  [[ -f "$root/web/dist/index.html" ]] || die "the dashboard build did not produce web/dist/index.html."
}

install_python() {
  local root=$1
  local user=$2
  local group=$3
  if [[ "$APPLY" == "1" ]]; then
    write_phase installing
  fi
  local py_version
  py_version=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  require_version "Python" "$py_version" 3 10

  mkdir -p "$INSTALL_ROOT" "$DATA_DIR"
  if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/python" -m pip install --upgrade pip
  # Force a reinstall so a repeat run picks up code even when the version
  # number in pyproject.toml has not changed.
  "$VENV/bin/python" -m pip install --upgrade --force-reinstall "${root}[pi]"
  chown -R "${user}:${group}" "$VENV" "$DATA_DIR"
  [[ -x "$VENV/bin/brewski" ]] || die "pip install did not create ${VENV}/bin/brewski."
}

install_service() {
  local root=$1
  local user=$2
  local group=$3
  local line
  [[ -f "$root/deploy/brewski.service" ]] || die "deploy/brewski.service is missing."
  [[ -f "$root/deploy/brewski-update.service" ]] || die "deploy/brewski-update.service is missing."
  [[ -f "$root/deploy/brewski-apply-update.service" ]] || die "deploy/brewski-apply-update.service is missing."
  command -v systemctl >/dev/null 2>&1 || die "systemd is required so Brewski can start on boot."

  : >"$UNIT_PATH"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      User=*) line="User=${user}" ;;
      Group=*) line="Group=${group}" ;;
    esac
    printf '%s\n' "$line" >>"$UNIT_PATH"
  done <"$root/deploy/brewski.service"
  cp "$root/deploy/brewski-update.service" "$UPDATE_UNIT_PATH"
  cp "$root/deploy/brewski-apply-update.service" "$APPLY_UNIT_PATH"
  install -m 755 "$root/deploy/install.sh" "${INSTALL_ROOT}/install.sh"
  write_sudoers "$user"

  systemctl daemon-reload
  systemctl enable brewski brewski-update
  if [[ "$BOOT_UPDATE" == "1" || "$APPLY" == "1" ]]; then
    return
  fi
  systemctl restart brewski
  if ! systemctl is-active --quiet brewski; then
    echo "brewski: the service did not stay running. Recent logs:" >&2
    journalctl -u brewski -n 40 --no-pager >&2 || true
    exit 1
  fi
}

lan_address() {
  local ip=""
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  if [[ -n "$ip" ]]; then
    printf '%s\n' "$ip"
  else
    printf '%s\n' "<pi-address>"
  fi
}

main() {
  local user group repo_root
  if [[ "${1:-}" == "--boot-update" ]]; then
    if [[ -x "${VENV}/bin/brewski" ]]; then
      export BREWSKI_DATA_DIR="${BREWSKI_DATA_DIR:-$DATA_DIR}"
      exec "${VENV}/bin/brewski" boot-update
    fi
    BOOT_UPDATE=1
  elif [[ "${1:-}" == "--apply" ]]; then
    APPLY=1
  fi
  trap '[[ -n "${BREWSKI_SRC_TMP:-}" ]] && rm -rf "$BREWSKI_SRC_TMP"' EXIT

  user=$(service_user)
  id "$user" >/dev/null 2>&1 || die "account ${user} does not exist."
  group=$(id -gn "$user")
  SERVICE_OWNER="${user}:${group}"

  if ! repo_root=$(checkout_root); then
    fetch_release "$BOOT_UPDATE"
    repo_root=$FETCHED_ROOT
  fi

  if [[ "$APPLY" == "1" || "$BOOT_UPDATE" == "1" ]]; then
    echo "Updating Brewski for ${user}:${group}"
  else
    echo "Installing Brewski for ${user}:${group}"
  fi
  echo "Source: ${repo_root}"
  if [[ "$APPLY" != "1" && "$BOOT_UPDATE" != "1" ]] || ! command -v python3 >/dev/null || ! command -v node >/dev/null || ! command -v npm >/dev/null; then
    install_packages
  fi
  enable_i2c
  add_device_groups "$user"
  build_dashboard "$repo_root"
  install_python "$repo_root" "$user" "$group"
  install_service "$repo_root" "$user" "$group"
  save_revision "$repo_root"

  if [[ "$APPLY" == "1" ]]; then
    write_phase complete
    echo "Brewski is updated."
    return
  fi
  if [[ "$BOOT_UPDATE" == "1" ]]; then
    echo "Brewski is updated and will start next."
    return
  fi
  echo "Brewski is installed and will start on every reboot."
  echo "A fresh boot does not look for an update until setup has saved the I2C pins."
  echo "After that, each boot asks on the LCD before installing a newer GitHub release."
  echo "Open http://$(lan_address):${PORT}"
  echo "Database: ${DATA_DIR}/brewski.db"
}

main "$@"
