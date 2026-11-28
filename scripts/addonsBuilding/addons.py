#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
addons.py — загрузка и сборка аддонов (дополнений) RRS согласно addons.yml.

Скрипт читает конфигурацию, клонирует перечисленные в ней репозитории
в каталог назначения (по умолчанию external_addons/<имя аддона>), по запросу
собирает их через cmake и пишет манифест external_addons/addons.manifest.json.

Работает одинаково на обычной системе и внутри docker-контейнера: все пути
вычисляются от корня проекта, внешние зависимости — только git, python3
с модулем PyYAML и cmake (нужен только для сборки).

Примеры запуска:
    python3 scripts/addonsBuilding/addons.py                 # все включённые аддоны
    python3 scripts/addonsBuilding/addons.py --list          # показать конфигурацию
    python3 scripts/addonsBuilding/addons.py --only tep70    # только один аддон
    python3 scripts/addonsBuilding/addons.py --skip chs2t    # все, кроме chs2t
    python3 scripts/addonsBuilding/addons.py --jobs 3        # три загрузки параллельно
    python3 scripts/addonsBuilding/addons.py --reclone       # перекачать заново
    python3 scripts/addonsBuilding/addons.py --build --sdk dist/sdk \
        --prefix-path .deps/install                         # скачать и собрать
    python3 scripts/addonsBuilding/addons.py --dry-run       # ничего не менять

Коды возврата:
    0 — прогон завершён: все аддоны загружены либо часть пропущена
        (по каждому пропущенному в конце печатается отчёт с причиной);
    1 — не загружен ни один из выбранных аддонов;
    2 — ошибка конфигурации или окружения (нет git/PyYAML, битый YAML).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - проверяется в build_addons.sh
    sys.stderr.write(
        "[addons] ОШИБКА: не найден модуль PyYAML.\n"
        "[addons] Установите его: sudo apt install python3-yaml\n"
        "[addons] В docker-контейнере он установлен на стадии build (Dockerfile.ubuntu26.04).\n"
    )
    sys.exit(2)


# Статусы аддона в манифесте
STATUS_OK = "ok"            # загружен или уже был загружен
STATUS_FAILED = "failed"    # загрузить не удалось
STATUS_SKIPPED = "skipped"  # пропущен намеренно (dry-run и т.п.)

# Статусы сборки аддона (заполняются только при --build)
BUILD_OK = "built"          # собрано без ошибок
BUILD_FAILED = "failed"     # сборка не удалась — аддон пропущен
BUILD_SKIPPED = "skipped"   # сборка не выполнялась (или пропущена намеренно)

# Вывод из разных потоков не должен перемешиваться построчно
_PRINT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
#  Вспомогательные функции
# ---------------------------------------------------------------------------

def log(message: str = "") -> None:
    """Печать строки состояния в stdout без буферизации."""
    with _PRINT_LOCK:
        print(message, flush=True)


def fail(message: str) -> None:
    """Печать ошибки в stderr."""
    sys.stderr.write(f"[addons] ОШИБКА: {message}\n")


