# Installation

## Install uv

[Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

## Install llmbroker

```bash
uv pip install llmbroker
```

For a shared DB across instances install the matching extra
(see [Servers & clusters](server.md)):

```bash
uv pip install "llmbroker[sqlite]"
uv pip install "llmbroker[postgres]"
uv pip install "llmbroker[mongodb]"
```

For a managed secrets store (see [API keys](secrets.md)):

```bash
uv pip install "llmbroker[aws]"    # AWS Secrets Manager
uv pip install "llmbroker[vault]"  # HashiCorp Vault
```
