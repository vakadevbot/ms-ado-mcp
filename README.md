# ms-ado-mcp

Локальный MCP-сервер для Azure DevOps: Python 3.12, stdio, PAT из `.env`,
WIT REST API **6.1-preview** (по умолчанию) или **6.1**.
Builds, classic Releases, Git и Pull Requests доступны только для чтения,
с отдельными версиями REST API (см. ниже).
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
| `search_my_work_items` | Поиск моих work items; по умолчанию созданных мной, опционально назначенных на меня |
| `list_my_active_work_items` | Только мои активные work items: назначение на меня и активные состояния проекта |
| `create_my_work_item` | Создать Task/User Story от моего имени и назначить на меня, с проверкой правил |
| `search_work_items` | Подстрока заголовка либо точный числовой ID, любые состояния |
| `get_work_item` | Все поля и связи одного work item |
| `get_work_item_hierarchy` | Непосредственные родители и дети с полями |
| `create_user_story` / `create_task` | Создание с обязательной проверкой |
| `update_work_item` | Изменить созданную мной Task/User Story только в разрешённой области, с проверкой правил и ревизии |
| `validate_work_item` | Проверка существующего элемента по `item_id` либо черновика |
| `validate_my_active_work_items` | Проверить все мои активные work items, с итогом и нарушениями по каждому |
| `validate_my_work_items` | Проверить мои элементы во всех состояниях, ограничив итерацией или датой создания |

Логические типы: `user_story`, `task`, `feature`, `pi_goal`; реальные названия
задаются для каждого проекта. Активность определяется категорией **InProgress**
в [метаданных состояний Azure](https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/work-item-type-states/list?view=azure-devops-rest-7.1):
так учитываются пользовательские состояния вроде «В анализе».
Для другого определения активности настройте `active_categories` или `active_states`.
Уберите из `work_item_types` типы, отсутствующие в вашем процессе.

Списки поддерживают `kinds`, `assigned_to` (включая `@Me`), `area_path`,
`iteration_path`, `created_since`, `created_before`, `limit` (1–200).
Для следующей страницы передавайте
`next_after_id` как `after_id` и возвращённый `as_of`.
Поиск через WIQL, без Search extension; описание не ищется.
Смешанные одинарные и двойные кавычки в одном фильтре не поддерживаются.
Иерархия — один уровень, в пределах одного проекта.

### Ограничения больших выборок

Списки и поиск **во всех состояниях** требуют хотя бы одного ограничения:
`created_since` (дата начала) или `iteration_path` конкретного спринта/PI.
Это касается поиска по заголовку в `search_my_work_items`, `search_work_items`
и массовой проверки `validate_my_work_items`. Одних `created_before`,
названия, области, ответственного или лимита страницы недостаточно.
Корень проекта вместо итерации также не разрешает выгрузку всей истории.

Пример поиска: `search_work_items(text="Авторизация", created_since="2026-08-01")`.
Для спринта/PI задайте полный `iteration_path`; выбираются и дочерние итерации.
Можно сочетать итерацию и даты. `created_since` включается в диапазон,
`created_before` исключается. Фильтруется дата **создания**, а не изменения.
Дата `YYYY-MM-DD` означает полночь UTC; местное время задаётся ISO-строкой
с часовым поясом, например `2026-08-01T00:00:00+03:00`.

**Текущие активные work items не требуют даты или итерации**:
`list_my_active_work_items`,
`validate_my_active_work_items`, а также списки/поиск с `active_only=true`.
Никакая дата не подставляется автоматически: старые, но всё ещё активные
элементы также входят в выборку. Даты доступны как необязательный фильтр.
По умолчанию выбираются все типы из `work_item_types`, включая Story, Feature
и другие настроенные типы, а не только Task. Активные состояния определяются
политикой проекта. Чтобы получить весь список, последовательно прочитайте страницы
до отсутствия `next_after_id`, сохраняя фильтры и `as_of`.

Получение одного элемента по ID, его иерархии и точный поиск по числовому ID
не требуют временного фильтра.

### Моя учётная запись и мои work items