def first_line(text: str, limit: int = 160) -> str:
    """
    Краткая суть многострочного сообщения — для отчёта.

    cmake и компилятор выдают десятки строк диагностики, среди которых
    встречаются и исходная строка, и указатели «^~~~~». В отчёте нужна только
    суть, поэтому строка с «error:» ищется явно, а путь к файлу сокращается до
    имени файла. Длинный текст обрезается, чтобы отчёт оставался читаемым.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if "error:" in line:
            place, _, message = line.partition("error:")
            # Из адреса вида /путь/файл.cpp:79:81 оставить файл и строку.
            head = place.split()[0] if place.split() else ""
            parts = head.replace("\\", "/").split("/")
            location = parts[-1].rstrip(":") if parts else ""
            message = message.strip()
            return f"{location}: {message}"[:limit] if location else message[:limit]
    return lines[0][:limit] if lines else ""


def run_git(args: list[str], *, cwd: Path | None = None, timeout: int,
            retries: int, clean_paths: list[Path] | None = None) -> str:
    """
    Выполнить git с повторами при сетевых сбоях.

    clean_paths — служебные каталоги (например, каталог клона), которые нужно
    удалить перед каждой попыткой: прерванный по таймауту git clone оставляет
    после себя неполный каталог, и повторная попытка без уборки падает с
    сообщением «целевой путь уже существует».

    Возвращает stdout при успехе, иначе поднимает RuntimeError с текстом
    последней ошибки.
    """
    command = ["git", *args]
    last_error = "неизвестная ошибка"

    for attempt in range(1, max(1, retries) + 1):
        if clean_paths:
            for path in clean_paths:
                shutil.rmtree(path, ignore_errors=True)
        try:
            process = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_error = f"таймаут {timeout} с"
        except FileNotFoundError:
            raise RuntimeError("git не найден в PATH")
        else:
            if process.returncode == 0:
                return process.stdout.strip()
            output = (process.stderr or process.stdout or "").strip()
            last_error = output.splitlines()[-1] if output else f"код возврата {process.returncode}"

        if attempt < max(1, retries):
            delay = min(2 ** attempt, 10)
            log(f"    попытка {attempt} не удалась ({last_error}), повтор через {delay} с")
            time.sleep(delay)

    raise RuntimeError(last_error)


def normalize_url(url: str, protocol: str) -> str:
    """
    Привести адрес репозитория к заданному протоколу.

    https://host/owner/repo(.git) ⇄ git@host:owner/repo.git
    """
    url = url.strip()
    if protocol == "ssh":
        if url.startswith("https://"):
            host, _, path = url[len("https://"):].partition("/")
            if host and path:
                if not path.endswith(".git"):
                    path += ".git"
                return f"git@{host}:{path}"
        return url
    if url.startswith("git@") and ":" in url:
        host, _, path = url[len("git@"):].partition(":")
        return f"https://{host}/{path}"
    return url


def resolve_path(value: str, project_root: Path) -> Path:
    """Разрешить путь из конфига относительно корня проекта."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path)


def addon_branches(addon: dict, defaults: dict) -> list[str]:
    """
    Ветки аддона в порядке попыток: первая — основная, остальные — запасные.

    Часть аддонов собирается только под определённую версию RRS, и в upstream
    такие ветки названы по версии (например devel-for-1.10.0). Список берётся
    из поля branches; поле branch (одна ветка) по-прежнему поддерживается,
    поэтому старые конфигурации работают без изменений.
    """
    branches = addon.get("branches")
    if isinstance(branches, (list, tuple)):
        ordered = [str(item).strip() for item in branches if str(item).strip()]
        if ordered:
            return ordered
    branch = str(addon.get("branch") or "").strip()
    return [branch] if branch else []


def addon_destination(addon: dict, defaults: dict, project_root: Path) -> Path:
    """Итоговый каталог аддона: <destination_root>/<name> либо явный destination."""
    if addon.get("destination"):
        return resolve_path(str(addon["destination"]), project_root)
    root = resolve_path(str(defaults.get("destination_root", "addons")), project_root)
    return root / str(addon["name"])


def addon_settings(addon: dict, defaults: dict, section: str) -> dict:
    """Настройки аддона из указанной секции с учётом значений по умолчанию."""
    merged = dict(defaults.get(section) or {})
    merged.update(addon.get(section) or {})
    return merged


def git_revision(path: Path) -> str:
    """Короткий хеш коммита в каталоге (пустая строка, если это не git-репозиторий)."""
    try:
        return run_git(["rev-parse", "--short", "HEAD"], cwd=path, timeout=30, retries=1)
    except RuntimeError:
        return ""


def detect_project_root(script_dir: Path) -> Path:
    """Корень проекта: по git-репозиторию скрипта, иначе на два уровня выше."""
    try:
        top_level = run_git(["rev-parse", "--show-toplevel"], cwd=script_dir, timeout=30, retries=1)
        if top_level:
            return Path(top_level)
    except RuntimeError:
        pass
    return script_dir.parent.parent


