# Installation

## Install uv

[Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install llmbroker

```bash
uv pip install llmbroker
```

To use a database backend, install the matching extra:

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[redis]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

To use a managed secret store:

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
