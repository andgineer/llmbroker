"""HashiCorp Vault backend for llmbroker.

Needs the ``hvac`` client (``llmbroker[vault]``). Uses KV v2.
"""

from llmbroker.vault.secrets import Secrets

__all__ = ["Secrets"]
