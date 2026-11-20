# -*- coding: utf-8 -*-
"""Добавляет недостающий импорт get_main_keyboard в handlers.py."""
from pathlib import Path
import py_compile
import shutil

folder = Path(__file__).resolve().parent
handlers = folder / "handlers.py"

if not handlers.is_file():
    raise SystemExit(f"Не найден файл: {handlers}")

source = handlers.read_text(encoding="utf-8-sig")
if "from utils import get_main_keyboard" in source:
    print("✅ Импорт get_main_keyboard уже присутствует — изменений нет.")
else:
    backup = handlers.with_suffix(".py.bak")
    if not backup.exists():
        shutil.copy2(handlers, backup)
    source = source.rstrip() + "\n\n# Импорт клавиатуры главного меню\nfrom utils import get_main_keyboard\n"
    handlers.write_text(source, encoding="utf-8")
    print("✅ Добавлен импорт get_main_keyboard в конец handlers.py")
    print(f"📦 Резервная копия: {backup.name}")

try:
    py_compile.compile(str(handlers), doraise=True)
except py_compile.PyCompileError as exc:
    raise SystemExit(f"❌ Синтаксическая ошибка после исправления: {exc}")

print("✅ handlers.py прошёл проверку синтаксиса.")
