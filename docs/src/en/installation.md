# Installation

## Install uv

[Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install llmbroker

```bash
uv pip install llmbroker
```

That is enough: `Broker()` works with no extra at all.

### Storing in a DB

llmbroker keeps a model list, keys and a call journal — by default in its own
directory on the machine. To put that in a DB instead — including one shared
across instances — install that DB's extra (see
[Servers & clusters](server.md)):

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

### Keys in a secrets store

llmbroker reads keys from the environment and `.env`, or from the DB where you
named one. To keep them in a secrets store of their own (see
[API keys](secrets.md)):

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