Отдельный ID пользователя в `.env` **не нужен**. Сервер работает от имени
владельца `AZURE_DEVOPS_PAT`: Azure автоматически определяет его для WIQL-макроса
`@Me` и записывает как автора (`System.CreatedBy`) при создании. Если PAT принадлежит
сервисной или чужой учётке, «мои» означает именно эту учётку. Для работы от своего
имени используйте личный PAT с доступом к проекту.

- `get_current_user()` показывает ID, отображаемое имя и логин владельца PAT.
  Учётка запрашивается через `/_apis/connectionData`, без ручного ввода ID.
- `search_my_work_items(text="Авторизация", created_since="2026-08-01")` ищет
  созданные вами элементы по фрагменту заголовка или точному числовому ID.
  По умолчанию `relationship="created"`; для назначенных на вас передайте
  `relationship="assigned"`. Для активных —
  `active_only=true`. Доступны `text` (фрагмент заголовка или ID), `kinds`, `project`,
  `area_path`, `iteration_path`, `created_since`, `created_before`
  и пагинация через `after_id`/`as_of`.
  Поиск ограничен настроенными в проекте типами work items; активность определяется
  политикой проекта. Для чтения через `@Me` запрос текущей учётки не требуется.
- `list_my_active_work_items()` сразу находит только назначенные на вас активные
  элементы. Отдельно передавать `active_only` или пользователя не нужно.
  По умолчанию это категория InProgress, включая пользовательские состояния;
  настройки `active_categories` и `active_states` из правил проекта учитываются.
  Поддерживаются поиск по `text`, типы `kinds`, проект, область, итерация и пагинация.
- `create_my_work_item(kind="task", title="Моя задача")` получает ваш логин,
  устанавливает `System.AssignedTo` и запускает обычную проверку правил перед записью.
  Для User Story передайте `kind="user_story"`. Поддерживаются `description`,
  `fields` и `parent_id`. Не передавайте `System.AssignedTo` в `fields`:
  инструмент назначает элемент на владельца PAT. Если Azure не вернул логин,
  создание остановится, а не выберет пользователя по неоднозначному имени.

Все инструменты создания уже работают от имени владельца PAT; отличие
`create_my_work_item` — автоматическое назначение на себя. Подмена автора
и обход правил Azure не используются. Для чтения достаточно PAT с Work Items Read,
для создания нужны Read & write и права на проект. Без применимых правил
в `ADO_RULES_FILE` создание остаётся заблокированным.

Примеры запросов в OpenCode:

- «Покажи, под какой учёткой я подключён к Azure DevOps».
- «Найди мои work items про авторизацию за пятый PI, включая новые и закрытые».
- «Найди именно мои активные воркайтемы».
- «Найди созданные мной work items со словом авторизация».
- «Создай задачу на меня: исправить авторизацию, сначала проверь правила проекта».

Общие списки `list_active_work_items` и `list_my_work_items` удалены из MCP.
Для общей выборки используйте ограниченный `search_work_items`, для личной —
`search_my_work_items`. Инструмент `get_work_item(item_id=123)` возвращает один
work item по ID без обязательных временных фильтров. Чтение чужих элементов
и их связей не даёт права редактировать их.

### Массовая проверка моих work items

Два инструмента применяют те же проверки, что `validate_work_item(item_id=...)`,
к элементам, **назначенным на владельца PAT** (`System.AssignedTo = @Me`):

- `validate_my_active_work_items()` — все мои активные элементы. Активность и типы
  определяются политикой проекта, как в `list_my_active_work_items`.
- `validate_my_work_items(created_since="2026-08-01")` — мои элементы во всех
  состояниях, созданные начиная с 1 августа 2026 года.
- `validate_my_work_items(iteration_path="Your Project\\PI 5")` — мои элементы
  в пятом PI, включая дочерние спринты. Для второго спринта укажите полный путь
  к нему, например `Your Project\\PI 5\\Sprint 2`.

У второго инструмента **обязателен** `created_since` или путь к спринту/PI
в `iteration_path`. Пустая итерация, корень проекта и один только `created_before`
не разрешают проверять всю историю. Итерацию и даты можно сочетать; эти фильтры
также доступны у инструмента проверки активных элементов.

