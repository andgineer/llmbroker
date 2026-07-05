"""llmbroker — a standalone, host-agnostic LLM-provider broker."""

from llmbroker.broker import AsyncBroker, AsyncLLM, AsyncResult
from llmbroker.chat import arun_tool_loop, run_tool_loop
from llmbroker.exceptions import (
    AllLLMsFailedError,
    LLMRequestError,
    NoLLMAvailableError,
    SecretsReadOnlyError,
    UserScopeError,
)
from llmbroker.models import LifecyclePhase
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets, Secrets
from llmbroker.standalone.telemetry import JsonlTelemetry, NoTelemetry, Telemetry
from llmbroker.sync import LLM, Broker, Result

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
    "UserScopeError",
    "arun_tool_loop",
    "run_tool_loop",
]
