#!/usr/bin/env python3
"""
Гейт резервного канала обновлений (INV-113).

Резерв — это `latest.json` каждого продукта, опубликованные на GitHub Pages.
`updater.rs` читает их, когда Edge-функция обновлений недоступна. Канал этот
дрейфует молча: заброшенный резерв продолжает отвечать — но устаревшей правдой,
и клиент получает старую сборку, считая её последней.

Сверяются ТРИ вещи, потому что расходится каждая по отдельности:
  1. версия      — резерв против `app_versions`;
  2. ссылка и контрольная сумма — они меняются даже при совпадающей версии;
  3. НАБОРЫ КЛЮЧЕЙ с обеих сторон — односторонняя сверка пропускает и «есть в
     резерве, нет на сервере», и обратное. Оба случая в этом канале реальны.

Проверка, которой нечего проверить, объявляет это громче результата (INV-112):
пустой список файлов или пустой ответ сервера — это отказ, а не «расхождений нет».

Использование:
    python check_backup_channel.py            # только проверка, код возврата 1 при расхождении
    python check_backup_channel.py --net      # дополнительно проверить, что ссылки живы
    python check_backup_channel.py --apply    # привести резерв в соответствие с app_versions
    python check_backup_channel.py --apply --only aurora-legal   # один продукт

Доступ к базе берётся из `~/.secrets/supabase_aurora.env`
(ключи `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`). В коде секретов нет.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
SECRETS = pathlib.Path.home() / ".secrets" / "supabase_aurora.env"
TIMEOUT = 30


def read_secrets() -> tuple[str, str]:
    if not SECRETS.exists():
        sys.exit(f"FAIL: файл доступа не найден: {SECRETS}")
    env = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    url = env.get("SUPABASE_URL")
    key = env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("FAIL: в файле доступа нет SUPABASE_URL или SUPABASE_SERVICE_ROLE_KEY")
    return url.rstrip("/"), key


def fetch_server(url: str, key: str) -> dict[str, dict]:
    req = urllib.request.Request(
        f"{url}/rest/v1/app_versions?select=product,version,download_url,checksum,release_notes",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"FAIL: сервер обновлений недоступен, сверять не с чем: {e}")
    if not rows:
        sys.exit("FAIL: таблица app_versions пуста — сверять не с чем (это отказ, не «всё чисто»)")
    return {r["product"]: r for r in rows}


def read_local() -> dict[str, dict]:
    out = {}
    for f in sorted(ROOT.glob("*/latest.json")):
        try:
            out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  🔴 {f.parent.name}: latest.json не разбирается — {e}")
            out[f.parent.name] = {}
    return out


def parse_version(v: str) -> tuple:
    """
    «2.1.0-rc7» → (2, 1, 0, 0) — предвыпускной суффикс считается СТАРШЕ выпуска
    с тем же номером, поэтому обычному релизу добавляется хвост 1.
    Нечисловое возвращает пустой кортеж: сравнивать такое нельзя.
    """
    if not v:
        return ()
    head, _, tail = str(v).partition("-")
    parts = head.split(".")
    if not all(p.isdigit() for p in parts):
        return ()
    return tuple(int(p) for p in parts) + (0 if tail else 1,)


def is_downgrade(local_v: str, server_v: str) -> bool:
    """Правка понизила бы версию у клиента? Неизвестное считаем понижением — безопаснее."""
    a, b = parse_version(local_v), parse_version(server_v)
    if not a or not b:
        return True
    return b < a


def link_alive(url: str) -> str:
    if not url:
        return "ссылки нет"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=TIMEOUT) as r:
            return f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 — интересует любой отказ, вид его печатается
        return f"ОТКАЗ ({e})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="привести резерв в соответствие с сервером")
    ap.add_argument("--net", action="store_true", help="проверить живость ссылок (HEAD-запрос)")
    ap.add_argument("--only", default=None, help="ограничиться одним ключом")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="разрешить правку, понижающую версию в резерве (по умолчанию запрещена)")
    args = ap.parse_args()

    url, key = read_secrets()
    server = fetch_server(url, key)
    local = read_local()

    if not local:
        sys.exit("FAIL: ни одного latest.json не найдено — проверять нечего (это отказ)")

    keys = sorted(set(server) | set(local))
    if args.only:
        keys = [k for k in keys if k == args.only] or sys.exit(f"FAIL: ключ {args.only} не найден ни с одной стороны")

    problems: list[str] = []
    fixed: list[str] = []
    print(f"сверяется ключей: {len(keys)} (в резерве {len(local)}, на сервере {len(server)})\n")

    for k in keys:
        s, l = server.get(k), local.get(k)

        if s is None:
            problems.append(f"{k}: есть в резерве ({l.get('version')}), на сервере строки НЕТ")
            print(f"  🔴 {k:<26} резерв {str(l.get('version')):<12} сервер —")
            continue
        if l is None:
            problems.append(f"{k}: есть на сервере ({s.get('version')}), файла резерва НЕТ")
            print(f"  🔴 {k:<26} резерв —            сервер {s.get('version')}")
            continue

        diffs = []
        if l.get("version") != s.get("version"):
            diffs.append(f"версия {l.get('version')} != {s.get('version')}")
        if (l.get("download_url") or "") != (s.get("download_url") or ""):
            diffs.append("ссылка расходится")
        if (l.get("checksum") or "") != (s.get("checksum") or ""):
            diffs.append("контрольная сумма расходится")
        if not s.get("download_url"):
            diffs.append("🔴 у сервера пустая ссылка — обновление ведёт в никуда")

        net = ""
        if args.net:
            net = "  [" + link_alive(l.get("download_url", "")) + "]"

        if diffs:
            problems.append(f"{k}: " + "; ".join(diffs))
            print(f"  🔴 {k:<26} {'; '.join(diffs)}{net}")
            if args.apply and s.get("download_url") and \
                    is_downgrade(l.get("version", ""), s.get("version", "")) and not args.allow_downgrade:
                print(f"      ⏸ НЕ трогаю: правка понизила бы резерв "
                      f"{l.get('version')} → {s.get('version')}. Скорее всего отстал СЕРВЕР, "
                      f"а не резерв. Разбирать вручную (или --allow-downgrade, осознанно).")
            elif args.apply and s.get("download_url"):
                path = ROOT / k / "latest.json"
                data = dict(l)
                data["version"] = s["version"]
                data["download_url"] = s["download_url"]
                data["checksum"] = s.get("checksum", "")
                if s.get("release_notes"):
                    data["release_notes"] = s["release_notes"]
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                fixed.append(k)
                print(f"      → приведён к серверу")
        else:
            print(f"  ok {k:<26} {s.get('version')}{net}")

    print()
    if args.apply and fixed:
        print(f"приведено в соответствие: {len(fixed)} — {', '.join(fixed)}")
        print("🔴 файлы изменены локально; резерв доедет до клиентов только после отправки в GitHub Pages")
        remaining = [p for p in problems if p.split(":")[0] not in fixed]
        if remaining:
            print(f"\nосталось без правки ({len(remaining)}) — требуют решения человека:")
            for p in remaining:
                print(f"  - {p}")
        return 1 if remaining else 0

    if problems:
        print(f"FAIL ({len(problems)}): резерв разошёлся с app_versions")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("OK: резерв совпадает с app_versions по версиям, ссылкам, суммам и набору ключей.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