# ---------------------------------------------------------------------------
#  Загрузка одного аддона
# ---------------------------------------------------------------------------

def download_addon(addon: dict, defaults: dict, project_root: Path,
                   *, reclone: bool, dry_run: bool) -> dict:
    """
    Загрузить один аддон.

    Возвращает запись манифеста: имя, адрес, ветку, коммит, путь, статус.
    """
    name = str(addon.get("name") or "").strip()
    result = {
        "name": name,
        "url": addon.get("url", ""),
        "branch": addon.get("branch", ""),
        "commit": "",
        "destination": "",
        "license": addon.get("license", ""),
        "status": STATUS_SKIPPED,
        "error": "",
    }

    if not name:
        result.update(status=STATUS_FAILED, error="в конфигурации аддона отсутствует поле name")
        return result

    clone_cfg = addon_settings(addon, defaults, "clone")
    timeout = int(clone_cfg.get("timeout_sec", 600))
    retries = int(clone_cfg.get("retries", 3))
    depth = int(clone_cfg.get("depth", 1))
    submodules = bool(clone_cfg.get("recurse_submodules", False))

    destination = addon_destination(addon, defaults, project_root)
    try:
        result["destination"] = str(destination.relative_to(project_root))
    except ValueError:
        result["destination"] = str(destination)

    log(f"  {name}: {addon.get('url', '')} "
        f"[{addon.get('branch') or 'ветка по умолчанию'} → {result['destination']}]")

    if dry_run:
        return result

    url = normalize_url(str(addon.get("url") or ""), str(clone_cfg.get("protocol", "https")))
    if not url:
        result.update(status=STATUS_FAILED, error="в конфигурации аддона отсутствует поле url")
        return result

    branch = (addon.get("branch") or "").strip() or None
    commit = (addon.get("commit") or "").strip() or None

    # Уже загружен: без --reclone сообщаем текущую ревизию и ничего не делаем.
    if destination.exists() and not reclone:
        revision = git_revision(destination)
        if revision:
            result.update(status=STATUS_OK, commit=revision)
            log(f"    уже загружен (коммит {revision}), обновление не запрашивалось")
            return result
        log("    каталог без git-метаданных, будет загружен заново")
        shutil.rmtree(destination, ignore_errors=True)

    if reclone and destination.exists():
        log("    --reclone: существующий каталог удалён")
        shutil.rmtree(destination, ignore_errors=True)

    # Клон сначала загружается в служебный каталог и лишь затем переносится
    # в build/addons/<имя> — так прерванная загрузка не оставляет полуфабрикат.
    cache_dir = resolve_path(str(defaults.get("cache_root", ".addons-cache")), project_root) \
        / f"{name}.clone"
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.parent.mkdir(parents=True, exist_ok=True)

    clone_args = ["clone", "--quiet"]
    if depth > 0:
        clone_args += ["--depth", str(depth)]
    if branch:
        clone_args += ["--branch", branch]
    if submodules:
        clone_args.append("--recurse-submodules")
    clone_args += [url, str(cache_dir)]

    log("    клонирование...")
    try:
        run_git(clone_args, cwd=project_root, timeout=timeout, retries=retries,
                clean_paths=[cache_dir])
    except RuntimeError as error:
        shutil.rmtree(cache_dir, ignore_errors=True)
        result.update(status=STATUS_FAILED, error=f"git clone: {error}")
        log(f"    не удалось: {error}")
        return result

    revision = git_revision(cache_dir)

    # Фиксация на конкретном коммите (если задана в конфиге).
    if commit:
        try:
            run_git(["fetch", "--depth", "1", "origin", commit],
                    cwd=cache_dir, timeout=timeout, retries=retries)
            run_git(["checkout", "--quiet", commit], cwd=cache_dir, timeout=timeout, retries=retries)
            revision = git_revision(cache_dir)
        except RuntimeError as error:
            shutil.rmtree(cache_dir, ignore_errors=True)
            result.update(status=STATUS_FAILED, error=f"git checkout {commit}: {error}")
            log(f"    не удалось зафиксировать коммит {commit}: {error}")
            return result

    subdirectory = (addon.get("subdirectory") or "").strip()
    if subdirectory:
        source = cache_dir / subdirectory
        if not source.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            result.update(status=STATUS_FAILED,
                          error=f"в репозитории нет каталога {subdirectory}")
            log(f"    не удалось: в репозитории нет каталога {subdirectory}")
            return result
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        shutil.rmtree(cache_dir, ignore_errors=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cache_dir), str(destination))

    result.update(status=STATUS_OK, commit=revision or git_revision(destination))
    log(f"    готово (коммит {result['commit']})")
    return result


