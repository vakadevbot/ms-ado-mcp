# ms-ado-mcp

Локальный MCP-сервер для Azure DevOps: Python 3.12, stdio, PAT из `.env`,
REST API **6.1-preview** (по умолчанию) или **6.1**.
Поддерживает Azure DevOps Services и Server с HTTPS URL коллекции.

## Быстрый запуск

```bash
git clone https://github.com/vakadevbot/ms-ado-mcp.git
cd ms-ado-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
mkdir -p private
cp examples/rules.example.py private/rules.py
```

В `.env` заполните `AZURE_DEVOPS_URL`, `AZURE_DEVOPS_PAT` и `AZURE_DEVOPS_PROJECT`.
PAT: **Work Items → Read & write**; для чтения достаточно Read.
В `private/rules.py` задайте имя проекта, реальные названия типов и свои правила.
Пример содержит **вымышленные проверки**, а не ваши корпоративные требования.

```bash
.venv/bin/ms-ado-mcp --env-file /absolute/path/ms-ado-mcp/.env
```

Сервер ждёт MCP-сообщения на stdin; отсутствие вывода при запуске нормально.
stdout занят протоколом, диагностика идёт в stderr. На Windows используйте
`py -3.12 -m venv .venv`, `.venv\Scripts\python.exe` и `.venv\Scripts\ms-ado-mcp.exe`;
файлы скопируйте средствами вашей оболочки.

## OpenCode v2

Добавьте в `opencode.json` проекта, заменив оба пути абсолютными:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "ado": {
        "type": "local",
        "command": [
          "/absolute/path/ms-ado-mcp/.venv/bin/ms-ado-mcp",
          "--env-file",
          "/absolute/path/ms-ado-mcp/.env"
        ]
      }
    }
  }
}
```

Проверка подключения: `opencode mcp list`; в интерфейсе — `/mcps`.
Пример запроса: «Покажи активные задачи, назначенные мне, в Azure DevOps».
Промпт подготовки: `/ado:prepare_work_item task "Your Project"`.
В v2 серверы находятся в `mcp.servers`; [схема подключения](https://dev.opencode.ai/v2/docs/mcp-servers/).
Используется стандартный legacy MCP handshake (SDK 1.x, зависимость ограничена `<2`).

## Инструменты

| Инструмент | Назначение |
| --- | --- |
| `get_project_rules` | Проекты, типы, значения по умолчанию, инструкции и правила |
| `list_active_work_items` | Активные US, Task, Feature, PI Goal |
| `search_work_items` | Подстрока заголовка либо точный числовой ID, любые состояния |
| `get_work_item` | Все поля и связи одного work item |
| `get_work_item_hierarchy` | Непосредственные родители и дети с полями |
| `create_user_story` / `create_task` | Создание с обязательной проверкой |
| `validate_work_item` | Проверка существующего элемента по `item_id` либо черновика |

Логические типы: `user_story`, `task`, `feature`, `pi_goal`; реальные названия
задаются для каждого проекта. Активность определяется категорией **InProgress**
в [метаданных состояний Azure](https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/work-item-type-states/list?view=azure-devops-rest-7.1):
так учитываются пользовательские состояния вроде «В анализе».
Для другого определения активности настройте `active_categories` или `active_states`.
Уберите из `work_item_types` типы, отсутствующие в вашем процессе.

Списки поддерживают `kinds`, `assigned_to` (включая `@Me`), `area_path`,
`iteration_path`, `limit` (1–200). Для следующей страницы передавайте
`next_after_id` как `after_id` и возвращённый `as_of`.
Поиск через WIQL, без Search extension; описание не ищется.
Смешанные одинарные и двойные кавычки в одном фильтре не поддерживаются.
Иерархия — один уровень, в пределах одного проекта.

## Приватные правила и проекты

Удобнее хранить `rules.py` **вне публичного checkout** или в отдельном приватном
репозитории, а в `.env` указать `ADO_RULES_FILE=/absolute/path/company/rules.py`.
`private/`, `.env*` и `*.local.py` исключены из Git; файл примера публичный.
Относительные пути правил и CA разрешаются относительно `.env`, не рабочего каталога OpenCode.
Переменные окружения имеют приоритет над `.env`.

Модуль экспортирует `PROJECTS = [ProjectPolicy(...), ...]`.
Каждый `Rule` содержит **инструкцию и исполняемую проверку рядом**.
Из тех же объектов сервер формирует инструкции при подключении, MCP-промпт,
ресурс `ado://rules` и результаты `get_project_rules`.
ИИ видит правила заранее, а создание независимо повторяет проверки.
Нет применимых правил, проверка упала или вернула ошибку — запись не выполняется.
Чтобы работать только на чтение без модуля, оставьте `ADO_RULES_FILE` пустым
и задайте `AZURE_DEVOPS_PROJECT`.

YAML/JSON подходят для обязательных полей и перечислений, но зависимости между
полями, родительские связи и условия удобнее выразить Python-функциями.
Поддерживаются обычные и `async` функции; контракт и примеры —
[docs/rules.md](docs/rules.md), шаблон — [examples/rules.example.py](examples/rules.example.py).
После изменения правил перезапустите MCP-подключение.
Это доверенный локальный Python-код. Тексты правил и рабочие данные передаются
в OpenCode и выбранную им модель; исключение из Git не скрывает их от ИИ.

Проверка черновика не подменяет серверные правила Azure. Значения, которые Azure
назначает после создания, в черновике отсутствуют; нужные для ваших проверок значения
задавайте в `defaults`. При сетевой ошибке создания сначала проверьте, появился ли
элемент: автоматических повторов записи нет.

## Разработка

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Слои: `client.py` — REST, `service.py` — операции, `policy.py` — контракт правил,
`server.py` — MCP tools/prompts/resources. Новую операцию добавляйте в сервис и
регистрируйте инструмент в сервере; корпоративный модуль менять не требуется.
Тесты используют подставной HTTP transport и реальное stdio-подключение MCP,
не создают work items в Azure.

`AZURE_DEVOPS_API_VERSION=6.1-preview` выбирает ревизии отдельно для WIQL (`.2`),
work items (`.3`), batch и states (`.1`). Значение `6.1` передаётся без preview.
Совместимость конкретной установки Azure Server нужно проверить с её URL/PAT.
Для корпоративного CA используйте `ADO_CA_BUNDLE=/path/ca.pem`; TLS-проверка включена.