`created_since` включается в диапазон, `created_before` исключается:
например, `created_since="2026-08-01", created_before="2026-09-01"` выбирает август.
Фильтры относятся к **дате создания**, а не изменения. Дата `YYYY-MM-DD` означает
полночь UTC; для местного времени используйте ISO с часовым поясом, например
`2026-08-01T00:00:00+03:00`. Без явно заданного года сервер дату не угадывает.
Можно дополнительно ограничить `project` и `kinds`.

Ответ содержит `selected_count`, `valid_count`, `invalid_count`, `error_count`
и `results` с результатом проверки или ошибкой для каждого ID. Отсутствие правил
считается нарушением, ошибка чтения — отдельной ошибкой, а не успешной проверкой.
Ошибка одного элемента не прерывает проверку остальных. Пустая выборка возвращает
нулевые счётчики. Никакие поля work items не меняются; `BoardScope` для чтения
и проверки не требуется, ограничения записи сохраняются.

За вызов проверяется до `max_items` элементов (по умолчанию 200, максимум 1000),
с автоматическим чтением страниц. Если `complete=false`, нужно продолжить:
передать `next_after_id` как `after_id`, возвращённый `as_of` и те же фильтры.
Не называйте проверку всей выборки завершённой до `complete=true`.
При наличии `as_of` состав выборки фиксируется на время первого поиска;
каждый элемент и его связи проверяются в актуальном состоянии на момент чтения.

Примеры запросов в OpenCode: «Проверь все мои активные work items»,
«Проверь мои work items, созданные с 1 августа 2026»,
«Проверь мои элементы только во втором спринте пятого PI».

## Builds, classic Releases, Git и PR: только чтение

Все следующие инструменты имеют `readOnlyHint=true`, `destructiveHint=false`.
Они отправляют только GET, не запускают и не отменяют pipelines/releases,
не создают PR/комментарии, не голосуют, не меняют политики, файлы, ветки или теги.
Скачивание пишет только на локальный диск. Ограничения записи work items,
включая `BoardScope`, владельца PAT и `test /rev`, сохраняются.

Нужны PAT **Build → Read**, **Release → Read**, **Code → Read** и доступ
к выбранному проекту. Секреты и корпоративные адреса заполняйте локально.
`project` может обозначать только проект из существующих правил; без правил —
`AZURE_DEVOPS_PROJECT`. ID репозитория не обходит проверку его проекта.

| Область | Инструменты |
| --- | --- |
| Build pipelines (YAML и classic) | `list_pipeline_definitions`, `list_pipeline_runs`, `get_pipeline_run` |
| Шаги и артефакты Build | `get_pipeline_run_steps`, `get_pipeline_step_log`, `list_pipeline_artifacts`, `download_pipeline_artifact` |
| Classic Releases («Выпуски») | `list_release_definitions`, `list_releases`, `get_release`, `get_release_steps`, `get_release_task_log`, `download_release_logs`, `list_release_artifacts` |
| Pull Requests | `search_pull_requests`, `get_pull_request`, `get_pull_request_changes`, `get_pull_request_threads`, `get_pull_request_statuses` |
| Репозитории | `list_repositories`, `get_repository`, `list_repository_refs` |
| Файлы | `list_repository_files`, `search_repository_files`, `get_repository_file`, `download_repository_file`, `download_repository_archive` |
| Коммиты | `list_repository_commits`, `get_repository_commit` |
| Локальные ZIP | `list_download_archive`, `get_download_archive_file` |

### Время и страницы

`since` — обязательная нижняя граница исторических списков запусков, releases,
коммитов и PR со статусами completed/abandoned/all. `before` задаёт верхнюю
границу. Даты: `YYYY-MM-DD` (полночь UTC) или ISO с часовым поясом; начало
должно предшествовать концу. Получение объекта по ID даты не требует.

- Builds: **время постановки в очередь**, `queryOrder=queueTimeDescending`.
  `minTime`/`maxTime` передаются Azure как границы этого времени, согласно его API;
  они не относятся ко времени начала или окончания. Текущие `inProgress`,
  `notStarted`, `postponed`, `cancelling` не требуют даты.
