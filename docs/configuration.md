# Конфигурация и переход с .env

В `.env` хранится только секрет:

```dotenv
AZURE_DEVOPS_PAT=your-token
```

Все остальные параметры находятся в JSON. Скопируйте `config.example.json` в
`config.json`, замените вымышленные значения и перезапустите MCP-подключение.
Токен должен иметь права в каждой выбранной организации; конфиг только сужает
эти права. `get_access_scope` показывает конфигурацию, а не проверяет права PAT.

```json
{
  "schema_version": 1,
  "default_organization": "first-org",
  "organizations": [
    {
      "name": "first-org",
      "url": "https://dev.azure.com/first-org",
      "projects": ["Product", "Support"],
      "default_project": "Product",
      "rules_file": null
    },
    {
      "name": "second-org",
      "url": "https://dev.azure.com/second-org",
      "projects": ["Product"]
    }
  ],
  "allowed_repositories": [
    "first-org/Product/backend",
    "first-org/Support/tools",
    "second-org/Product/backend"
  ]
}
```

Организация, проект и репозиторий — отдельные уровни. Область Git/PR задаётся
полным путём `Organization/Project/Repository`. Нет wildcard и автоматического
разрешения всех репозиториев проекта. Пустой или отсутствующий
`allowed_repositories` запрещает Git/PR; остальные инструменты чтения остаются
ограничены списками организаций и проектов. Репозиторий вне этих списков вызывает
ошибку запуска. GUID репозитория проверяется через его принадлежность разрешённому
проекту и имя. Неизвестные поля, повторяющиеся ключи и дубликаты запрещены.

`url` — корень организации/коллекции, без проекта и `/_apis`. В Azure DevOps
Services имя должно совпадать с организацией в URL. Для Azure DevOps Server
`name` — выбранное локальное имя коллекции, а `url` — её HTTPS URL.

Инструменты чтения принимают необязательный `organization`, затем `project` там,
где он нужен. При отсутствии параметров используются настроенные значения по
умолчанию (либо первые организация и проект). Одинаковые имена проектов в разных
организациях не пересекаются. Курсоры страниц привязаны к организации и запросу;
нативный курсор Azure также оборачивается сервером. После перезапуска получите
новые курсоры. `download_id` действует только внутри создавшей его организации.

Для записи разрешён единственный `BoardScope` среди всех организаций. Инструменты
записи не принимают `organization` или `project`; выбор организации чтения не
меняет область записи. Без приватных правил запись запрещена. Правила по-прежнему
задаются исполняемым Python-модулем — см. [контракт](rules.md).

## Параметры

| JSON-поле | Значение по умолчанию / назначение |
| --- | --- |
| `schema_version` | `1` |
| `$schema` | Необязательная ссылка на `config.schema.json` для редактора |
| `default_organization` | Первая из `organizations` |
| `organizations` | Обязательный непустой список |
| `organizations[].name`, `url`, `projects` | Обязательные имя, URL и список проектов |
| `organizations[].default_project` | Первый проект организации |
| `organizations[].rules_file` | `null`: чтение без приватных правил |
| `organizations[].release_url` | `null`: автоматический выбор хоста Releases |
| `allowed_repositories` | `[]`: Git/PR запрещены |
| `api_version` | `"6.1-preview"`; также поддерживается `"6.1"` для WIT |
| `http_timeout` | `30` секунд, больше 0 и не больше 300 |
| `ca_bundle` | `null`: системные CA; иначе путь к PEM |
| `download_dir` | `null`: скачивания отключены |
| `max_download_bytes` | `268435456` (256 MiB), максимум 1 GiB |
| `download_hosts` | `[]`: дополнительные разрешённые хосты скачиваний |

Относительные пути разрешаются от каталога JSON. Логи также находятся рядом
с ним в `logs/`. Конфиг не содержит токенов, но может содержать внутренние имена;
`config.json` и `config.local.json` исключены из Git.

## Запуск

```text
python -m ms_ado_mcp.server --config-file /path/config.json
python -m ms_ado_mcp.server --config-file /path/config.json --env-file /secrets/.env
```

Без аргументов используется `config.json` текущего каталога. Без `--env-file`
используется `.env` рядом с JSON. Переменная процесса `AZURE_DEVOPS_PAT` имеет
приоритет над `.env`; если она задана, файл секрета необязателен. Подстановка
переменных внутри `.env` отключена. Остальные переменные процесса не меняют конфиг.

## Миграция

Старые параметры в `.env` вызывают ошибку запуска, даже если их значения пусты.
Автоматического возврата к старому формату нет.

| Старый параметр .env | Новое поле JSON |
| --- | --- |
| `AZURE_DEVOPS_URL` | `organizations[].url` и отдельное `name` |
| `AZURE_DEVOPS_PROJECTS` | `organizations[].projects`, массив вместо строки через запятую |
| `AZURE_DEVOPS_PROJECT` | `organizations[].default_project` и включение в `projects` |
| `ADO_ALLOWED_REPOSITORIES` | `allowed_repositories`, массив с добавленным префиксом организации |
| `ADO_RULES_FILE` | `organizations[].rules_file` |
| `ADO_RELEASE_URL` | `organizations[].release_url` |
| `AZURE_DEVOPS_API_VERSION` | `api_version` |
| `ADO_HTTP_TIMEOUT` | `http_timeout` |
| `ADO_CA_BUNDLE` | `ca_bundle` |
| `ADO_DOWNLOAD_DIR` | `download_dir` |
| `ADO_MAX_DOWNLOAD_BYTES` | `max_download_bytes` |
| `ADO_DOWNLOAD_HOSTS` | `download_hosts`, массив |

Сначала создайте JSON, перенесите параметры и обновите аргументы MCP-клиента,
затем оставьте в `.env` только `AZURE_DEVOPS_PAT` и перезапустите сервер.
