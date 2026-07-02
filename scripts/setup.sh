#!/usr/bin/env bash
# Automates the "Setting up on a new server" steps from README.md.
# Safe to re-run: every step is idempotent (skips what's already done).
#
# Usage: scripts/setup.sh [-y|--yes] [--fetch-data] [--install-service] [--install-cron]
#   -y, --yes           Skip confirmation prompts (still requires --fetch-data /
#                        --install-service / --install-cron explicitly for the
#                        heavier/privileged steps below).
#   --fetch-data        Run data_updater.py to download the initial dataset.
#                        Hits the network and can take a while — opt-in even
#                        under -y.
#   --install-service   Install/refresh the systemd unit (Linux only; skipped
#                        with a message elsewhere).
#   --install-cron      Add the data-refresh cron entry (skipped if `crontab`
#                        isn't available).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

VENV_DIR="${REPO_DIR}/.venv"
ASSUME_YES=0
FETCH_DATA=0
INSTALL_SERVICE=0
INSTALL_CRON=0

for arg in "$@"; do
  case "${arg}" in
    -y|--yes) ASSUME_YES=1 ;;
    --fetch-data) FETCH_DATA=1 ;;
    --install-service) INSTALL_SERVICE=1 ;;
    --install-cron) INSTALL_CRON=1 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 1 ;;
  esac
done

section() { echo; echo "== $1 =="; }

confirm() {
  # confirm "prompt" -> 0 if yes, 1 if no. Always yes under -y.
  local prompt="$1"
  if [ "${ASSUME_YES}" -eq 1 ]; then
    return 0
  fi
  read -r -p "${prompt} [y/N] " reply
  case "${reply}" in
    [yY][eE][sS]|[yY]) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
section "Checking python3 version (need 3.12+)"
# ---------------------------------------------------------------------------
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "ERROR: python3 >= 3.12 is required. Found: $(python3 --version 2>&1)" >&2
  exit 1
fi
echo "OK: $(python3 --version)"

# ---------------------------------------------------------------------------
section "Setting up virtualenv and dependencies"
# ---------------------------------------------------------------------------
if [ ! -d "${VENV_DIR}" ]; then
  echo "Creating venv at ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
else
  echo "venv already exists at ${VENV_DIR}, reusing it"
fi

echo "Installing dependencies..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r requirements.txt
echo "OK: dependencies installed"

# ---------------------------------------------------------------------------
section "Configuring environment variables"
# ---------------------------------------------------------------------------
if [ -f "${REPO_DIR}/.env" ]; then
  echo ".env already exists, leaving it untouched"
else
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
  echo "Created .env from .env.example."
  echo "IMPORTANT: review and edit .env now, especially CORS_ORIGIN for production."
  if ! confirm "Continue once you've reviewed .env?"; then
    echo "Stopping so you can edit .env. Re-run this script when ready."
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
section "Preparing CARD_JSON_DIR"
# ---------------------------------------------------------------------------
CARD_JSON_DIR="$("${VENV_DIR}/bin/python3" -c "from settings import settings; print(settings.card_json_dir)")"
echo "CARD_JSON_DIR resolved to: ${CARD_JSON_DIR}"
mkdir -p "${CARD_JSON_DIR}"
echo "OK: directory ready"

# ---------------------------------------------------------------------------
section "Initial data fetch"
# ---------------------------------------------------------------------------
if [ "${FETCH_DATA}" -eq 1 ]; then
  echo "Running data_updater.py (this can take a while and hits the network)..."
  "${VENV_DIR}/bin/python3" data_updater.py
  echo "OK: initial data fetched"
else
  echo "Skipping (pass --fetch-data to download the initial dataset now)."
  echo "The server will refuse to start until the .ndjson files referenced by"
  echo "BULK_DATA_TYPES exist under ${CARD_JSON_DIR}."
fi

# ---------------------------------------------------------------------------
section "Caddy reverse proxy"
# ---------------------------------------------------------------------------
echo "Not automated — domain/TLS config is environment-specific."
echo "See README.md step 5 for the Caddyfile example and install instructions."

# ---------------------------------------------------------------------------
section "systemd service"
# ---------------------------------------------------------------------------
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found (not a systemd/Linux host) — skipping."
elif [ "${INSTALL_SERVICE}" -eq 1 ]; then
  SERVICE_USER="${SERVICE_USER:-$(whoami)}"
  echo "Rendering deploy/mtg-card-search.service.template with:"
  echo "  REPO_DIR=${REPO_DIR}"
  echo "  VENV_DIR=${VENV_DIR}"
  echo "  SERVICE_USER=${SERVICE_USER}"

  RENDERED_UNIT="$(mktemp)"
  trap 'rm -f "${RENDERED_UNIT}"' EXIT
  if command -v envsubst >/dev/null 2>&1; then
    REPO_DIR="${REPO_DIR}" VENV_DIR="${VENV_DIR}" SERVICE_USER="${SERVICE_USER}" \
      envsubst '${REPO_DIR} ${VENV_DIR} ${SERVICE_USER}' \
      < "${REPO_DIR}/deploy/mtg-card-search.service.template" > "${RENDERED_UNIT}"
  else
    sed -e "s#\${REPO_DIR}#${REPO_DIR}#g" \
        -e "s#\${VENV_DIR}#${VENV_DIR}#g" \
        -e "s#\${SERVICE_USER}#${SERVICE_USER}#g" \
        "${REPO_DIR}/deploy/mtg-card-search.service.template" > "${RENDERED_UNIT}"
  fi

  echo "Installing /etc/systemd/system/mtg-card-search.service (needs sudo)..."
  sudo tee /etc/systemd/system/mtg-card-search.service < "${RENDERED_UNIT}" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable mtg-card-search
  sudo systemctl start mtg-card-search
  echo "OK: mtg-card-search service installed and started"
else
  echo "Skipping (pass --install-service to install/refresh the systemd unit)."
fi

# ---------------------------------------------------------------------------
section "Cron job for data refresh"
# ---------------------------------------------------------------------------
CRON_CMD="${VENV_DIR}/bin/python3 ${REPO_DIR}/data_updater.py >> /var/log/mtg-card-search-updater.log 2>&1"
if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab not found — skipping."
elif [ "${INSTALL_CRON}" -eq 1 ]; then
  EXISTING_CRON="$(crontab -l 2>/dev/null || true)"
  if echo "${EXISTING_CRON}" | grep -qF "${REPO_DIR}/data_updater.py"; then
    echo "Cron entry for this repo's data_updater.py already present — leaving crontab untouched."
  else
    echo "Adding weekly cron entry (Tuesdays 05:00 America/New_York):"
    echo "  0 5 * * 2 ${CRON_CMD}"
    {
      echo "${EXISTING_CRON}"
      echo "TZ=America/New_York"
      echo "0 5 * * 2 ${CRON_CMD}"
    } | crontab -
    echo "OK: cron entry added"
  fi
else
  echo "Skipping (pass --install-cron to add the weekly data-refresh cron entry)."
fi

# ---------------------------------------------------------------------------
section "Health check"
# ---------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet mtg-card-search 2>/dev/null; then
  echo "Service is active, checking http://localhost:8000/v1/health ..."
  curl -sf localhost:8000/v1/health && echo
else
  echo "Server isn't running yet under systemd. Once it's up, verify with:"
  echo "  curl localhost:8000/v1/health"
fi

section "Done"
