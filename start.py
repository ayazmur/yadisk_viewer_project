#!/usr/bin/env python3
"""Единая точка запуска Yadisk Viewer.

Запускает:
  1) установку зависимостей (если их нет),
  2) запрос ключей Yandex OAuth в консоли (нигде не сохраняются),
  3) миграции Django,
  4) collectstatic (для whitenoise),
  5) ASGI-сервер uvicorn на 0.0.0.0:8000.

В Codespaces порт 8000 подхватывается автоматически (см. .devcontainer).
Использование: python start.py
"""

import subprocess
import sys
import os
import getpass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PORT = 8000
HOST = "0.0.0.0"
MIN_PYTHON = (3, 10)  # код использует синтаксис str | None


def prompt_oauth_credentials() -> None:
    """Запрашивает ключи Yandex OAuth в консоли. Живут только в памяти процесса."""
    if os.environ.get('YANDEX_OAUTH2_KEY') and os.environ.get('YANDEX_OAUTH2_SECRET'):
        print("  [OK] Yandex OAuth уже задан через переменные окружения.")
        return

    print()
    print("  Yandex OAuth (необязательно).")
    print("  Вставьте данные приложения с https://oauth.yandex.ru/")
    print("  Ввод не сохраняется на диск - ключи живут только до остановки сервера.")
    print("  Оставьте пустым и нажмите Enter, чтобы пропустить.")
    print()

    try:
        key = input("  Client ID (Key): ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        print("  [INFO] OAuth пропущен - вход через Яндекс недоступен.")
        return

    try:
        secret = getpass.getpass("  Client Secret (ввод скрыт): ").strip()
    except (EOFError, KeyboardInterrupt):
        secret = ""
    if not secret:
        print("  [INFO] Секрет не введён - OAuth пропущен.")
        return

    os.environ['YANDEX_OAUTH2_KEY'] = key
    os.environ['YANDEX_OAUTH2_SECRET'] = secret
    print("  [OK] Ключи приняты (в памяти процесса).")


def run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=BASE_DIR)
    if check and result.returncode != 0:
        print(f"\n[ОШИБКА] Команда завершилась с кодом {result.returncode}: {' '.join(cmd)}", file=sys.stderr)
        sys.exit(result.returncode)
    return result


def main() -> None:
    print()
    print("=" * 60)
    print("  YADISK LINK VIEWER - запуск")
    print("=" * 60)

    if sys.version_info < MIN_PYTHON:
        print(f"[ОШИБКА] Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, у вас {sys.version.split()[0]}.", file=sys.stderr)
        sys.exit(1)

    try:
        import aiohttp  # noqa: F401
        import django  # noqa: F401
        import uvicorn  # noqa: F401
        deps_ok = True
    except ImportError:
        deps_ok = False

    if not deps_ok:
        print("\n[1/5] Зависимости не найдены - устанавливаю из requirements.txt ...")
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    else:
        print("\n[1/5] Зависимости уже установлены - пропускаю.")

    print("\n[2/5] Проверяю конфигурацию OAuth ...")
    prompt_oauth_credentials()

    print("\n[3/5] Применяю миграции Django ...")
    run([sys.executable, "manage.py", "migrate", "--noinput"])

    print("\n[4/5] Собираю статические файлы ...")
    run([sys.executable, "manage.py", "collectstatic", "--noinput"], check=False)

    print("\n[5/5] Запускаю сервер (uvicorn) ...")
    print("-" * 60)
    print(f"  Локальный адрес:   http://127.0.0.1:{PORT}")
    print(f"  Сеть (Codespaces): http://{HOST}:{PORT}")
    if "CODESPACE" in __import__("os").environ:
        print()
        print("  [Codespaces] Порт 8000 доступен автоматически:")
        print("  - панель 'Ports' в VS Code -> кнопка 'Forward a Port',")
        print("    либо вкладка 'Ports' покажет ссылку вида")
        print("    https://<имя-репо>.<workspace>.<user>.app.github.dev")
    print("-" * 60)

    import uvicorn
    uvicorn.run("yadisk_viewer.asgi:application", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