def download_addon_safe(addon: dict, defaults: dict, project_root: Path,
                        *, reclone: bool, dry_run: bool) -> dict:
    """
    Загрузка одного аддона без права уронить весь прогон.

    Аддоны в конфигурации заданы извне (ссылки могут устареть, каталог — быть
    занятым, диск — заполниться), поэтому любая нештатная ситуация превращается
    в статус failed с причиной. Остальные аддоны обрабатываются, итог печатается
    в отчёте: так сборка не останавливается из-за одного недоступного репозитория.
    """
    name = str(addon.get("name") or "?")
    try:
        return download_addon(addon, defaults, project_root,
                              reclone=reclone, dry_run=dry_run)
    except Exception as error:  # общий перехват намеренный: аддон должен быть пропущен
        destination = ""
        try:
            destination = str(addon_destination(addon, defaults, project_root))
        except Exception:
            pass
        reason = str(error) or error.__class__.__name__
        log(f"    не удалось: {reason}")
        return {
            "name": name,
            "url": addon.get("url", ""),
            "branch": addon.get("branch", ""),
            "commit": "",
            "destination": destination,
            "license": addon.get("license", ""),
            "status": STATUS_FAILED,
            "error": reason,
        }


def switch_branch(addon: dict, defaults: dict, project_root: Path,
                  branch: str) -> dict:
    """
    Переключить уже загруженный аддон на другую ветку.

    Загрузка «с нуля» не используется намеренно: у аддонов ветки ответвлены от
    одной истории, повторный clone при медленном канале занимает десятки минут.
    Fetch в тот же каталог переносит только новые объекты.

    Результат: {"status": ok|failed, "commit": sha, "error": причина}.
    """
    destination = addon_destination(addon, defaults, project_root)
    if not (destination / ".git").is_dir():
        return {"status": STATUS_FAILED, "commit": "",
                "error": "каталог аддона не является git-репозиторием"}

    clone_cfg = addon_settings(addon, defaults, "clone")
    timeout = int(clone_cfg.get("timeout_sec", 600))
    retries = int(clone_cfg.get("retries", 3))
    depth = int(clone_cfg.get("depth", 1))

    try:
        run_git(["-C", str(destination), "fetch", "--force", f"--depth={depth}",
                 "origin", branch], cwd=destination, timeout=timeout, retries=retries)
        run_git(["-C", str(destination), "checkout", "--force", "--detach",
                 "FETCH_HEAD"], cwd=destination, timeout=timeout, retries=retries)
    except RuntimeError as error:
        return {"status": STATUS_FAILED, "commit": "", "error": str(error)}

    return {"status": STATUS_OK, "commit": git_revision(destination), "error": ""}


# ---------------------------------------------------------------------------
#  Сборка аддонов
# ---------------------------------------------------------------------------
#  Аддон — самостоятельный CMake-проект: он ищет установленный RRS через
#  find_package(RRS), поэтому собирать его можно только после цели dist
#  (там выкладывается dist/sdk с RRSConfig.cmake). Результат сборки —
#  разделяемые библиотеки в <аддон>/modules/<цель>/*.so.