- Releases: **дата создания** (`minCreatedTime`/`maxCreatedTime`), нижняя граница
  обязательна даже для `active`: активный release может быть историческим.
- PR: **creationDate**, `since` включается, `before` исключается. Для текущих
  `active` дата необязательна. В API 6.0 нет серверных фильтров дат PR:
  за вызов читается одна ограниченная страница и фильтруется локально.
  Пустая страница с продолжением не означает конец. История может потребовать
  много вызовов; скрытой автоматической выгрузки всех PR нет.
- Коммиты: серверные `searchCriteria.fromDate/toDate` по дате коммита.

`limit` — 1–200, по умолчанию 100. Ответы списков содержат `items`,
`continuation_token`, `has_more`, `incomplete` и параметры/версию выборки.
Передавайте токен в следующий вызов с теми же фильтрами. Build definitions/runs,
Release definitions/releases и Git refs используют настоящий Azure continuation
token. PR, policies и commits используют документированные offset-параметры.
PR и policies — живая выборка: изменения состава во время обхода могут сдвинуть
страницы; это не снимок базы. Для аудита меняющейся выборки повторите обход.

Для API без пагинации (например, repositories, threads, statuses, timeline)
реализованы локальные страницы с проверкой отпечатка ответа. Если источник
изменился, продолжение отклоняется: начните заново. JSON-ответ ограничен 8 МиБ;
при превышении выдаётся ошибка, а не ложный полный результат. Локальные курсоры
подписаны, привязаны к запросу и действуют только до перезапуска сервера.

### Pipelines и classic Releases

```text
list_pipeline_definitions(name="Example CI", folder="\\Examples")
list_pipeline_runs(definition_id=12, since="2026-11-01", status="completed", result="failed", branch="refs/heads/main")
get_pipeline_run(build_id=123)
get_pipeline_run_steps(build_id=123)
get_pipeline_step_log(build_id=123, log_id=7, start_line=0, line_count=200)
list_releases(since="2026-11-01", definition_id=8, status="active", environment_status=8, definition_environment_id=3)
get_release_steps(release_id=42)
```

Имена определений проверяются на точное совпадение; неоднозначность или ещё
непрочитанные страницы требуют использовать ID. Для Builds доступны `build_number`
(допускает `*` в конце), статус, результат, ветка, определение и диапазон времени.
Timeline сохраняет `parentId`, `attempt`, `previousAttempts`, `log.id`.
Если есть `details.id`, его можно передать как `timeline_id` для вложенного timeline.

Classic Releases — самостоятельная сущность; YAML stages остаются в Build timeline.
`status` release: `active`, `draft`, `abandoned`. `environment_status` — отдельная
битовая маска Azure: 1 notStarted, 2 inProgress, 4 succeeded, 8 canceled,
16 rejected, 32 queued, 64 scheduled, 128 partiallySucceeded; значения можно объединять.
Этот фильтр требует `definition_environment_id` выбранного окружения определения.
Шаги возвращаются отдельными записями environment/deployment/phase/job/task со
связями, номером попытки и `release_deploy_phase_id`. Этот ID вместе с
`release_id`, `environment_id`, `task_id` выбирает конкретный лог попытки.
Источники release-артефактов сохраняют `definitionReference`, включая build ID
в `version.id` для типа Build. Ссылки на источники других проектов не обходятся.

Логи читаются окнами `start_line` (с нуля), `line_count` (до 1000),
`max_bytes` (до 256 КиБ). `truncated` и `byte_limit_reached` показывают ограничения.
При наличии `next_start_line` продолжайте с него. Если одна строка превысила
байтовый лимит, точного продолжения нет: увеличьте лимит либо скачайте логи release.
Логи работающего процесса могут дополняться; ответ отражает момент чтения.

Для Services автоматически выбирается `https://vsrm.dev.azure.com/<organization>`
(включая исходный URL `<organization>.visualstudio.com`). Для Server используется
URL коллекции. `ADO_RELEASE_URL` позволяет переопределить его локально; облачный
адрес для on-premises не требуется.

### PR и проверки

