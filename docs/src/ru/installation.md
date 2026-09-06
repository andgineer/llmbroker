# Установка

## Установка uv

[Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

## Установка llmbroker

```bash
uv pip install llmbroker
```

Этого достаточно для использования `Broker()` без дополнительных зависимостей.

### Хранение в базе данных

По умолчанию llmbroker хранит список моделей, ключи и журнал вызовов в локальном
каталоге. Чтобы использовать базу данных, в том числе общую для нескольких
экземпляров приложения, установите соответствующую группу зависимостей. Подробнее
см. в разделе [Серверы и кластеры](server.md).

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

### Ключи в хранилище секретов

По умолчанию llmbroker читает ключи из переменных окружения и файла `.env`. При
настройке базы данных ключи могут храниться в ней. Для отдельного хранилища
секретов установите соответствующую группу зависимостей. Подробнее см. в разделе
[API-ключи](secrets.md).

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
