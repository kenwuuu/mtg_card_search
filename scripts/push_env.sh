#!/usr/bin/env bash
# Copies the local .env file to a remote server over scp, for step 3 of the
# "Setting up on a new server" flow in SETUP.md. Prompts before overwriting an
# existing remote .env, and locks down permissions on the copy afterward.
#
# Usage: scripts/push_env.sh -h HOST [-d REMOTE_DIR] [-i IDENTITY_FILE] [-p PORT]
#   HOST            SSH destination, e.g. user@1.2.3.4 or a configured ssh
#                    host alias. Required.
#   REMOTE_DIR      Directory on the server containing the repo clone.
#                    Defaults to /root/aura-api/mtg_card_search (see SETUP.md).
#   IDENTITY_FILE   Passed through to ssh/scp as -i.
#   PORT            Passed through to ssh/scp as -p/-P.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOST=""
REMOTE_DIR="/root/aura-api/mtg_card_search"
IDENTITY_FILE=""
PORT=""

while getopts "h:d:i:p:" opt; do
  case "${opt}" in
    h) HOST="${OPTARG}" ;;
    d) REMOTE_DIR="${OPTARG}" ;;
    i) IDENTITY_FILE="${OPTARG}" ;;
    p) PORT="${OPTARG}" ;;
    *) echo "Usage: $0 -h HOST [-d REMOTE_DIR] [-i IDENTITY_FILE] [-p PORT]" >&2; exit 1 ;;
  esac
done

if [ -z "${HOST}" ]; then
  echo "Usage: $0 -h HOST [-d REMOTE_DIR] [-i IDENTITY_FILE] [-p PORT]" >&2
  exit 1
fi

ENV_FILE="${REPO_DIR}/.env"
if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found — create it locally first (see .env.example)." >&2
  exit 1
fi

SCP_ARGS=()
SSH_ARGS=()
if [ -n "${IDENTITY_FILE}" ]; then
  SCP_ARGS+=(-i "${IDENTITY_FILE}")
  SSH_ARGS+=(-i "${IDENTITY_FILE}")
fi
if [ -n "${PORT}" ]; then
  SCP_ARGS+=(-P "${PORT}")
  SSH_ARGS+=(-p "${PORT}")
fi

if ssh "${SSH_ARGS[@]}" "${HOST}" "test -f '${REMOTE_DIR}/.env'" 2>/dev/null; then
  read -r -p "${HOST}:${REMOTE_DIR}/.env already exists — overwrite? [y/N] " reply
  case "${reply}" in
    [yY][eE][sS]|[yY]) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

echo "Copying ${ENV_FILE} to ${HOST}:${REMOTE_DIR}/.env ..."
scp "${SCP_ARGS[@]}" "${ENV_FILE}" "${HOST}:${REMOTE_DIR}/.env"

echo "Restricting permissions on remote .env to 600..."
ssh "${SSH_ARGS[@]}" "${HOST}" "chmod 600 '${REMOTE_DIR}/.env'"

echo "OK: .env pushed to ${HOST}:${REMOTE_DIR}/.env"
