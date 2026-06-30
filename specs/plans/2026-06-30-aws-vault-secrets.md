# Plan: AWS Secrets Manager and HashiCorp Vault secret backends

**Source of truth:** https://github.com/andgineer/llmbroker/issues/3
Acceptance criteria, scope, and any future clarifications live there. This plan is the
implementation roadmap only.

---

## Goal

Add `llmbroker.aws.Secrets` and `llmbroker.vault.Secrets`, both satisfying
`MutableSecretsProtocol`, available as `llmbroker[aws]` and `llmbroker[vault]` optional extras.

---

## File layout

```
src/llmbroker/aws/__init__.py      # exports Secrets
src/llmbroker/aws/secrets.py
src/llmbroker/vault/__init__.py    # exports Secrets
src/llmbroker/vault/secrets.py
```

Mirror the existing backend structure exactly (see `llmbroker/postgres/` and
`llmbroker/mongodb/` as the canonical templates).

---

## pyproject.toml changes

Add two new optional extras alongside the existing storage extras:

```toml
aws   = ["aioboto3>=13.0"]
vault = ["hvac>=2.0"]
```

`aioboto3` is the async wrapper around `boto3`; it exposes the same resource/client API
but returns coroutines.  `hvac` is the official HashiCorp Python client; it is sync-only so
calls must be wrapped in `asyncio.to_thread` (see implementation notes below).

---

## `llmbroker/aws/secrets.py`

Constructor signature:
```python
class Secrets:
    def __init__(
        self,
        *,
        region_name: str | None = None,
        prefix: str = "llmbroker/",
        require_user_id: bool = False,
    ) -> None: ...
```

`aioboto3.Session().client("secretsmanager", region_name=region_name)` is used as an
**async context manager per call** — open, call, close inside each `resolve`/`set`. No
shared client state; `aclose()` is therefore a no-op. This matches the idiomatic aioboto3
pattern and avoids lifecycle complexity.

### Secret naming convention

| `user_id` | secret name in Secrets Manager        |
|-----------|---------------------------------------|
| `None`    | `{prefix}{ref}`                       |
| `"alice"` | `{prefix}{ref}/{user_id}`             |

`prefix` defaults to `"llmbroker/"`, namespacing all managed secrets away from other
secrets in the same AWS account.  Secrets created via `set()` also carry the tag
`{"Key": "llmbroker", "Value": "1"}` so they can be enumerated/cleaned up independently.

### Method contracts

`resolve(ref, user_id)`:
- Calls `check_user_id(user_id)` first (rejects empty-string user_id).
- Raises `UserScopeError` if `require_user_id=True` and `user_id is None`.
- Calls `get_secret_value(SecretId=_name(ref, user_id))`.
- On `ClientError` with code `ResourceNotFoundException` → raise `KeyError`.

`set(ref, value, user_id)`:
- Calls `check_user_id(user_id)` first.
- Raises `UserScopeError` if `require_user_id=True` and `user_id is None`.
- Tries `put_secret_value`; on `ResourceNotFoundException` falls back to
  `create_secret` (AWS has no upsert).

`aclose()`: no-op; return `None` (client is per-call, no shared state to release).

---

## `llmbroker/vault/secrets.py`

Constructor signature:
```python
class Secrets:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        mount_point: str = "secret",
        require_user_id: bool = False,
    ) -> None: ...
```

`hvac.Client` is created eagerly (no network call happens at construction).  All `hvac`
calls happen inside `asyncio.to_thread(...)` because hvac is sync.

### Secret path convention (KV v2)

| `user_id` | Vault KV path               |
|-----------|-----------------------------|
| `None`    | `llmbroker/{ref}`           |
| `"alice"` | `llmbroker/users/{user_id}/{ref}` |

### Method contracts

`resolve(ref, user_id)`:
- Calls `check_user_id(user_id)` first (rejects empty-string user_id).
- Raises `UserScopeError` if `require_user_id=True` and `user_id is None`.
- Calls `client.secrets.kv.v2.read_secret_version(path=_path(ref, user_id), mount_point=...)`.
- On `hvac.exceptions.InvalidPath` → raise `KeyError`.
- Returns `data["data"]["data"]["value"]` (KV v2 nesting).

