# -*- coding: utf-8 -*-
"""Исправляет маршрутизацию нижних кнопок UA BOT после удаления обычных emoji.
Запускать из папки проекта: python fix_menu_buttons.py
"""
from pathlib import Path
import ast
import shutil

BASE = Path(__file__).resolve().parent
HANDLERS = BASE / "handlers.py"
MIDDLEWARES = BASE / "middlewares.py"

LABELS = {
    "📝 Задания": "Задания",
    "📝 Задания №2": "Задания №2",
    "📝 Задания №3": "Задания №3",
    "📝 Задания №4": "Задания №4",
    "👥 Рефералы": "Рефералы",
    "📱 Кабинет": "Кабинет",
    "📢 Продвижение": "Продвижение",
    "📚 Инфо": "Инфо",
    "👨‍💻 Админка": "Админка",
}


def save_if_changed(path: Path, original: str, updated: str) -> bool:
    if original == updated:
        print(f"ℹ️ {path.name}: изменений не требуется")
        return False
    backup = path.with_suffix(path.suffix + ".before_menu_fix.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    # Проверка синтаксиса до записи.
    if path.suffix == ".py":
        compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")
    print(f"✅ Исправлен {path.name}; резервная копия: {backup.name}")
    return True


def patch_handlers() -> None:
    if not HANDLERS.is_file():
        raise FileNotFoundError("Не найден handlers.py рядом со скриптом")
    original = HANDLERS.read_text(encoding="utf-8-sig")
    updated = original
    changed = 0
    for old, new in LABELS.items():
        old_line = f'@dp.message(F.text == "{old}")'
        new_line = f'@dp.message(F.text == "{new}")'
        if old_line in updated:
            count = updated.count(old_line)
            updated = updated.replace(old_line, new_line)
            changed += count
    if changed == 0:
        # Удостоверимся, что файл уже использует новые подписи.
        expected = [f'@dp.message(F.text == "{v}")' for v in LABELS.values()]
        found = sum(line in updated for line in expected)
        if found < 7:
            raise RuntimeError(
                "В handlers.py не найдены ожидаемые фильтры нижнего меню. "
                "Похоже, у вас другая версия файла; приложите handlers.py и я подгоню патч."
            )
    # Проверяем общий синтаксис перед записью.
    ast.parse(updated, filename=str(HANDLERS))
    save_if_changed(HANDLERS, original, updated)
    print(f"   Обработчиков меню с изменёнными фильтрами: {changed}")


def patch_middlewares() -> None:
    if not MIDDLEWARES.is_file():
        print("⚠️ middlewares.py не найден — пропускаю синхронизацию рекламы")
        return
    original = MIDDLEWARES.read_text(encoding="utf-8-sig")
    updated = original
    marker = "MAIN_MENU_BUTTONS = {"
    start = updated.find(marker)
    if start < 0:
        print("⚠️ MAIN_MENU_BUTTONS не найден — обработчики меню уже будут работать")
        return
    end = updated.find("}", start)
    if end < 0:
        raise RuntimeError("Не удалось разобрать список MAIN_MENU_BUTTONS")
    block = updated[start:end + 1]
    for old, new in LABELS.items():
        block = block.replace(f'"{old}"', f'"{new}"')
    updated = updated[:start] + block + updated[end + 1:]
    save_if_changed(MIDDLEWARES, original, updated)


if __name__ == "__main__":
    patch_handlers()
    patch_middlewares()
    print("\n✅ Готово. Запустите бота командой: python main.py")
