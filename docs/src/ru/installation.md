# Установка

## Установка uv

[Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

## Установка llmbroker

```bash
uv pip install llmbroker
```

Этого достаточно: `Broker()` работает без единого extra.

### Хранение в БД

llmbroker хранит список моделей, ключи и журнал вызовов — по умолчанию в своём
каталоге на машине. Чтобы вместо этого была БД — в том числе одна на несколько
инстансов — установите её extra (см. [Серверы и кластеры](server.md)):

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

### Ключи в хранилище секретов

Ключи llmbroker читает из окружения и `.env`, а если ему назвали БД — из неё.
Чтобы они лежали в отдельном хранилище секретов (см. [API-ключи](secrets.md)):

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
