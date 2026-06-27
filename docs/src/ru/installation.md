# Установка

## Установка uv

[Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

## Установка llmbroker

```bash
uv pip install llmbroker
```

Для использования бэкенда установите соответствующий extra:

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[redis]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```
