"""llmbroker — a standalone, host-agnostic LLM-provider broker.

Public surface only. Protocols and DTOs are imported from their defining
modules (registry/secrets/shared_state/telemetry/models). Dependency-carrying
backends (sqlite/redis/…) are submodules imported explicitly — never from here.
"""

from llmbroker.broker import (
    AllLLMsFailedError,
    AsyncBroker,
    AsyncLLM,
    AsyncResult,
    LLMRequestError,
    NoLLMAvailableError,
    Optimizer,
)
from llmbroker.chat import arun_tool_loop, run_tool_loop
from llmbroker.models import LifecyclePhase
from llmbroker.registry import Registry
from llmbroker.secrets import DictSecrets, Secrets, SecretsReadOnlyError
from llmbroker.sync import LLM, Broker, Result
from llmbroker.telemetry import JsonlTelemetry, NoTelemetry, Telemetry

__all__ = [
    "AllLLMsFailedError",
    "AsyncBroker",
    "AsyncLLM",
    "AsyncResult",
    "Broker",
    "DictSecrets",
    "JsonlTelemetry",
    "LLM",
    "LifecyclePhase",
    "LLMRequestError",
    "NoLLMAvailableError",
    "NoTelemetry",
    "Optimizer",
    "Registry",
    "Result",
    "Secrets",
    "SecretsReadOnlyError",
    "Telemetry",
    "arun_tool_loop",
    "run_tool_loop",
]
