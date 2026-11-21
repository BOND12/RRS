#!/usr/bin/env bash
# =============================================================================
# build_addons.sh — загрузка и установка внешних аддонов RRS.
#
# Скрипт передаёт свои аргументы цели addons из scripts/Makefile.linux
# (переменная ADDONS_FLAGS); список аддонов и их параметры описаны в addons.yml.
# Всё скачивается в external_addons/ (каталог в .gitignore), аддоны, помеченные
# add_to_cmake: true, подключаются через addons/CMakeLists.txt.
#
# Установка зависимостей скрипта:
#   Убунту 26.04+: sudo apt install python3 python3-yaml git
#   иные системы:    python3 -> python 3
#
# Использование:
#   ./scripts/addonsBuilding/build_addons.sh                    # все аддоны
#   ./scripts/addonsBuilding/build_addons.sh tep70              # только tep70
#   ./scripts/addonsBuilding/build_addons.sh --list             # что доступно
#   ./scripts/addonsBuilding/build_addons.sh --dry-run          # без загрузки
#   ./scripts/addonsBuilding/build_addons.sh --skip tem15 --skip ra3
#   ADDONS_CONFIG=/path/to/addons.yml ./scripts/addonsBuilding/build_addons.sh
#
# Флаги addons.py (--only, --skip, --jobs, --reclone, --dry-run, --list) можно
# передавать как обычно; синонимы: позиционные аргументы = --only.
#
# Кодировка: UTF-8. Комментарии — на русском.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MAKEFILE="${ROOT_DIR}/scripts/Makefile.linux"

if ! command -v make >/dev/null 2>&1; then
    echo "Ошибка: не найдена утилита make." >&2
    echo "Установите её пакетом дистрибутива: sudo apt install build-essential make" >&2
    exit 127
fi

# Позиционные аргументы трактуются как --only, как это делал прежний скрипт.
# ADDONS_FLAGS: аргументы передаются make одной переменной; кавычки внутри
# неё make не поддерживает, поэтому значения с пробелами не используются.
if [ "$#" -gt 0 ]; then
    exec make --no-print-directory -C "${ROOT_DIR}" -f "${MAKEFILE}" addons ADDONS_FLAGS="$*"
fi

exec make --no-print-directory -C "${ROOT_DIR}" -f "${MAKEFILE}" addons