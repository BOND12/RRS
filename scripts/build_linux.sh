#!/usr/bin/env bash
# =============================================================================
# build_linux.sh — обёртка над scripts/Makefile.linux.
#
# Скрипт только проверяет наличие утилиты make и передаёт ему все параметры;
# описание целей сборки (зависимости, симулятор, дистрибутив, аддоны, очистка)
# целиком находится в scripts/Makefile.linux.
#
# Установка зависимостей (build-essential, cmake, ninja, qt6-*, libvulkan-dev,
# glslang-dev, libopenal-dev, liblua5.4-dev, libassimp-dev, libfreetype-dev,
# zlib1g-dev, libpng-dev, libx11-dev, libxcb1-dev, libxkbcommon-x11-dev,
# libwayland-dev, libxrandr-dev, libxcursor-dev, libxi-dev, libudev-dev,
# pkg-config, python3, python3-yaml, libsfml-dev, sol2-dev) — в документации:
#   docs/HOWTOBUILD_Linux.md
#
# Использование (те же цели, что у make):
#   ./scripts/build_linux.sh                    # все этапы сборки (цель all)
#   ./scripts/build_linux.sh simulator          # сборка симулятора
#   ./scripts/build_linux.sh simulator BUILD_TYPE=Debug
#   ./scripts/build_linux.sh dist               # сборка и дистрибутив в dist/
#   ./scripts/build_linux.sh dist DIST_DIR=/tmp/rrs-dist
#   ./scripts/build_linux.sh addons             # загрузка и сборка аддонов
#   ./scripts/build_linux.sh addons ADDONS_FLAGS=--only\ tep70
#   ./scripts/build_linux.sh dist-addons        # аддоны в dist/
#   ./scripts/build_linux.sh clean              # полная очистка
#   ./scripts/build_linux.sh clean-artifacts    # очистка build и dist
#
# Список целей и переменных — в комментарии в начале scripts/Makefile.linux.
#
# Кодировка: UTF-8. Комментарии — на русском.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAKEFILE="${SCRIPT_DIR}/Makefile.linux"

if ! command -v make >/dev/null 2>&1; then
    echo "Ошибка: не найдена утилита make." >&2
    echo "Установите её пакетом дистрибутива: sudo apt install build-essential make" >&2
    exit 127
fi

# -C ROOT_DIR: цели работают независимо от текущего каталога.
# Все параметры командной строки передаются make без изменений.
exec make --no-print-directory -C "${ROOT_DIR}" -f "${MAKEFILE}" "$@"