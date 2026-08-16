# API keys

Every model in the model list refers to its key by name; where the value comes
from is up to the configured secrets backend. The default is environment
variables and `.env`, and the skeleton with the hints is printed by
[`llmbroker env`](cli.md#env).

The same hints reach your own code ready-made: every missing key in the
[pool snapshot](monitoring.md#pool-health) carries a `help` field, and so does
every one in the [sync report](usage.md#sync). So a "what is missing" screen is
built without a provider directory of your own.

A model without a key simply stays inactive — the pool runs on whatever keys are
present. That holds for every source below.

## The environment and `.env`

A broker with no source of its own reads the `.env` in its working directory as a
fallback. An exported environment variable always wins over the file, and a
missing `.env` is simply no fallback. Nothing is installed for this: the file is
parsed with the standard library (`KEY=VALUE` lines, `#` comments, no
interpolation). Point it elsewhere explicitly with
`secrets=llmbroker.Secrets("/etc/llmbroker.env")`.

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
) as broker:
    reply = await broker.ask("Hello")
```

Secret names are `llmbroker/{key name}`; the prefix is configurable.

## HashiCorp Vault {#vault}

Install the `llmbroker[vault]` extra:

```python
from llmbroker.vault import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")
```

KV v2, path `llmbroker/{key name}`; the mount point is configurable via
`mount_point=`.

One quirk applies to [per-user keys](server.md#multiuser). A slash is a directory
separator in KV, so the name `u-42/GROQ_API_KEY` is flattened into the single
segment `u-42__GROQ_API_KEY` in the path. You only ever see that browsing the
store by hand; but a ref that already contains `__` is refused by this backend —
rename the ref or the scope.

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
one. A user's key goes into the same store under the name `<scope>/<REF>` —
`u-42/GROQ_API_KEY`, say — and a caller built with `for_scope("u-42")` picks it up
instead of the shared one. In full — [scope](server.md#multiuser).