```text
search_pull_requests(personal="created", status="active")
search_pull_requests(repository="example-repo", personal="review")
search_pull_requests(status="completed", since="2026-11-01", target_branch="refs/heads/main")
get_pull_request_changes(repository="example-repo", pull_request_id=24)
get_pull_request_statuses(repository="example-repo", pull_request_id=24)
```

Дополнительные фильтры: `creator_id`, `reviewer_id`, `source_branch`, `target_branch`.
Личные режимы определяют ID владельца PAT через connectionData. `personal="review"`
выбирает активные PR, где он reviewer с голосом 0; групповые назначения без
индивидуальной записи reviewer не разворачиваются. Изменения PR фиксируют выбранную
итерацию в курсоре; `compare_to=0` сравнивает с общим предком, другой ID — с итерацией.
Обсуждения содержат комментарии и статусы тредов.

Проверки выдаются двумя последовательными источниками `source=statuses/policies`:
следуйте курсору, чтобы получить оба. При недоступности одного API (403/404)
возвращаются `unavailable` и `incomplete=true`, доступный источник остаётся доступен.
Никаких изменений checks/policies/reviewers инструмент не выполняет.

### Git: версии, поиск файлов и ограничения

```text
list_repository_refs(repository="example-repo", prefix="heads/feature/")
search_repository_files(repository="example-repo", version="main", version_type="branch", directory="/src", extensions=[".py"], exclude=["*/generated/*"])
get_repository_file(repository="example-repo", path="/README.md", version="v1.0", version_type="tag")
list_repository_commits(repository="example-repo", version="main", since="2026-11-01", path="/src")
```

Версия обязательна: `version_type=branch/tag/commit`; commit — полный SHA из
40 hex-символов. Имена веток передаются как ветки через `versionDescriptor`,
без неявного выбора default branch. Первый вызов разрешает выбранную версию;
обход файлов и список коммитов фиксируют SHA. Смена ветки между страницами
не меняет дерево. Чтение и скачивание файла также фиксируют найденный commit.

Файлы обходятся через нерекурсивные Git Trees: до 20 деревьев и 2000 просмотренных
записей за вызов. Остаток обхода хранится в подписанном курсоре. Ограничение очереди —
1000 каталогов; при превышении нужно сузить `directory`. При `truncated` от Azure
результат помечается `incomplete=true`, включая последние страницы.
Несуществующие `$top`/continuationToken в Git Items/Trees не отправляются.

Фильтры регистрозависимы: `name` (подстрока имени), `path_contains`, `prefix`,
`suffix`, `extensions` (суффиксы, например `.yaml`), `glob`, `exclude` (список масок).
Маски используют Python fnmatch по абсолютному POSIX-пути; `*` совпадает также с `/`.
Подкаталоги обходятся независимо от фильтров результатов. Полнотекстовый Code Search
не реализован; поиск имён/путей не зависит от расширения Azure Search.

Текст читается как UTF-8 с ограничением `max_bytes` и диапазоном строк. Байтовый
лимит относится к префиксу файла: строки за его пределами требуют скачивания.
Для бинарных/non-UTF-8 файлов возвращаются метаданные и предложение скачать.
Git LFS pointer отмечается `lfs_pointer=true`; настоящие LFS-объекты не разрешаются.
Скачивание сохраняет pointer как есть (`lfs_resolved=false`).

### Локальные скачивания и архивы

Задайте `ADO_DOWNLOAD_DIR` локально (относительно `.env` или абсолютный путь).
Пустое значение отключает скачивание. MCP-клиент не задаёт имя или путь назначения:
файл получает случайное имя. Ответ содержит `download_id`, `local_path`, `size`,
`sha256`. Скачанные данные не удаляются после завершения; удаляйте их локально.
Идентификатор для просмотра ZIP действует только в текущем сеансе сервера.

```text
download_pipeline_artifact(build_id=123, artifact_name="drop")
download_release_logs(release_id=42)
download_repository_archive(repository="example-repo", version="main", directory="/src")
list_download_archive(download_id="<ID из результата>")
get_download_archive_file(download_id="<ID из результата>", member="README.md", line_count=100)
```