def find_rrs_config(prefix_paths: list[str]) -> Path | None:
    """
    Поиск RRSConfig.cmake среди путей CMAKE_PREFIX_PATH.

    RRS кладёт config-файл прямо в <префикс>/lib/cmake/RRSConfig.cmake
    (вариант с подкаталогом RRS и раскладка lib64 тоже проверяются).

    Возвращается каталог с файлом: cmake не находит такой файл через
    CMAKE_PREFIX_PATH, потому что ожидает подкаталог с именем пакета,
    поэтому путь передаётся отдельно — переменной RRS_DIR.
    """
    for base in prefix_paths:
        for libdir in ("lib", "lib64"):
            for relative in ("cmake/RRS", "cmake"):
                candidate = Path(base) / libdir / relative / "RRSConfig.cmake"
                if candidate.is_file():
                    return candidate.parent
    return None


def run_cmake(command: list[str], *, timeout: int) -> tuple[bool, str]:
    """
    Запуск cmake с разбором результата. Возвращает (успех, текст ошибки).

    При неудаче в текст попадает хвост вывода cmake (последние строки содержат
    собственно ошибку компиляции), чтобы причина сборки была видна в отчёте.
    """
    try:
        process = subprocess.run(command, cwd=None, capture_output=True,
                                 text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"таймаут {timeout} с"
    except FileNotFoundError:
        return False, "cmake не найден в PATH"

    if process.returncode == 0:
        return True, ""

    output = "\n".join((process.stderr or "").splitlines() + (process.stdout or "").splitlines())
    lines = [line for line in output.splitlines() if line.strip()]
    return False, "\n".join(lines[-25:]) or f"код возврата {process.returncode}"


def build_addon(addon: dict, defaults: dict, project_root: Path,
                *, prefix_paths: list[str], generator: str, jobs: int,
                build_type: str, timeout: int) -> dict:
    """
    Сборка одного аддона через cmake.

    Никогда не бросает исключение: любая проблема возвращается как
    BUILD_FAILED/BUILD_SKIPPED с причиной, чтобы остальные аддоны были собраны,
    а итог напечатан в отчёте.
    """
    name = str(addon.get("name") or "?")
    settings = addon_settings(addon, defaults, "build")
    result = {"status": BUILD_SKIPPED, "error": ""}

    destination = addon_destination(addon, defaults, project_root)
    if not destination.is_dir():
        result["error"] = "каталог аддона отсутствует (загрузка не удалась)"
        return result

    cmake = shutil.which("cmake")
    if cmake is None:
        result.update(status=BUILD_FAILED, error="cmake не найден в PATH")
        return result

    rrs_dir = find_rrs_config(prefix_paths)
    if rrs_dir is None:
        result["error"] = ("SDK RRS не найден среди путей поиска "
                           f"({os.pathsep.join(prefix_paths)}) — сначала нужна цель dist")
        return result

    # Каталог сборки — внутри каталога аддона: external_addons целиком
    # игнорируется git и удаляется целью clean, поэтому мусор не мешает.
    build_dir = destination / "build"
    prefix = os.pathsep.join(str(path) for path in prefix_paths)

    log(f"    SDK: {rrs_dir}")
    log("    настройка cmake...")

    configure = [
        cmake, "-S", str(destination), "-B", str(build_dir),
        f"-DCMAKE_BUILD_TYPE={build_type}",
        f"-DCMAKE_PREFIX_PATH={prefix}",
        f"-DRRS_DIR={rrs_dir}",
    ]
    if generator:
        configure += ["-G", generator]
    extra = settings.get("cmake_args") or []
    if isinstance(extra, (list, tuple)):
        configure += [str(item) for item in extra]

    ok, error = run_cmake(configure, timeout=timeout)
    if not ok:
        result.update(status=BUILD_FAILED, error=f"cmake: {error}")
        return result

    log(f"    сборка ({jobs} задач)...")
    ok, error = run_cmake([cmake, "--build", str(build_dir), "-j", str(jobs)],
                          timeout=timeout)
    if not ok:
        result.update(status=BUILD_FAILED, error=f"сборка: {error}")
        return result

    result.update(status=BUILD_OK, error="")
    log("    собрано")
    return result


# ---------------------------------------------------------------------------
#  Манифест и печать конфигурации
# ---------------------------------------------------------------------------

def write_manifest(defaults: dict, project_root: Path, config_path: Path,
                   entries: list[dict]) -> Path:
    """Записать манифест рядом с аддонами (по умолчанию external_addons/addons.manifest.json)."""
    destination_root = resolve_path(str(defaults.get("destination_root", "build/addons")),
                                   project_root)
    destination_root.mkdir(parents=True, exist_ok=True)
    manifest_path = destination_root / "addons.manifest.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": str(config_path),
        "addons": entries,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest_path


def print_list(config: dict, project_root: Path) -> None:
    """Печать конфигурации аддонов в виде таблицы."""
    defaults = config.get("defaults") or {}
    log(f"Версия схемы: {config.get('version')}")
    log(f"destination_root: {defaults.get('destination_root', 'addons')}")
    log("")
    log(f"{'ИМЯ':<14} {'ВЕТКА':<8} {'СБОРКА':<7} КАТАЛОГ")
    for addon in config["addons"]:
        build = addon_settings(addon, defaults, "build")
        destination = addon_destination(addon, defaults, project_root)
        branches = addon_branches(addon, defaults)
        # В таблице показывается основная ветка и число запасных: список веток
        # нужен, когда аддон собирается только под конкретную версию RRS.
        branch = branches[0] if branches else str(addon.get("branch", "-"))
        if len(branches) > 1:
            branch = f"{branch} (+{len(branches) - 1})"
        log(f"{str(addon.get('name', '?')):<14} "
            f"{branch:<8} "
            f"{('да' if build.get('enabled', True) else 'нет'):<7} "
            f"{destination}")


# ---------------------------------------------------------------------------
#  Основная логика
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    """Прочитать и проверить конфигурацию аддонов."""
    if not config_path.is_file():
        raise ValueError(f"файл конфигурации не найден: {config_path}")

    with config_path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}

    if not isinstance(data, dict):
        raise ValueError("конфигурация должна быть отображением (mapping) YAML")

    if not isinstance(data.get("addons"), list):
        raise ValueError("в конфигурации отсутствует список addons")

    data.setdefault("version", 1)
    data.setdefault("defaults", {})
    return data


