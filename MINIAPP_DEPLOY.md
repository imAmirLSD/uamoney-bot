# Mini App UA — 설정/выкладка

MVP: главная, реальные задания №1–№4, кейсы, рулетка и звёздное поле с кодом раунда, вывод, профиль с историей операций. Суммы показываются и в `$`, и в `⭐` по курсу `1 ⭐ = 0.017 $`.

Задания в Mini App только показываются и открывают Telegram-ссылку. Проверка подписок идёт кнопкой «Проверить задания» — она отправляет действие в чат боту, и бот запускает уже существующие начисления по заданиям №1–№4. Новых правил начисления в Mini App нет.

**Сейчас Mini App доступен только администратору (`ADMIN_IDS`).** Кнопки в боте скрыты для остальных, а API возвращает `403` даже при правильном `initData`.

## 1. Загрузить файлы

Из PowerShell в каталоге проекта:

```powershell
scp "miniapp_api.py" "deploy_miniapp.py" "deploy_miniapp_admin_only.py" "deploy_miniapp_handlers_flexible.py" "deploy_miniapp_tasks.py" "deploy_miniapp_nginx.py" root@193.39.245.135:/opt/ua-bot/
scp -r "miniapp/." root@193.39.245.135:/opt/ua-bot/miniapp/
```

## 2. Применить патчи бота

По SSH на сервере:

```bash
cd /opt/ua-bot
systemctl stop uabot
cp services.py services.py.bak_miniapp_$(date +%s)
cp handlers.py handlers.py.bak_miniapp_$(date +%s)
cp .gitignore .gitignore.bak_miniapp_$(date +%s)
venv/bin/python deploy_miniapp.py
venv/bin/python -m py_compile main.py services.py handlers.py miniapp_api.py
systemctl start uabot
systemctl status uabot --no-pager
```

Ожидаемо:

```
OK: services register miniapp routes
OK: handlers import WebAppInfo
OK: cabinet Mini App button
OK: handlers /miniapp command
OK: handlers web_app_data
OK: gitignore stats_temp.jpg
DONE
```

Если `services.py` пропатчился, а в `handlers.py` нет `cmd_miniapp` / `WebAppInfo`, примени гибкий патч:

```bash
venv/bin/python deploy_miniapp_handlers_flexible.py handlers.py
```

Если Mini App уже был установлен без админ-ограничения, дополнительно:

```bash
venv/bin/python deploy_miniapp_admin_only.py
```

Ожидаемо `OK` по админ-правкам или `SKIP`, если они уже применены.

Чтобы включить реальные задания №1–№4 в уже работающем Mini App, после загрузки новых файлов выполни инкрементальный патч:

```bash
cd /opt/ua-bot
systemctl stop uabot
cp handlers.py handlers.py.bak_miniapp_tasks_$(date +%s)
cp miniapp_api.py miniapp_api.py.bak_miniapp_tasks_$(date +%s)
cp -a miniapp miniapp.bak_miniapp_tasks_$(date +%s)
venv/bin/python deploy_miniapp_tasks.py handlers.py
venv/bin/python -m py_compile handlers.py miniapp_api.py
venv/bin/python -c "import miniapp_api; print('miniapp_api ok')"
systemctl start uabot
systemctl status uabot --no-pager
```

Ожидаемо:

```
OK: miniapp task check helpers
OK: check_tasks action
DONE
miniapp_api ok
```

При повторном запуске патч выведет `SKIP` — это нормально.

## 3. Nginx

```bash
cd /opt/ua-bot
venv/bin/python deploy_miniapp_nginx.py
nginx -t
systemctl reload nginx
```

Скрипт создаёт `/etc/nginx/snippets/uabot-miniapp.conf` и добавляет `include` в server-блок с `server_name uamoneybots.ru`. Бэкап конфига создаётся автоматически.

## 4. Проверка

Без Telegram API должен ответить `401` (это правильно):

```bash
curl -i https://uamoneybots.ru/miniapi/profile | head
```

Не-админ даже с правильным `initData` получит `403`. С включённым демо-режимом это можно проверить так:

```bash
curl -i "https://uamoneybots.ru/miniapi/profile?demo=1&user_id=1" | head
```

Статика:

```bash
curl -I https://uamoneybots.ru/miniapp/
curl -I https://uamoneybots.ru/miniapp/app.js
```

В боте:

- `/miniapp` — отдельная кнопка открытия **только у админа**; у остальных ответ про недоступность;
- `Кабинет` → кнопка `🌐 Mini App` **только у админа**;
- Profile подтягивает баланс, заморозку, уровень, реф-ссылку и историю операций;
- на главной доступны кейсы `Старт 2.5⭐`, `Про 12.5⭐`, `Гигант 50⭐` с экраном шансов: 5 призов, возврат `88 / 88 / 85%`, главные призы `50 / 250 / 500⭐`; приз и код раунда пишутся в историю;
- на главной доступна рулетка: 16 секторов, возврат 88%, максимум x2.4, ставки `1.25 / 2.5 / 5⭐`; ноль сгорает, код раунда пишется в историю;
- на главной доступно звёздное поле: 25 клеток, 6 чёрных дыр, возврат 80%, максимум x82.56, ставки `1.25 / 2.5 / 5⭐`; можно забрать в любой момент, чёрная дыра сжигает ставку, код раунда пишется в историю;
- `Задания` показывает реальные задания №1–№4 группами; ссылки открываются через Telegram;
- «Проверить задания» закрывает Mini App и присылает в чат результат проверки №1–№4;
- `Вывод` отправляет действие в чат и использует существующую логику заявок.

Логи:

```bash
grep -iE "Mini App|miniapi" /var/log/ua-bot/bot.log | tail -30
```

## Демо-режим без Telegram

Только для локального теста можно временно добавить в `.env`:

```
MINIAPP_DEMO=1
```

После этого `https://uamoneybots.ru/miniapi/profile?demo=1` отвечает данными администратора без `initData`. **На рабочем сервере оставлять `MINIAPP_DEMO=0`**, иначе API доступен без авторизации.
