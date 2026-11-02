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
.venv/bin/python -m ms_ado_mcp.server --env-file /absolute/path/ms-ado-mcp/.env
```

Сервер ждёт MCP-сообщения на stdin; отсутствие вывода при запуске нормально.
stdout занят протоколом, диагностика идёт в stderr.

### Windows (PowerShell)

Установите Python 3.12 с Python Launcher (`py`) и Git for Windows.
Откройте PowerShell в каталоге, где хотите разместить проект:

```powershell
py -3.12 --version
git clone https://github.com/vakadevbot/ms-ado-mcp.git
cd ms-ado-mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
New-Item -ItemType Directory -Force private | Out-Null
Copy-Item examples\rules.example.py private\rules.py
notepad .env
notepad private\rules.py
```

Заполните URL, PAT и проект в `.env`, а в `private\rules.py` настройте проект,
типы и правила, как описано выше. Для доступа только на чтение без правил
оставьте `ADO_RULES_FILE=` пустым. При повторной настройке сохраните существующие
`.env` и `private\rules.py`: команды `Copy-Item` перезаписывают эти файлы.

Запустите сервер из каталога проекта:

```powershell
.\.venv\Scripts\python.exe -m ms_ado_mcp.server --env-file "$PWD\.env"
```

Активация виртуального окружения не требуется; менять Execution Policy для
этих команд не нужно. Сервер ожидает MCP-запросы; для остановки нажмите `Ctrl+C`.

В конфигурации OpenCode ниже замените пути на Windows-пути с прямыми слешами,
например `C:/Users/YourName/ms-ado-mcp/.venv/Scripts/python.exe` и
`C:/Users/YourName/ms-ado-mcp/.env`. Если в JSON используются обратные слеши,
их нужно удваивать (`C:\\Users\\YourName\\ms-ado-mcp\\.env`).

Для разработки установите дополнительные зависимости и запустите проверки:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

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
          "/absolute/path/ms-ado-mcp/.venv/bin/python",
          "-m",
          "ms_ado_mcp.server",
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

### OpenCode v2 на Windows

После установки сервера по инструкции PowerShell выше убедитесь, что команда
`opencode --version` доступна в новом окне PowerShell. Перейдите в каталог
проекта, с которым будете работать в OpenCode (он может отличаться от каталога
`ms-ado-mcp`), и откройте его конфигурацию:

```powershell
cd C:\Users\YourName\Projects\MyProject
notepad opencode.json
```

Добавьте следующую конфигурацию, заменив `C:/Users/YourName/ms-ado-mcp` на
абсолютный путь к установленному серверу. Если файл уже содержит настройки,
добавьте сервер `ado` в `mcp.servers`, сохранив остальные параметры.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "ado": {
        "type": "local",
        "command": [
          "C:/Users/YourName/ms-ado-mcp/.venv/Scripts/python.exe",
          "-m",
          "ms_ado_mcp.server",
          "--env-file",
          "C:/Users/YourName/ms-ado-mcp/.env"
        ]
      }
    }
  }
}
```

Прямые слеши допустимы в Windows-путях и не требуют экранирования в JSON.
Каждый аргумент остаётся отдельной строкой массива, даже если путь содержит
пробелы. OpenCode запускает Python из `.venv`, который выполняет
`src/ms_ado_mcp/server.py` как модуль `ms_ado_mcp.server`. Активация `.venv`
и ручной запуск сервера перед OpenCode не требуются.

Используйте `-m ms_ado_mcp.server`: запуск файла командой
`python src/ms_ado_mcp/server.py` не поддерживается из-за относительных импортов
внутри пакета. Установка через `pip install -e .` делает модуль доступным
из любого рабочего каталога.

Из того же каталога проверьте подключение и запустите OpenCode:

```powershell
opencode mcp list
opencode
```

В интерфейсе откройте `/mcps` и проверьте подключение `ado`, затем запросите:
«Покажи правила проекта Azure DevOps». Для проверки чтения из Azure запросите:
«Покажи активные задачи, назначенные мне, в Azure DevOps».
После изменения `.env`, правил или конфигурации перезапустите OpenCode.
Если сервер не подключается, проверьте оба пути и запустите команду
`.\.venv\Scripts\python.exe -m ms_ado_mcp.server --env-file "$PWD\.env"` из каталога
`ms-ado-mcp`, чтобы увидеть диагностику конфигурации в stderr.

