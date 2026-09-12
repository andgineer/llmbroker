# API keys

Each model configuration names the API key it requires. The configured secrets
store determines where the value comes from. By default, llmbroker uses
environment variables and `.env`. [`llmbroker env`](cli.md#env) creates a
template with the required variable names and links for obtaining the keys.

The hints are also available through the API. Each missing key has a `help`
field in the [pool state](monitoring.md#pool-health) and the
[sync report](usage.md#sync). You can use those fields to show which keys are
required without maintaining a separate provider directory.

A model without a key is not used; the remaining models continue to work. This
rule applies to every source below.

## The environment and `.env`

If no separate secrets store is configured, the broker checks environment
variables first and then `.env` in the working directory. A missing file is not
an error. The standard library parses the file: `KEY=VALUE` lines and `#`
comments are supported, but variable interpolation is not. To use another path,
set `secrets=llmbroker.Secrets("/etc/llmbroker.env")`.

## Keys from code

```python
secrets = llmbroker.DictSecrets({"GROQ_API_KEY": "gsk_..."})
```

The broker's `secrets=` parameter also accepts a synchronous or asynchronous
function of the form `(name) -> value`. This provides a small interface for
custom storage.

## AWS Secrets Manager

Install the `llmbroker[aws]` optional dependencies. See
[Installation](installation.md).

```python
from llmbroker.aws import Secrets as AwsSecrets

async with llmbroker.AsyncBroker(
    "postgresql://host/db",
    secrets=AwsSecrets(region_name="us-east-1"),
) as broker:
    reply = await broker.ask("Hello")
```

Secret names use `llmbroker/{key name}` by default. The prefix is configurable.

## HashiCorp Vault {#vault}

Install the `llmbroker[vault]` optional dependencies:

```python
from llmbroker.vault import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")
```

This integration uses KV v2 and the path `llmbroker/{key name}`. Set the mount
point with `mount_point=`.

[Per-user keys](server.md#multiuser) require one additional rule. Vault treats
`/` as a path separator, so `u-42/GROQ_API_KEY` is stored as the single segment
`u-42__GROQ_API_KEY`. This difference is visible only when browsing Vault
directly. Key names and scopes that already contain `__` are not supported and
must be renamed.

## Keys in a database

A broker configured with a database (`Broker("broker.db")`, PostgreSQL, or
MongoDB) stores keys in that database by default. See
[Servers & clusters](server.md#datasource). You can configure the key source
separately:

```python
# database for models and the journal; environment variables for keys
llmbroker.AsyncBroker("postgresql://host/db", secrets=llmbroker.Secrets())
```

Keys stored this way are lost when the `llmbroker_*` tables are dropped, which is
how an llmbroker release that changes the schema is installed. Export them first;
see [Upgrading llmbroker](server.md#upgrade).

## A key per user

In a multi-user application, the broker looks for a user-specific key before the
shared key. Store a user key as `<scope>/<key name>`, for example
`u-42/GROQ_API_KEY`. An object created with `for_scope("u-42")` uses that value
instead of the shared one. See [Multi-user applications](server.md#multiuser).
