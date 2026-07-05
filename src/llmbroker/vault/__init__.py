"""HashiCorp Vault backend for llmbroker.

Needs the ``hvac`` client (``llmbroker[vault]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. Uses KV v2.
"""

from llmbroker.vault.secrets import Secrets

__all__ = ["Secrets"]