def select_addons(config: dict, only: list[str], skip: list[str]) -> list[dict]:
    """Отобрать аддоны для загрузки согласно build.enabled, --only и --skip."""
    defaults = config.get("defaults") or {}
    selected = []

    for addon in config["addons"]:
        name = str(addon.get("name") or "")
        build = addon_settings(addon, defaults, "build")

        if not build.get("enabled", True):
            log(f"[addons] {name}: выключен в конфигурации (build.enabled: false)")
            continue
        if only and name not in only:
            continue
        if name in skip:
            log(f"[addons] {name}: пропущен по --skip")
            continue
        selected.append(addon)

    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Загрузка аддонов RRS согласно addons.yml",
    )
    parser.add_argument("--config", default=None,
                        help="путь к addons.yml (по умолчанию рядом со скриптом)")
    parser.add_argument("--project-root", default=None,
                        help="корень проекта (по умолчанию определяется по git)")
    parser.add_argument("--only", action="append", default=[], metavar="ИМЯ",
                        help="загрузить только указанные аддоны (можно указать несколько раз)")
    parser.add_argument("--skip", action="append", default=[], metavar="ИМЯ",
                        help="пропустить указанные аддоны (можно указать несколько раз)")
    parser.add_argument("--jobs", type=int, default=1, metavar="N",
                        help="число одновременных загрузок (по умолчанию 1)")
    parser.add_argument("--reclone", action="store_true",
                        help="удалить существующие каталоги и загрузить заново")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план и выйти, ничего не загружая")
    parser.add_argument("--list", action="store_true",
                        help="показать конфигурацию аддонов и выйти")
    parser.add_argument("--build", action="store_true",
                        help="собрать загруженные аддоны через cmake (нужен установленный RRS)")
    parser.add_argument("--sdk", default=None, metavar="ПУТЬ",
                        help="путь к установленному RRS (например dist/sdk); добавляется к путям поиска cmake")
    parser.add_argument("--prefix-path", action="append", default=[], metavar="ПУТЬ",
                        help="путь для CMAKE_PREFIX_PATH (можно указать несколько раз)")
    parser.add_argument("--generator", default="", metavar="ИМЯ",
                        help="генератор cmake (по умолчанию Ninja, если он установлен)")
    parser.add_argument("--build-type", default="Release", metavar="ТИП",
                        help="CMAKE_BUILD_TYPE для сборки аддонов (по умолчанию Release)")
    parser.add_argument("--build-timeout", type=int, default=1800, metavar="СЕК",
                        help="таймаут одной сборки в секундах (по умолчанию 1800)")
    parser.add_argument("--build-jobs", type=int, default=0, metavar="N",
                        help="число задач сборки cmake (по умолчанию число ядер)")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config).expanduser() if args.config else script_dir / "addons.yml"
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root \
        else detect_project_root(script_dir)

    try:
        config = load_config(config_path)
    except ValueError as error:
        fail(str(error))
        return 2

    if args.list:
        print_list(config, project_root)
        return 0

    known = [str(addon.get("name") or "") for addon in config["addons"]]
    unknown = [name for name in args.only if name not in known]
    if unknown:
        fail(f"в конфигурации нет аддонов: {', '.join(unknown)}")
        return 2

    log(f"[addons] конфигурация: {config_path}")
    log(f"[addons] корень проекта: {project_root}")
    log("")

    selected = select_addons(config, args.only, args.skip)
    log(f"[addons] к загрузке: {len(selected)} из {len(config['addons'])}")
    log("")

    # Основная ветка из списка branches становится рабочей: загрузка и отчёт
    # оперируют одним полем branch, запасные ветки остаются в addon_branches.
    defaults = config.get("defaults") or {}
    for addon in selected:
        branches = addon_branches(addon, defaults)
        if branches:
            addon["branch"] = branches[0]

    if not selected:
        log("[addons] нет аддонов для загрузки")
        write_manifest(config.get("defaults") or {}, project_root, config_path, [])
        return 0

    if args.jobs > 1 and len(selected) > 1:
        log(f"[addons] параллельных загрузок: {args.jobs}")
        log("")
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            entries = list(pool.map(
                lambda addon: download_addon_safe(
                    addon, config.get("defaults") or {}, project_root,
                    reclone=args.reclone, dry_run=args.dry_run),
                selected,
            ))
    else:
        entries = [download_addon_safe(addon, config.get("defaults") or {}, project_root,
                                       reclone=args.reclone, dry_run=args.dry_run)
                   for addon in selected]

    manifest_path = write_manifest(defaults, project_root, config_path, entries)

    # --- сборка загруженных аддонов ----------------------------------------
    # Сборка выполняется последовательно: аддоны независимы, но параллельный
    # cmake на слабой машине только мешает, а --jobs уходит в cmake --build.
    built = attempted = 0
    if args.build and not args.dry_run:
        prefix_paths = [str(Path(item).expanduser().resolve())
                        for item in (list(args.prefix_path)
                                     + ([args.sdk] if args.sdk else []))]
        generator = args.generator
        if not generator and shutil.which("ninja"):
            generator = "Ninja"

        log("")
        log(f"[addons] сборка аддонов ({len(entries)} шт., пути поиска cmake: "
            f"{os.pathsep.join(prefix_paths) if prefix_paths else 'нет'})")

        def build(candidate: dict) -> dict:
            """Сборка аддона с текущими параметрами прогонa."""
            return build_addon(candidate, defaults, project_root,
                               prefix_paths=prefix_paths,
                               generator=generator,
                               jobs=args.build_jobs or (os.cpu_count() or 1),
                               build_type=args.build_type,
                               timeout=args.build_timeout)

        for entry in entries:
            if entry["status"] != STATUS_OK:
                entry["build_status"] = BUILD_SKIPPED
                entry["build_error"] = "загрузка не удалась"
                continue

            addon = next((item for item in selected
                          if str(item.get("name") or "") == entry["name"]), {})
            if not addon_settings(addon, defaults, "build").get("enabled", True):
                entry["build_status"] = BUILD_SKIPPED
                entry["build_error"] = "сборка отключена в конфигурации"
                log(f"  {entry['name']}: сборка отключена в конфигурации")
                continue

            attempted += 1
            log(f"  {entry['name']}:")
            result = build(addon)
            entry["build_branch"] = entry["branch"]

            # Запасные ветки: если аддон не собирается из основной ветки,
            # каталог заменяется на следующую ветку из списка branches, и
            # сборка повторяется. Так без правки исходников подхватываются
            # ветки, собранные под конкретную версию RRS (devel-for-1.10.0).
            for branch in addon_branches(addon, defaults)[1:]:
                if result["status"] == BUILD_OK:
                    break
                if branch == entry["branch"]:
                    continue
                log(f"    пробую ветку {branch}")
                # Каталог сборки удаляется: он настроен под предыдущую ветку,
                # а CMakeCache хранит и пути, и найденные зависимости.
                shutil.rmtree(addon_destination(addon, defaults, project_root) / "build",
                              ignore_errors=True)
                switched = switch_branch(addon, defaults, project_root, branch)
                if switched["status"] != STATUS_OK:
                    log(f"    ветка {branch} недоступна: "
                        f"{first_line(switched['error'])}")
                    continue
                log(f"    ветка {branch}: коммит {switched['commit']}, сборка")
                entry["branch"] = branch
                entry["commit"] = switched["commit"]
                entry["build_branch"] = branch
                result = build(dict(addon, branch=branch))
                if result["status"] == BUILD_OK:
                    log(f"    аддон собран из ветки {branch}")

            entry["build_status"] = result["status"]
            entry["build_error"] = result["error"]
            if result["status"] == BUILD_OK:
                built += 1
            else:
                log(f"    не удалось: {first_line(result['error'])}")
                log("    аддон пропущен, сборка остальных продолжается")

    succeeded = [entry for entry in entries if entry["status"] == STATUS_OK]
    failed = [entry for entry in entries if entry["status"] == STATUS_FAILED]
    skipped = [entry for entry in entries if entry["status"] == STATUS_SKIPPED]

    log("")
    log("[addons] отчёт:")
    for entry in entries:
        download_mark = {"ok": "загружен", "failed": "ПРОПУСК",
                         "skipped": "пропуск"}[entry["status"]]
        line = f"  {entry['name']:<14} {download_mark:<8} {entry['error'] or entry['commit']}"

        build_status = entry.get("build_status", "")
        build_error = first_line(entry.get("build_error", ""))
        if build_status == BUILD_OK:
            line += "  → собран"
        elif build_status == BUILD_FAILED:
            line += f"  → сборка пропущена: {build_error}"
        elif build_status == BUILD_SKIPPED and build_error \
                and entry["status"] == STATUS_OK:
            line += f"  → сборка пропущена: {build_error}"
        log(line)

    log(f"[addons] загружено: {len(succeeded)}, пропущено по ошибкам загрузки: {len(failed)}, "
        f"без изменений: {len(skipped)}")
    if args.build and not args.dry_run:
        log(f"[addons] собрано: {built}, пропущено при сборке: {attempted - built}")
        failed_builds = [entry for entry in entries
                         if entry.get("build_status") == BUILD_FAILED]
        if failed_builds:
            log("[addons] не собраны, требуют внимания:")
            for entry in failed_builds:
                log(f"  {entry['name']}: {first_line(entry.get('build_error', ''))}")
    if failed:
        log("[addons] пропущены, сборка продолжается:")
        for entry in failed:
            log(f"  {entry['name']}: {entry['error']}")
    log(f"[addons] манифест: {manifest_path}")

    if args.dry_run:
        log("[addons] демонстрационный прогон (--dry-run): загрузка и сборка не выполнялись")
        return 0

    if not succeeded:
        return 1

    # Провал сборки не считается ошибкой запуска: аддон пропущен, причина
    # указана в отчёте выше. Ошибкой считается только полная неудача загрузки.
    log(f"[addons] загружено аддонов: {len(succeeded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())