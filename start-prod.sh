#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================================
# ニキ - Production start script (Linux / Ubuntu)
# ============================================================================
# 本番スタック(docker-compose.prod.yml)を起動する。trpg_terminal/start-prod.sh と同作法。
#
# 使い方:
#   bash start-prod.sh            # 既存イメージで起動 (up -d)
#   bash start-prod.sh --build    # 再ビルドして反映 (up -d --build)
#
# 自動起動(再起動後も復帰)は systemd を使う:
#   - Docker Engine:  sudo systemctl enable --now docker
#   - 本スタック:     deploy/systemd/niki-stack.service を導入 (deploy/README.md 参照)
#                     ※ 各サービスは restart: always のため、一度起動すれば再起動後も復帰する。
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
AIVENV_ENV="../vm-play-server/.env"
AIVENV_WORK_DIR="${AIVENV_WORK_DIR:-/opt/aivenv/work}"
DOCKER_MAX_WAIT_SECONDS=300
DOCKER_RETRY_INTERVAL=5

log() { printf "[%s] [%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }

# --- 0. 必須ファイル/ディレクトリ -------------------------------------------
[ -f "$COMPOSE_FILE" ] || { log ERROR "$COMPOSE_FILE が見つかりません。"; exit 1; }
[ -f ".env" ]          || { log ERROR "./.env がありません。本番用の値を設定してください。"; exit 1; }
[ -f "$AIVENV_ENV" ]   || { log ERROR "$AIVENV_ENV がありません（OPENAI_API_KEY / NGROK_AUTHTOKEN）。"; exit 1; }
[ -d "../fishing-mcp" ] || { log ERROR "../fishing-mcp が見つかりません（mcp イメージのビルドに必要）。"; exit 1; }
[ -d "../vm-play-server" ] || { log ERROR "../vm-play-server が見つかりません（aivenv のビルドに必要）。"; exit 1; }

# aivenv がスクリプトを書き出す共有作業ディレクトリ（ホストと同一パス）。
mkdir -p "$AIVENV_WORK_DIR"
export AIVENV_WORK_DIR

# --- 1. Docker daemon の起動を待つ (boot 直後の systemd 起動順対策) -----------
log INFO "Waiting for Docker daemon (max ${DOCKER_MAX_WAIT_SECONDS}s)..."
elapsed=0
until docker info >/dev/null 2>&1; do
  if [ "$elapsed" -ge "$DOCKER_MAX_WAIT_SECONDS" ]; then
    log ERROR "Docker daemon did not become available. Aborting."
    exit 1
  fi
  sleep "$DOCKER_RETRY_INTERVAL"
  elapsed=$((elapsed + DOCKER_RETRY_INTERVAL))
done
log SUCCESS "Docker daemon is ready."

# --- 2. 本番スタックを起動 --------------------------------------------------
if [ "${1:-}" = "--build" ]; then
  log INFO "Starting production stack (up -d --build)..."
  docker compose -f "$COMPOSE_FILE" up -d --build
else
  log INFO "Starting production stack (up -d)..."
  docker compose -f "$COMPOSE_FILE" up -d
fi
log SUCCESS "Production stack started."

docker compose -f "$COMPOSE_FILE" ps
log INFO "start-prod.sh finished."