Build-артефакты типа **Container** скачиваются как ZIP через сконструированный Build
endpoint. PipelineArtifact, FilePath и прочие типы пока дают явную ошибку —
произвольный `resource.downloadUrl` не используется. Release logs и Git archives
также получаются через фиксированные маршруты. Скачивание потоковое; по умолчанию
до 256 МиБ, `ADO_MAX_DOWNLOAD_BYTES` допускает 1 байт – 1 ГиБ. Ошибка, обрыв,
превышение лимита или опасный ZIP удаляют временный `.part`.

Редиректы по умолчанию запрещены. Разрешить внешнее хранилище можно только локально
через `ADO_DOWNLOAD_HOSTS`: точные HTTPS-hostnames, без wildcard, порт 443, максимум
четыре перехода. При любом переходе Authorization/PAT и cookies не передаются,
даже обратно на исходный host. URL с credentials блокируются; TLS всегда проверяется.
Не добавляйте недоверенные хосты в локальный список.

ZIP не распаковываются и файлы не выполняются. Проверяются traversal, абсолютные
пути, symlinks, дубликаты, encryption, ZIP bombs и размер central directory.
Лимиты: 10 000 записей, каталог 4 МиБ, 1 ГиБ суммарно распакованных данных,
256 МиБ на запись, коэффициент сжатия до 200. ZIP64/multidisk не поддерживаются.
Просмотр члена архива читает только небольшой UTF-8 файл (до `max_bytes`, максимум
256 КиБ); для большего возвращает метаданные и `truncated=true`.

Диагностика идёт в существующий `logs/ms-ado-mcp.log`. Не логируются тела HTTP,
PAT, signed URL, cookies и Authorization. Структурированные download URL удаляются,
секретные переменные Release скрываются. Содержимое файлов, логов и комментариев
может само содержать чувствительные данные: оно передаётся MCP-клиенту как
**недоверенные данные**, а не инструкции для агента.

### Версии API и проверка