`set(ref, value, user_id)`:
- Calls `check_user_id(user_id)` first.
- Raises `UserScopeError` if `require_user_id=True` and `user_id is None`.
- Calls `client.secrets.kv.v2.create_or_update_secret(path=..., secret={"value": value})`.

`aclose()`: no-op; return `None`.

---

## `__init__.py` for each subpackage

```python
# llmbroker/aws/__init__.py
from llmbroker.aws.secrets import Secrets

__all__ = ["Secrets"]
```

Same pattern for `llmbroker/vault/__init__.py`.

---

## Tests

### AWS — use `moto`

`moto` intercepts `boto3`/`aioboto3` HTTP calls in-process; no container needed.

Add `moto[secretsmanager]` to the `dev` group in `pyproject.toml`.

Create `tests/conftest.py` fixture (or add to the existing `conftest.py`):

```python
@pytest.fixture
def aws_secrets(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        boto3.client("secretsmanager", region_name="us-east-1").create_secret(
            Name="__moto_init__", SecretString="x"
        )
        yield llmbroker.aws.Secrets(region_name="us-east-1")
```

### Vault — use testcontainers

Add `testcontainers[vault]` (or the `testcontainers-vault` package) to `dev`.
Use the official `hashicorp/vault` Docker image in dev mode (`VAULT_DEV_ROOT_TOKEN_ID=root`).

```python
@pytest.fixture(scope="session")
def vault_url_and_token():
    from testcontainers.vault import VaultContainer
    with VaultContainer("hashicorp/vault:1.17") as vault:
        yield vault.get_url(), "root"

@pytest.fixture
async def vault_secrets(vault_url_and_token):
    url, token = vault_url_and_token
    import hvac
    yield llmbroker.vault.Secrets(url=url, token=token)
    # clean up all secrets written during the test so the session-scoped container
    # starts each test with a blank slate
    client = hvac.Client(url=url, token=token)
    try:
        keys = client.secrets.kv.v2.list_secrets(path="llmbroker/", mount_point="secret")
        for key in keys["data"]["keys"]:
            client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=f"llmbroker/{key}", mount_point="secret"
            )
    except hvac.exceptions.InvalidPath:
        pass
```

### Parametrized `mutable_secrets` fixture (`tests/test_secrets.py`)

Extend the `mutable_secrets` and `strict_mutable_secrets` fixtures to add `"aws"` and
`"vault"` params alongside `"sqlite"`, `"postgres"`, `"mongodb"`.  All existing
parametrized tests (`test_mutable_set_and_resolve`, `test_mutable_set_upserts`,
`test_mutable_two_users_isolated`, etc.) will then run against the new backends for free.

### New `stack` variants (`tests/conftest.py`)

Add two curated E2E stacks to `_ALL_STACKS` and `_stack_ctx`:

- `"scaled_aws_secrets"`: postgres Registry + AWS Secrets + redis StateStore + postgres
  Telemetry.  Models a typical cloud deployment where secrets are managed externally.
- `"scaled_vault_secrets"`: postgres Registry + Vault Secrets + redis StateStore + postgres
  Telemetry.  Same shape but HashiCorp Vault as the secret store.

Both stacks set `queryable=True` and `persistent=True`.  Add them only to `_ALL_STACKS`
(not to `_PERSISTENT_STACKS`, since the secrets port is not required for persistence
semantics).  Clean up postgres tables in `finally` blocks as the other postgres stacks do.

---

## Implementation order

1. `pyproject.toml`: add `aws` and `vault` extras; add `moto[secretsmanager]` and
   `testcontainers[vault]` (or equivalent) to `dev`.
2. `llmbroker/aws/`: implement `secrets.py` + `__init__.py`.
3. `llmbroker/vault/`: implement `secrets.py` + `__init__.py`.
4. Tests: extend fixtures and verify all parametrized cases pass.
5. `invoke pre` → green; `python -m pytest` → green.

---

## Done gate

- `invoke pre` exits 0.
- `python -m pytest` exits 0 with zero failures/skips.
- `isinstance(llmbroker.aws.Secrets(...), MutableSecretsProtocol)` is `True`.
- `isinstance(llmbroker.vault.Secrets(...), MutableSecretsProtocol)` is `True`.
