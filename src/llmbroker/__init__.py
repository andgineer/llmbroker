"""llmbroker — a standalone, host-agnostic LLM-provider broker."""

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.report import format_report
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.chat import arun_tool_loop, run_tool_loop
from llmbroker.direct import AsyncDirectClient, DirectClient, DirectResult
from llmbroker.exceptions import (
    AuthError,
    EmptyRegistryError,
    InvalidProviderResponseError,
    LLMBrokerError,
    LLMRequestError,
    LLMTimeoutError,
    MissingKeyError,
    NoLLMAvailableError,
    PoolModelError,
    ProviderError,
    RateLimitError,
    StreamInterruptedError,
    SyncRefusedError,
    ToolLoopLimitError,
    UnknownModelError,
)
from llmbroker.models import (
    LifecyclePhase,
    PendingKey,
    PoolSnapshot,
    Retirement,
    SyncReport,
)
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets, Secrets
from llmbroker.standalone.store import FileStore, InMemoryStore
from llmbroker.sync import LLM, Broker, Result

__all__ = [
    "AsyncBroker",
    "AsyncDirectClient",
    "AsyncLLM",
    "AsyncResult",
    "AuthError",
    "Broker",
    "DictSecrets",
    "DirectClient",
    "DirectResult",
    "EmptyRegistryError",
    "FileStore",
    "InMemoryStore",
    "InvalidProviderResponseError",
    "LLM",
    "LifecyclePhase",
    "LLMBrokerError",
    "LLMRequestError",
    "LLMTimeoutError",
    "MissingKeyError",
    "NoLLMAvailableError",
    "Optimizer",
    "PendingKey",
    "PoolModelError",
    "PoolSnapshot",
    "ProviderError",
    "RateLimitError",
    "Retirement",
    "StreamInterruptedError",
    "SyncRefusedError",
    "SyncReport",
    "ToolLoopLimitError",
    "UnknownModelError",
    "Registry",
    "Result",
    "Secrets",
    "arun_tool_loop",
    "format_report",
    "run_tool_loop",
]
