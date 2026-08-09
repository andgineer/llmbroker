# API keys

Every model in the lineup refers to its key by name; where the value comes from
is up to the configured secrets backend. The default is environment variables
and `.env`:

```bash
llmbroker env > .env
```

The command prints a skeleton with a hint above each key, where to get it. The
same hints reach your own code through the registry's key-info capability.

A broker with no source of its own reads the `.env` in its working directory as a
fallback. An exported environment variable always wins over the file, and a
missing `.env` is simply no fallback. Nothing is installed for this: the file is
parsed with the standard library (`KEY=VALUE` lines, `#` comments, no
interpolation). Point it elsewhere explicitly with
`secrets=llmbroker.Secrets("/etc/llmbroker.env")`.

A model without a key simply stays inactive — the pool runs on whatever keys are
present.

## Keys from code

```python
secrets = llmbroker.DictSecrets({"GROQ_API_KEY": "gsk_..."})
```

The broker's `secrets=` also accepts a plain function `(name) -> value`, sync or
async — the minimal way to plug in any storage of your own.

## AWS Secrets Manager

Install the `llmbroker[aws]` extra ([Installation](installation.md)):

```python
from llmbroker.aws import Secrets as AwsSecrets

async with llmbroker.AsyncBroker(
    "postgresql://host/db",
    secrets=AwsSecrets(region_name="us-east-1"),
) as llms:
    reply = await llms.ask("Hello")
```

Secret names are `llmbroker/{key name}`; the prefix is configurable.

## HashiCorp Vault

Install the `llmbroker[vault]` extra:

```python
from llmbroker.vault import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")
```

KV v2, path `llmbroker/{key name}`; the mount point is configurable via
`mount_point=`.

## Keys in a DB

A broker created from a DB (`Broker("broker.db")`, Postgres, MongoDB) keeps the
keys in that same DB — see [Servers & clusters](server.md#datasource). Any
combination can be assembled explicitly:

```python
# DB for everything, but keys from the environment
llmbroker.AsyncBroker("postgresql://host/db", secrets=llmbroker.Secrets())
```

## A key per user

In a multi-user application the key is looked up by user first, then the shared
one — see [scope](server.md#multiuser).
