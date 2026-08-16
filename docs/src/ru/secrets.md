# API-ключи

Каждая модель в списке моделей ссылается на свой ключ по имени; откуда берётся
значение — решает подключённый бэкенд секретов. По умолчанию — переменные
окружения и `.env`:

```bash
llmbroker env freetier > .env
```

Команда печатает заготовку с подсказкой над каждым ключом, где его получить.
Те же подсказки доступны и вашему коду — через возможность реестра отдавать
key-info.

Модель без ключа просто остаётся неактивной — пул работает на тех ключах, что
есть. Это верно для любого источника ниже.

## Окружение и `.env`

Брокер без собственного источника читает `.env` из рабочего каталога как
запасной источник. Переменная окружения всегда важнее файла, а отсутствующий
`.env` — просто пустой запасной источник. Никаких зависимостей: файл разбирается
стандартной библиотекой (строки `KEY=VALUE`, комментарии `#`, без подстановок).
Указать другой файл явно: `secrets=llmbroker.Secrets("/etc/llmbroker.env")`.

## Ключи из кода

```python
secrets = llmbroker.DictSecrets({"GROQ_API_KEY": "gsk_..."})
```

`secrets=` брокера принимает и обычную функцию `(имя) -> значение`, синхронную
или асинхронную — минимальный способ подключить любое своё хранилище.

## AWS Secrets Manager

Установите extra `llmbroker[aws]` ([Установка](installation.md)):

```python
from llmbroker.aws import Secrets as AwsSecrets

async with llmbroker.AsyncBroker(
    "postgresql://host/db",
    secrets=AwsSecrets(region_name="us-east-1"),
) as llms:
    reply = await llms.ask("Привет")
```

Имена секретов — `llmbroker/{имя ключа}`; префикс настраивается.

## HashiCorp Vault

Установите extra `llmbroker[vault]`:

```python
from llmbroker.vault import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")
```

KV v2, путь `llmbroker/{имя ключа}`; точка монтирования настраивается через
`mount_point=`.

## Ключи в БД

Брокер, созданный из БД (`Broker("broker.db")`, Postgres, MongoDB), хранит ключи
в той же БД — см. [Серверы и кластеры](server.md#datasource). Любую комбинацию
можно собрать явно:

```python
# БД для всего, но ключи из окружения
llmbroker.AsyncBroker("postgresql://host/db", secrets=llmbroker.Secrets())
```

## Свой ключ на пользователя

В многопользовательском приложении ключ ищется сначала по пользователю, потом
общий — см. [scope](server.md#multiuser).
