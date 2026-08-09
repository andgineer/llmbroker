"""HashiCorp Vault (KV v2) backend. Needs ``hvac`` (``llmbroker[vault]``); importing
this package is how a host declares that dependency."""

from llmbroker.vault.secrets import Secrets

__all__ = ["Secrets"]