Build, обычные Release и Git операции используют документированные стабильные
маршруты 6.0, независимо от настройки WIT. Исключения: Release logs/task logs —
`6.0-preview.2`, Git Trees/PR statuses и Policy evaluations — `6.0-preview.1`.
Параметры сверены с [Build REST](https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/list?view=azure-devops-rest-6.0),
[Release REST](https://learn.microsoft.com/en-us/rest/api/azure/devops/release/releases/list?view=azure-devops-rest-6.0),
[Git Items](https://learn.microsoft.com/en-us/rest/api/azure/devops/git/items/list?view=azure-devops-rest-6.0),
[PR REST](https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/get-pull-requests?view=azure-devops-rest-6.0)
и официальным [SDK Microsoft](https://github.com/microsoft/azure-devops-python-api)
(`azure-devops==6.0.0b4`, только справочник, не зависимость проекта).

Тесты используют `httpx.MockTransport` и реальный локальный MCP stdio; корпоративные
данные не нужны. Проверяются GET-only, границы проекта, фильтры и версии, курсоры,
SHA, ограничения текста/архивов, удаление недокачанных файлов, отсутствие PAT на
download-host и прежние ограничения записи work items. Живая проверка на конкретном
Azure DevOps Services/Server требует локального URL/PAT и здесь не заявляется.

## Приватные правила и проекты

### Запись только в область своей команды

В Azure доска отображает элементы по настройкам команды, прежде всего **Area Path**.
В приватном `rules.py` укажите **точный Area Path вашей команды** через `BoardScope`:

```python
from ms_ado_mcp.policy import BoardScope, ProjectPolicy

# Фрагмент существующей политики: сохраните свои rules, defaults и work_item_types.
# Значения заполните локально, не публикуйте корпоративные пути.
policy = ProjectPolicy(
    name="Your Project",
    write_scope=BoardScope(
        area_path="Your Project\\Your Team",
        iteration_path="Your Project\\PI 5\\Sprint 4",
    ),
    # rules=(...),  # Ваши обязательные проверки по-прежнему нужны.
)
```

Добавьте `write_scope` в свой существующий `ProjectPolicy` внутри `PROJECTS`.
Только **один** проект в `PROJECTS` может иметь `write_scope`; остальные доступны
для чтения. **Без `write_scope` все операции записи запрещены**, даже если правила
проверки уже заданы. После обновления добавьте эту настройку локально и перезапустите
OpenCode. Готовая форма настройки есть в `examples/rules.example.py`.

У инструментов `create_task`, `create_user_story`, `create_my_work_item` и
`update_work_item` нет параметра проекта или доски. Сервер сам выбирает разрешённый
проект и при создании подставляет область и настроенную итерацию. Названия доски
и служебных полей для обычного запроса ИИ передавать не нужно.
Через `fields` нельзя передать область, итерацию, колонку/дорожку доски, тип или
проект — даже альтернативным регистром имени поля. `defaults` не могут переопределить
фиксированную область. Родитель при создании также должен быть в разрешённой области
и создан владельцем PAT: добавление связи может менять обратную связь родителя.

Создавать и редактировать можно только **Task и User Story**. Feature и остальные
типы доступны только для чтения. Для правки достаточно ID и нужных изменений:

```text
update_work_item(item_id=123, title="Исправленное название", state="In Progress")
```

Также доступны `description` и необязательный `fields` для корпоративных полей.
Неуказанные поля и связи не меняются. Перед PATCH сервер читает элемент, проверяет
точное совпадение области/итерации и типа, затем проверяет полный изменённый элемент
корпоративными правилами. Дополнительно сервер получает текущую учётку из Azure
и сравнивает её неизменяемый ID с `System.CreatedBy.id` **до записи**.
Назначения элемента на вас, совпадения отображаемого имени или email недостаточно.
Если автор другой или Azure не вернул ID автора, изменение запрещено.
Для этой проверки не нужен дополнительный ID в `.env`: «я» — владелец PAT.
При создании автора задаёт Azure по тому же PAT; передавать `System.CreatedBy`
и `System.ChangedBy` через `fields` запрещено.

В PATCH включён `test /rev`: если другой пользователь
изменил или переместил элемент после чтения, запись отклоняется; автоматического
повтора нет. После конфликта нужно заново прочитать элемент.

Область сравнивается **точно**, без автоматического разрешения дочерних областей.
При создании сервер всегда задаёт конкретную итерацию: сначала берёт
`BoardScope.iteration_path`, а если она не задана — `System.IterationPath` из
приватных `defaults` соответствующего типа. Значение из `defaults` теперь сохраняется.
Без такой настройки или при указании корня проекта создание блокируется
**до отправки запроса**, вместо молчаливого создания вне спринта.
Если в `BoardScope` итерация не задана, она не ограничивает обычное редактирование.
Автоматического выбора «текущего спринта» нет: целевую итерацию задайте локально
в политике и меняйте её при переходе на следующий спринт.

Уже созданный в корне элемент можно перенести через MCP:

```text
update_work_item(item_id=123, use_configured_iteration=true)
```

Путь назначения инструмент не принимает: он выбирается по той же приватной политике.
При таком переносе исходная итерация может отличаться от фиксированной, но
исходная область команды должна точно совпадать, тип — Task/User Story,
а автор — владелец PAT. Целевой элемент повторно проверяется правилами и
`BoardScope`, PATCH защищён ревизией. Чужие элементы и другие области перенести нельзя.
Попытка передать `System.IterationPath` через `fields` по-прежнему запрещена.
Доски могут показывать одну и ту же область: это ограничение по владению элементами
команды, а не гарантия, что они визуально отображаются только на одной доске.
Для ограничения конкретным спринтом задайте также точную итерацию.

Существующее чтение и поиск работают в рамках настроенных проектов;
ограничение записи не зависит от того, какие элементы ИИ прочитал.

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
work items (`.3`), batch и states (`.1`). Значение `6.1` передаётся без preview
для WIT. `connectionData` всегда использует `6.1-preview.1`: этот endpoint preview-only.
Для запросов WIQL включён `timePrecision=true`; фильтры дат передаются в UTC с `Z`.
Совместимость конкретной установки Azure Server нужно проверить с её URL/PAT.
Для корпоративного CA используйте `ADO_CA_BUNDLE=/path/ca.pem`; TLS-проверка включена.
