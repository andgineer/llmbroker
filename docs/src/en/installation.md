# Installation

## Install uv

[Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install llmbroker

```bash
uv pip install llmbroker
```

That is enough to use `Broker()` without optional dependencies.

### Database storage

By default, llmbroker keeps the model list, keys, and call journal in a local
directory. To use a database, including one shared by several application
instances, install the matching optional dependency. See
[Servers & clusters](server.md).

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

### Keys in a secrets store

By default, llmbroker reads keys from environment variables and `.env`. When a
database is configured, keys can be stored there. Install the matching optional
dependency to use a dedicated secrets store. See [API keys](secrets.md).

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
