#!/usr/bin/env bash
# build_addons.sh — точка входа для загрузки аддонов (дополнений) RRS.
#
# Читает scripts/addonsBuilding/addons.yml и скачивает перечисленные в нём
# репозитории GitHub в build/addons/<имя аддона>. Отчёт о загрузке пишется
# в build/addons/addons.manifest.json, журнал — в build/addons/logs/.
#
# Скрипт одинаково работает на обычной системе и внутри docker-контейнера:
# корень проекта определяется по расположению самого скрипта, а все внешние
# зависимости (git, python3, PyYAML) проверяются при запуске.
#
# Использование:
#   scripts/addonsBuilding/build_addons.sh [параметры addons.py]
#
# Примеры:
#   scripts/addonsBuilding/build_addons.sh                 # все включённые аддоны
#   scripts/addonsBuilding/build_addons.sh --list          # показать конфигурацию
#   scripts/addonsBuilding/build_addons.sh --only tep70    # только один аддон
#   scripts/addonsBuilding/build_addons.sh --jobs 3        # три загрузки параллельно
#   scripts/addonsBuilding/build_addons.sh --reclone       # перекачать заново
#
# В docker-контейнере образ собирается по Dockerfile.ubuntu26.04: git есть
# из стадии deps, python3 и PyYAML установлены в стадии build, поэтому
# достаточно выполнить:
#   docker build -f Dockerfile.ubuntu26.04 -t rrs-ubuntu26.04 .
#   docker run --rm -v "$PWD/build/addons:/src/rrs/build/addons" \
#       rrs-ubuntu26.04 scripts/addonsBuilding/build_addons.sh

set -Eeuo pipefail

# --- Пути ---------------------------------------------------------------
# Скрипт лежит в <корень проекта>/scripts/addonsBuilding/, поэтому корень
# проекта — на два уровня выше каталога скрипта. Это работает и на хосте,
# и в контейнере, где проект смонтирован в /src/rrs.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

CONFIG_FILE="${ADDONS_CONFIG:-${SCRIPT_DIR}/addons.yml}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# --- Журнал -------------------------------------------------------------
LOG_DIR="${PROJECT_ROOT}/build/addons/logs"
mkdir -p -- "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/addons-$(date +%Y%m%d-%H%M%S).log"

# --- Вывод --------------------------------------------------------------
info() { printf '[addons] %s\n' "$*"; }
error() { printf '[addons] ОШИБКА: %s\n' "$*" >&2; }

# --- Проверка окружения -------------------------------------------------
if ! command -v -- "${PYTHON_BIN}" >/dev/null 2>&1; then
    error "не найден интерпретатор ${PYTHON_BIN}. Установите python3: sudo apt install python3"
    exit 2
fi

if ! "${PYTHON_BIN}" -c 'import yaml' >/dev/null 2>&1; then
    error "модуль PyYAML недоступен для ${PYTHON_BIN}. Установите его: sudo apt install python3-yaml"
    error "в docker-контейнере пакет python3-yaml установлен на стадии build (Dockerfile.ubuntu26.04)"
    exit 2
fi

if ! command -v -- git >/dev/null 2>&1; then
    error "не найден git. Установите его: sudo apt install git"
    exit 2
fi

# --- Запуск -------------------------------------------------------------
# Конфигурацию, корень проекта и ошибки проверок печатает сам addons.py:
# он же применяет --config, поэтому дублировать эти строки здесь нельзя,
# иначе журнал расходился бы с фактически используемым конфигом.
info "журнал: ${LOG_FILE}"
info ""

set +e
"${PYTHON_BIN}" "${SCRIPT_DIR}/addons.py" \
    --config "${CONFIG_FILE}" \
    --project-root "${PROJECT_ROOT}" \
    "$@" 2>&1 | tee -a "${LOG_FILE}"
status="${PIPESTATUS[0]}"
set -e

info ""
if [ "${status}" -eq 0 ]; then
    info "загрузка аддонов завершена успешно"
else
    error "загрузка аддонов завершилась с кодом ${status}, подробности: ${LOG_FILE}"
fi

exit "${status}"