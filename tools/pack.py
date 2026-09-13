#!/usr/bin/env python3
"""pack.py — инструмент автора пака для площадки ФиксАР.

Стандартная библиотека Python 3.10+, без обязательных зависимостей.
Единственная необязательная зависимость — PyYAML: без неё команда ``check``
пропускает разбор ``pack.yaml`` (и, значит, сверку namespace/name/version с
путём) с явным сообщением об этом, но остальные локальные проверки всё
равно выполняются. Команды ``build``, ``tag`` и ``validate`` читают из
``pack.yaml`` намерение автора (namespace/name/version, а иногда — состав
направлений), и без PyYAML честно отказываются работать, а не гадают по
регулярным выражениям.

Команды:
  check <каталог-пака>                локальные проверки, без сети
  build <каталог-пака> --out <dir>    детерминированный tar.gz
  tag <каталог-пака>                  метка выпуска: namespace.name@version
  validate <каталог-пака>             POST pack.yaml на /registry/validate

Схема тега и файла выпуска (namespace.name@version):
  tag  = "<namespace>.<name>@<version>"
  file = "<namespace>-<name>-<version>.tar.gz"
  url  = https://github.com/<owner>/<repo>/releases/download/<tag>/<file>

Разбор тега — как в схеме площадки: разрезать по ПОСЛЕДНЕМУ "@" → version;
левую часть разрезать по ПЕРВОЙ "." → namespace / name. Точка и "@" не
встречаются в алфавите namespace/name/version, поэтому разбор однозначен.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
    _HAS_YAML = True
except ImportError:  # PyYAML необязателен — см. модульный docstring выше.
    yaml = None  # type: ignore[assignment]
    _HAS_YAML = False


# ═══════════════════════════════════════════════════════════════════════════
# ЗЕРКАЛО ПРАВИЛ ПЛОЩАДКИ.
#
# Эти константы и регулярные выражения повторяют пределы архива и форму
# имени/версии, которые проверяет сканер площадки. Это ЗЕРКАЛО, А НЕ ИСТОЧНИК
# ПРАВДЫ: сканер площадки решает, что примут, а что нет. Если правила площадки
# когда-нибудь изменятся, эти константы придётся поправить руками — здешняя
# проверка призвана поймать явные беды ДО отправки, а не подменить собой
# разбор площадки.
# ═══════════════════════════════════════════════════════════════════════════

#: Сжатый архив ≤ ПРЕДЕЛ_АРХИВА байт — иначе archive_too_big (fatal у сканера).
ПРЕДЕЛ_АРХИВА = 5 * 1024 * 1024
#: Суммарно развёрнутое содержимое ≤ ПРЕДЕЛ_РАЗВЁРНУТОГО — иначе
#: unpacked_too_big (fatal, признак архивной бомбы).
ПРЕДЕЛ_РАЗВЁРНУТОГО = 20 * 1024 * 1024
#: Один файл ≤ ПРЕДЕЛ_ФАЙЛА — иначе file_too_big (сам файл дальше не
#: проверяется, но разбор архива продолжается).
ПРЕДЕЛ_ФАЙЛА = 2 * 1024 * 1024
#: Записей в архиве ≤ ПРЕДЕЛ_ФАЙЛОВ — иначе too_many_files (fatal).
ПРЕДЕЛ_ФАЙЛОВ = 500

#: Разрешённые расширения — ЗАКРЫТЫЙ список. Пак декларативен: кода
#: (.py/.js/.sh/.so и т.п.) не бывает никогда.
РАСШИРЕНИЯ = frozenset({".yaml", ".yml", ".json", ".md", ".txt", ".csv"})

#: Машинное имя: namespace, name составной части. Строчная латиница, цифры,
#: дефис, 3..64 знака.
ИМЯ_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
#: semver без пред-релизов: у пака в реестре версия окончательная.
ВЕРСИЯ_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: Публичная дверь площадки — проверка манифеста без входа, ничего не создаёт.
VALIDATE_URL = "https://api.agrigate.pro/registry/validate"

#: Регэксп площадки для ссылки на выпуск (namespace.name@version).
#: Используется только чтобы предупредить автора ДО того, как он создаст тег
#: в git и релиз на GitHub — сама сборка тега за площадкой не проверяется.
ССЫЛКА_ВЫПУСКА_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/releases/download/"
    r"(?P<tag>[^/?#\s]{1,128})/(?P<file>[^/?#\s]{1,255}\.tar\.gz)$")


class ПакОтказ(Exception):
    """Беда, из-за которой команда дальше не идёт. Сообщение — по-русски."""


# ═══════════════════════════════════════════════════════════════════════════
# Разбор каталога пака (используется и check, и build).
# ═══════════════════════════════════════════════════════════════════════════


def _walk_entries(pack_dir: Path):
    """(путь, признак_каталога) для каждой записи внутри pack_dir.

    ``os.walk(..., followlinks=False)`` не заходит внутрь симлинк-каталогов,
    но всё равно перечисляет их имена — значит находка о них не потеряется.
    """
    import os

    for root, dirnames, filenames in os.walk(pack_dir, followlinks=False):
        rootp = Path(root)
        for d in sorted(dirnames):
            yield rootp / d, True
        for f in sorted(filenames):
            yield rootp / f, False


class РезультатРазбора:
    """Что нашли в каталоге пака: беды и то, что удалось прочитать."""

    def __init__(self) -> None:
        self.находки: list[str] = []
        #: Относительные POSIX-пути файлов, прошедших проверку имени,
        #: размера и расширения — «годные», как у сканера площадки.
        self.годные: list[str] = []
        self.всего_файлов = 0
        self.развёрнуто = 0
        self.manifest_text: str | None = None
        self.manifest: dict[str, Any] | None = None
        self.namespace: str | None = None
        self.name: str | None = None
        self.version: str | None = None

    @property
    def passed(self) -> bool:
        return not self.находки


def разобрать_каталог(pack_dir: Path) -> РезультатРазбора:
    """Локальные проверки: то, что можно узнать без сети и без сборки.

    Порядок проверки одной записи повторяет порядок сканера площадки: имя/
    ссылка → размер файла → суммарный развёрнутый размер (fatal, останавливает
    разбор) → расширение. Отличие: здесь разбирается каталог на диске, а не
    члены tar — коннектор путей и сама остановка при архивной бомбе поэтому
    свои.
    """
    r = РезультатРазбора()

    if not pack_dir.is_dir():
        r.находки.append(f"нет такого каталога: {pack_dir}")
        return r

    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.is_file():
        r.находки.append(
            f"нет манифеста: «pack.yaml» должен лежать в корне пака "
            f"({manifest_path})")
    else:
        try:
            r.manifest_text = manifest_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as ошибка:
            r.находки.append(f"pack.yaml не в UTF-8: {ошибка}")

    ожид_namespace = pack_dir.parent.name
    ожид_name = pack_dir.name
    for роль, значение in (("namespace", ожид_namespace), ("name", ожид_name)):
        if not ИМЯ_RE.match(значение):
            r.находки.append(
                f"каталог «{значение}» (сегмент пути «{роль}») не по форме "
                f"имени площадки ({ИМЯ_RE.pattern})")

    if r.manifest_text is not None:
        if not _HAS_YAML:
            print(
                "PyYAML не найден — проверка соответствия "
                "namespace/name/version пути ПРОПУЩЕНА "
                "(pip install pyyaml, чтобы включить)",
                file=sys.stderr)
        else:
            try:
                сырой = yaml.safe_load(r.manifest_text)
            except yaml.YAMLError as ошибка:
                r.находки.append(f"pack.yaml не читается как YAML: {ошибка}")
                сырой = None
            if сырой is not None and not isinstance(сырой, dict):
                r.находки.append("pack.yaml — это не набор полей")
            elif isinstance(сырой, dict):
                r.manifest = сырой
                метаданные = сырой.get("metadata")
                if not isinstance(метаданные, dict):
                    r.находки.append("pack.yaml: нет раздела metadata")
                else:
                    r.namespace = метаданные.get("namespace")
                    r.name = метаданные.get("name")
                    r.version = метаданные.get("version")
                    if r.namespace != ожид_namespace:
                        r.находки.append(
                            f"metadata.namespace «{r.namespace}» не совпадает "
                            f"с каталогом «{ожид_namespace}»")
                    if r.name != ожид_name:
                        r.находки.append(
                            f"metadata.name «{r.name}» не совпадает с "
                            f"каталогом «{ожид_name}»")
                    if not isinstance(r.version, str) or not ВЕРСИЯ_RE.match(
                            r.version):
                        r.находки.append(
                            f"metadata.version «{r.version}» — не semver "
                            f"«д.д.д» ({ВЕРСИЯ_RE.pattern})")

    # Файлы: имя/ссылка, размер, расширение — в порядке сканера площадки.
    остановлен_бомбой = False
    for путь, каталог in _walk_entries(pack_dir):
        относительный = путь.relative_to(pack_dir).as_posix()
        if путь.is_symlink():
            r.находки.append(f"ссылка в паке недопустима: {относительный}")
            continue
        if каталог:
            continue
        if not путь.is_file():
            r.находки.append(f"не файл и не каталог: {относительный}")
            continue

        r.всего_файлов += 1
        размер = путь.stat().st_size
        if размер > ПРЕДЕЛ_ФАЙЛА:
            r.находки.append(
                f"файл больше предела {ПРЕДЕЛ_ФАЙЛА} байт: {относительный} "
                f"— {размер} байт")
            continue

        if остановлен_бомбой:
            continue
        r.развёрнуто += размер
        if r.развёрнуто > ПРЕДЕЛ_РАЗВЁРНУТОГО:
            r.находки.append(
                f"суммарный развёрнутый размер больше предела "
                f"{ПРЕДЕЛ_РАЗВЁРНУТОГО} байт — признак архивной бомбы")
            остановлен_бомбой = True
            continue

        расш = путь.suffix.lower()
        if расш not in РАСШИРЕНИЯ:
            r.находки.append(
                f"недопустимое расширение «{расш or 'без расширения'}»: "
                f"{относительный} (разрешены: "
                f"{', '.join(sorted(РАСШИРЕНИЯ))})")
            continue

        r.годные.append(относительный)

    if r.всего_файлов > ПРЕДЕЛ_ФАЙЛОВ:
        r.находки.append(
            f"файлов больше предела {ПРЕДЕЛ_ФАЙЛОВ}: {r.всего_файлов}")

    # tests/ и evals/ — из манифеста, если распарсили, иначе умолчания
    # площадки. Логика повторяет pack_scan._сверить_состав: путь считается
    # присутствующим, если среди «годных» есть файл, равный пути или лежащий
    # внутри него.
    fixtures = "tests/"
    evals = "evals/"
    if isinstance(r.manifest, dict):
        проверки = (r.manifest.get("spec") or {}).get("tests") or {}
        fixtures = проверки.get("fixtures", fixtures) or fixtures
        evals = проверки.get("evals", evals) or evals
    годные_множество = set(r.годные)
    for что, путь_строкой in (("тесты (tests)", fixtures),
                              ("оценки (evals)", evals)):
        путь_строкой = путь_строкой.rstrip("/")
        есть = any(и == путь_строкой or и.startswith(путь_строкой + "/")
                   for и in годные_множество)
        if not есть:
            r.находки.append(
                f"объявлено «{путь_строкой}», а в паке пусто: {что} "
                "обязательны — без них пак принимать нечем")

    return r


# ═══════════════════════════════════════════════════════════════════════════
# check
# ═══════════════════════════════════════════════════════════════════════════


def cmd_check(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir)
    r = разобрать_каталог(pack_dir)
    if r.passed:
        print(f"check: пак «{pack_dir}» в порядке "
              f"({r.всего_файлов} файлов, {r.развёрнуто} байт развёрнуто)")
        return 0
    print(f"check: в паке «{pack_dir}» {len(r.находки)} беда(-ы):")
    for находка in r.находки:
        print(f"  - {находка}")
    return 1


# ═══════════════════════════════════════════════════════════════════════════
# build — детерминированная сборка tar.gz.
#
# Что нормализуется и почему (всё — прямо здесь, чтобы tools/pack.py
# оставался единственным файлом без собственных импортов внутри репозитория
# паков):
#   - порядок записей — sorted() по имени пути как строке Python;
#   - mtime каждой записи = 0;
#   - mode = 0o644 у каждого файла явно;
#   - uid=gid=0, uname=gname="";
#   - format=tarfile.USTAR_FORMAT — явно, а не умолчание (PAX добавил бы
#     собственное поле времени в расширенный заголовок);
#   - каталоги в опись не кладутся, только файлы — сканер площадки при
#     isdir() их просто пропускает;
#   - gzip: mtime=0, fileobj без имени (io.BytesIO), явный filename="" —
#     тогда флаг FNAME не выставляется. Байт ОС в заголовке gzip модуль
#     всегда пишет как 0xFF независимо от платформы.
#
# ОГОВОРКА: сами сжатые deflate-байты формирует zlib, и его версия зависит от
# сборки Python. sha256 итогового .tar.gz может отличаться между разными
# ОС/версиями zlib при побайтово одинаковом содержимом внутри — площадка
# сверяет sha256 присланного архива с объявленным автором, а не пересобирает
# его сама, так что это не мешает приёмке. Если когда-нибудь понадобится
# кросс-платформенная гарантия «тот же tar.gz» — хранить рядом ещё и sha256
# несжатого tar-потока и сверять по нему; в контракте ручки этого сегодня нет.
# ═══════════════════════════════════════════════════════════════════════════


def _собрать_tar_gz(файлы: dict[str, bytes]) -> bytes:
    """``файлы``: относительный POSIX-путь → содержимое. Возвращает tar.gz."""
    буфер = io.BytesIO()
    with gzip.GzipFile(fileobj=буфер, mode="wb", compresslevel=9,
                        mtime=0, filename="") as гзип:
        with tarfile.open(fileobj=гзип, mode="w",
                           format=tarfile.USTAR_FORMAT) as тар:
            for имя in sorted(файлы):
                данные = файлы[имя]
                инфо = tarfile.TarInfo(name=имя)
                инфо.size = len(данные)
                инфо.mtime = 0
                инфо.mode = 0o644
                инфо.type = tarfile.REGTYPE
                инфо.uid = 0
                инфо.gid = 0
                инфо.uname = ""
                инфо.gname = ""
                тар.addfile(инфо, io.BytesIO(данные))
    return буфер.getvalue()


def cmd_build(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir)
    r = разобрать_каталог(pack_dir)
    if not r.passed:
        print(f"build: пак «{pack_dir}» не проходит локальную проверку — "
              "сборка отменена:")
        for находка in r.находки:
            print(f"  - {находка}")
        return 1
    if not _HAS_YAML:
        print(
            "build: PyYAML не установлен, а сборка требует значений из "
            "pack.yaml (namespace/name/version для имени файла и тега). "
            "Установите: pip install pyyaml", file=sys.stderr)
        return 1
    if r.namespace is None or r.name is None or r.version is None:
        print("build: не удалось прочитать namespace/name/version из "
              "pack.yaml", file=sys.stderr)
        return 1

    файлы = {
        относительный: (pack_dir / относительный).read_bytes()
        for относительный in r.годные
    }

    архив = _собрать_tar_gz(файлы)
    if len(архив) > ПРЕДЕЛ_АРХИВА:
        print(f"build: собранный архив {len(архив)} байт превышает предел "
              f"площадки {ПРЕДЕЛ_АРХИВА} байт (сжатых) — площадка отклонит "
              "его как archive_too_big", file=sys.stderr)
        return 1

    отпечаток = hashlib.sha256(архив).hexdigest()
    tag = f"{r.namespace}.{r.name}@{r.version}"
    if len(tag) > 128:
        print(f"build: тег «{tag[:40]}…» длиннее 128 знаков — регэксп "
              "площадки для ссылки на выпуск его не примет", file=sys.stderr)

    имя_файла = f"{r.namespace}-{r.name}-{r.version}.tar.gz"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    итоговый_путь = out_dir / имя_файла
    итоговый_путь.write_bytes(архив)

    print(json.dumps({
        "file": итоговый_путь.as_posix(),
        "sha256": отпечаток,
        "size": len(архив),
        "tag": tag,
        "namespace": r.namespace,
        "name": r.name,
        "version": r.version,
    }, ensure_ascii=False))
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# tag
# ═══════════════════════════════════════════════════════════════════════════


def cmd_tag(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir)
    r = разобрать_каталог(pack_dir)
    if not _HAS_YAML:
        print("tag: PyYAML не установлен, а для метки нужны namespace/name/"
              "version из pack.yaml. Установите: pip install pyyaml",
              file=sys.stderr)
        return 1
    if r.namespace is None or r.name is None or r.version is None:
        print(f"tag: не удалось прочитать namespace/name/version из "
              f"pack.yaml в «{pack_dir}»", file=sys.stderr)
        for находка in r.находки:
            print(f"  - {находка}", file=sys.stderr)
        return 1
    tag = f"{r.namespace}.{r.name}@{r.version}"
    if len(tag) > 128:
        print(f"tag: «{tag}» длиннее 128 знаков — регэксп площадки для "
              "ссылки на выпуск его не примет", file=sys.stderr)
        print(tag)
        return 1
    print(tag)
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# validate — без входа, ничего не создаёт.
# ═══════════════════════════════════════════════════════════════════════════


def _кандидаты_направления(имя: str) -> list[str]:
    """Где может лежать файл направления — так же, как ищет площадка."""
    return [f"{имя}.yaml", f"{имя}.yml",
            f"domains/{имя}.yaml", f"domains/{имя}.yml"]


def cmd_validate(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack_dir)
    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.is_file():
        print(f"validate: нет pack.yaml в «{pack_dir}»", file=sys.stderr)
        return 1
    pack_yaml_text = manifest_path.read_text(encoding="utf-8")

    domain_yaml_text = ""
    if _HAS_YAML:
        try:
            сырой = yaml.safe_load(pack_yaml_text)
        except yaml.YAMLError:
            сырой = None
        if isinstance(сырой, dict):
            имена = (((сырой.get("spec") or {}).get("components") or {})
                     .get("domains") or [])
            for имя in имена:
                for кандидат in _кандидаты_направления(str(имя)):
                    путь = pack_dir / кандидат
                    if путь.is_file():
                        domain_yaml_text = путь.read_text(encoding="utf-8")
                        break
                if domain_yaml_text:
                    break
    else:
        print("validate: PyYAML не найден — направление отдельно не "
              "проверяется, отправлен только pack.yaml", file=sys.stderr)

    тело = json.dumps({
        "pack_yaml": pack_yaml_text,
        "domain_yaml": domain_yaml_text,
    }).encode("utf-8")

    запрос = urllib.request.Request(
        VALIDATE_URL, data=тело, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(запрос, timeout=30) as ответ:
            сырой_ответ = ответ.read()
    except urllib.error.HTTPError as ошибка:
        сырой_ответ = ошибка.read()
        print(f"validate: площадка ответила {ошибка.code}", file=sys.stderr)
    except urllib.error.URLError as ошибка:
        print(f"validate: не удалось обратиться к площадке: {ошибка}",
              file=sys.stderr)
        return 1

    try:
        разобранный = json.loads(сырой_ответ.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ошибка:
        print(f"validate: ответ площадки не разобрать как JSON: {ошибка}",
              file=sys.stderr)
        print(сырой_ответ.decode("utf-8", errors="replace"))
        return 1

    print(json.dumps(разобранный, ensure_ascii=False, indent=2))
    return 0 if разобранный.get("ok") else 1


# ═══════════════════════════════════════════════════════════════════════════
# точка входа
# ═══════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    парсер = argparse.ArgumentParser(
        prog="pack.py", description="Инструмент автора пака для площадки ФиксАР.")
    подкоманды = парсер.add_subparsers(dest="команда", required=True)

    п_check = подкоманды.add_parser(
        "check", help="локальные проверки пака без сети")
    п_check.add_argument("pack_dir", help="путь к каталогу пака, packs/<ns>/<name>")
    п_check.set_defaults(func=cmd_check)

    п_build = подкоманды.add_parser(
        "build", help="собрать детерминированный tar.gz")
    п_build.add_argument("pack_dir", help="путь к каталогу пака, packs/<ns>/<name>")
    п_build.add_argument("--out", default="dist", help="каталог для .tar.gz (по умолчанию dist)")
    п_build.set_defaults(func=cmd_build)

    п_tag = подкоманды.add_parser(
        "tag", help="напечатать метку выпуска по схеме площадки")
    п_tag.add_argument("pack_dir", help="путь к каталогу пака, packs/<ns>/<name>")
    п_tag.set_defaults(func=cmd_tag)

    п_validate = подкоманды.add_parser(
        "validate", help="проверить pack.yaml на площадке, без входа")
    п_validate.add_argument("pack_dir", help="путь к каталогу пака, packs/<ns>/<name>")
    п_validate.set_defaults(func=cmd_validate)

    ns = парсер.parse_args(argv)
    try:
        return ns.func(ns)
    except ПакОтказ as ошибка:
        print(f"{ns.команда}: {ошибка}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
