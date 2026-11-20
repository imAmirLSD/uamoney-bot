# -*- coding: utf-8 -*-
"""Безопасно сохраняет API-ключ PiarFlow в bot_database.db."""
from getpass import getpass
from pathlib import Path
import sqlite3
import sys

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot_database.db"


def main() -> int:
    if not DB_PATH.is_file():
        print(f"❌ Не найдена база данных: {DB_PATH}")
        print("Положите этот скрипт рядом с main.py и bot_database.db.")
        return 1

    key = getpass("Введите API-ключ PiarFlow (ввод скрыт): ").strip()
    if not key:
        print("❌ Ключ пустой. Ничего не изменено.")
        return 1

    try:
        with sqlite3.connect(DB_PATH) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone()
            if not table:
                print("❌ Таблица settings не найдена. Сначала один раз запустите main.py.")
                return 1

            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("piarflow_api_key", key),
            )
            conn.commit()

        print("✅ API-ключ PiarFlow сохранён в bot_database.db.")
        print("Перезапустите бота и проверьте сообщение: PiarFlow: ключ загружен из БД.")
        return 0
    except sqlite3.Error as exc:
        print(f"❌ Ошибка SQLite: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