## Инструменты

| Инструмент | Назначение |
| --- | --- |
| `get_project_rules` | Проекты, типы, значения по умолчанию, инструкции и правила |
| `get_current_user` | Текущая учётная запись Azure DevOps — владелец настроенного PAT |
| `list_my_work_items` | Назначенные на меня или созданные мной work items, с поиском и фильтрами |
| `create_my_work_item` | Создать Task/User Story от моего имени и назначить на меня, с проверкой правил |
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

### Моя учётная запись и мои work items

Отдельный ID пользователя в `.env` **не нужен**. Сервер работает от имени
владельца `AZURE_DEVOPS_PAT`: Azure автоматически определяет его для WIQL-макроса
`@Me` и записывает как автора (`System.CreatedBy`) при создании. Если PAT принадлежит
сервисной или чужой учётке, «мои» означает именно эту учётку. Для работы от своего
имени используйте личный PAT с доступом к проекту.

- `get_current_user()` показывает ID, отображаемое имя и логин владельца PAT.
  Учётка запрашивается через `/_apis/connectionData`, без ручного ввода ID.
- `list_my_work_items()` возвращает назначенные на вас элементы во всех состояниях.
  Для созданных вами передайте `relationship="created"`; для активных —
  `active_only=true`. Доступны `text` (фрагмент заголовка или ID), `kinds`, `project`,
  `area_path`, `iteration_path` и пагинация через `after_id`/`as_of`.
  Поиск ограничен настроенными в проекте типами work items; активность определяется
  политикой проекта. Для чтения через `@Me` запрос текущей учётки не требуется.
- `create_my_work_item(kind="task", title="Моя задача")` получает ваш логин,
  устанавливает `System.AssignedTo` и запускает обычную проверку правил перед записью.
  Для User Story передайте `kind="user_story"`. Поддерживаются `description`,
  `project`, `fields` и `parent_id`. Не передавайте `System.AssignedTo` в `fields`:
  инструмент назначает элемент на владельца PAT. Если Azure не вернул логин,
  создание остановится, а не выберет пользователя по неоднозначному имени.

Все инструменты создания уже работают от имени владельца PAT; отличие
`create_my_work_item` — автоматическое назначение на себя. Подмена автора
и обход правил Azure не используются. Для чтения достаточно PAT с Work Items Read,
для создания нужны Read & write и права на проект. Без применимых правил
в `ADO_RULES_FILE` создание остаётся заблокированным.

Примеры запросов в OpenCode:

- «Покажи, под какой учёткой я подключён к Azure DevOps».
- «Покажи все мои задачи, включая новые и закрытые».
- «Найди созданные мной work items со словом авторизация».
- «Создай задачу на меня: исправить авторизацию, сначала проверь правила проекта».

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

### Логи ошибок

Ошибки конфигурации и запросов к Azure DevOps записываются в
`logs/ms-ado-mcp.log` **рядом с выбранным `.env`**, независимо от рабочего
каталога OpenCode. Папка создаётся автоматически при запуске сервера.
Например, для `--env-file C:/Users/YourName/ms-ado-mcp/.env` лог находится в
`C:/Users/YourName/ms-ado-mcp/logs/ms-ado-mcp.log`.

Лог содержит время ошибки, HTTP-статус, имя сервера, тип операции и подсказки
для ошибок сети, DNS, TLS, прокси и тайм-аута. PAT, заголовки авторизации,
содержимое запросов/ответов и тексты исключений приватного кода не записываются.
Размер файла ограничен 2 МБ, сохраняются три предыдущих файла (`.1`–`.3`).
`logs/` исключён из Git. Если папка недоступна для записи, сервер сообщает об этом
в stderr и продолжает выводить ошибки в MCP-клиент.

Просмотр последних ошибок в PowerShell:

```powershell
Get-Content U:\Users\YourName\projects\ms-ado-mcp\logs\ms-ado-mcp.log -Tail 50
```

### Проверки

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
