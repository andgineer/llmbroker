# Установка

## Установка uv

[Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

## Установка llmbroker

```bash
uv pip install llmbroker
```

Для общей БД на несколько инстансов установите соответствующий extra
(см. [Серверы и кластеры](server.md)):

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

Для управляемого хранилища секретов (см. [API-ключи и секреты](secrets.md)):

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
