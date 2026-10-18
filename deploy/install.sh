#!/usr/bin/env bash
# Install Brewski on a Raspberry Pi and start it on every boot.
#
# From a checkout: sudo ./deploy/install.sh
# Without git, from the Pi:
#   curl -fsSL https://raw.githubusercontent.com/anomaddev/Brewski/redev/deploy/install.sh | sudo bash

set -euo pipefail

GITHUB_REPO=anomaddev/Brewski
DEFAULT_REF=redev
INSTALL_ROOT=/opt/brewski
DATA_DIR=/var/lib/brewski
VENV="${INSTALL_ROOT}/venv"
UNIT_PATH=/etc/systemd/system/brewski.service
PORT=8080

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
      "BREWSKI_REF=${BREWSKI_REF:-}" \
      "$SCRIPT_PATH" "$@"
  fi
  echo "brewski: run this installer as root. Example:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/${GITHUB_REPO}/${DEFAULT_REF}/deploy/install.sh | sudo bash" >&2
  exit 1
fi

die() {
  echo "brewski: $*" >&2
  exit 1
}

service_user() {
  if [[ -n "${BREWSKI_USER:-}" ]]; then
    printf '%s\n' "$BREWSKI_USER"
    return
  fi
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    printf '%s\n' "$SUDO_USER"
    return
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

# Download the branch named by BREWSKI_REF (default redev) and replace this
# process with the install.sh inside that archive. Does not return.
download_and_reexec() {
  local ref dest tarball url status repo
  ref=${BREWSKI_REF:-$DEFAULT_REF}
  [[ "$ref" =~ ^[A-Za-z0-9._/-]+$ && "$ref" != *..* && "$ref" != /* && "$ref" != */ ]] \
    || die "BREWSKI_REF must be a branch name."

  ensure_curl
  dest=$(mktemp -d /tmp/brewski-src.XXXXXX)
  tarball=$dest/brewski.tar.gz
  url="https://api.github.com/repos/${GITHUB_REPO}/tarball/${ref}"
  echo "Downloading ${GITHUB_REPO} (${ref})..."

  status=$(
    curl -sS -L -o "$tarball" -w "%{http_code}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "$url" || true
  )
  if [[ "$status" != "200" ]]; then
    rm -rf "$dest"
    die "could not download ${GITHUB_REPO} at ${ref} (HTTP ${status}). Check BREWSKI_REF."
  fi

  tar -xzf "$tarball" -C "$dest" || {
    rm -rf "$dest"
    die "the download was not a valid archive."
  }
  rm -f "$tarball"

  shopt -s nullglob
  local dirs=("$dest"/*/)
  if [[ ${#dirs[@]} -ne 1 ]]; then
    rm -rf "$dest"
    die "the download did not contain a single project directory."
  fi
  repo=${dirs[0]%/}
  if [[ ! -f "$repo/deploy/install.sh" || ! -f "$repo/pyproject.toml" ]]; then
    rm -rf "$dest"
    die "the ${ref} branch has no deploy/install.sh. Push this installer to that branch, or set BREWSKI_REF."
  fi

  export BREWSKI_SRC_TMP=$dest
  exec "$repo/deploy/install.sh" < /dev/null
  rm -rf "$dest"
  die "could not start the downloaded installer."
}

install_packages() {
  command -v apt-get >/dev/null 2>&1 || die "apt-get was not found. This script installs Brewski on Raspberry Pi OS."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update < /dev/null
  apt-get install -y python3 python3-venv python3-pip nodejs npm ca-certificates < /dev/null
}

build_dashboard() {
  local root=$1
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
  "$VENV/bin/python" -m pip install --upgrade --force-reinstall "$root"
  chown -R "${user}:${group}" "$INSTALL_ROOT" "$DATA_DIR"
  [[ -x "$VENV/bin/brewski" ]] || die "pip install did not create ${VENV}/bin/brewski."
}

install_service() {
  local root=$1
  local user=$2
  local group=$3
  local line
  [[ -f "$root/deploy/brewski.service" ]] || die "deploy/brewski.service is missing."
  command -v systemctl >/dev/null 2>&1 || die "systemd is required so Brewski can start on boot."

  : >"$UNIT_PATH"
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      User=*) line="User=${user}" ;;
      Group=*) line="Group=${group}" ;;
    esac
    printf '%s\n' "$line" >>"$UNIT_PATH"
  done <"$root/deploy/brewski.service"

  systemctl daemon-reload
  systemctl enable brewski
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
  if [[ -n "${BREWSKI_SRC_TMP:-}" ]]; then
    trap 'rm -rf "$BREWSKI_SRC_TMP"' EXIT
  fi

  user=$(service_user)
  id "$user" >/dev/null 2>&1 || die "account ${user} does not exist."
  group=$(id -gn "$user")

  if ! repo_root=$(checkout_root); then
    download_and_reexec
  fi

  echo "Installing Brewski for ${user}:${group}"
  echo "Source: ${repo_root}"
  install_packages
  build_dashboard "$repo_root"
  install_python "$repo_root" "$user" "$group"
  install_service "$repo_root" "$user" "$group"

  echo "Brewski is installed and will start on every reboot."
  echo "Open http://$(lan_address):${PORT}"
  echo "Database: ${DATA_DIR}/brewski.db"
}

main "$@"
