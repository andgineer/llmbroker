"""llmbroker — a standalone, host-agnostic LLM-provider broker."""

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.llms import AsyncLLMs
from llmbroker.broker.report import format_report
from llmbroker.broker.result import AsyncLLM, AsyncResult, StreamHandle
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
    SchemaVersionError,
    StreamInterruptedError,
    SyncRefusedError,
    ToolLoopLimitError,
    UnknownCallError,
    UnknownModelError,
)
from llmbroker.models import (
    Call,
    CallStatus,
    LifecyclePhase,
    LLMConfig,
    LLMMetrics,
    LLMSnapshot,
    LLMState,
    LLMStats,
    PendingKey,
    PoolSnapshot,
    SyncReport,
    Usage,
)
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.secrets import DictSecrets, Secrets
from llmbroker.standalone.store import FileStore, InMemoryStore
from llmbroker.sync import LLM, Broker, LLMs, Result
from llmbroker.tool_loop import arun_tool_loop, run_tool_loop

__all__ = [
    "AsyncBroker",
    "AsyncDirectClient",
    "AsyncLLM",
    "AsyncLLMs",
    "AsyncResult",
    "AuthError",
    "Broker",
    "Call",
    "CallStatus",
    "DictSecrets",
    "DirectClient",
    "DirectResult",
    "EmptyRegistryError",
    "FileStore",
    "InMemoryStore",
    "InvalidProviderResponseError",
    "LLM",
    "LLMs",
    "LifecyclePhase",
    "LLMBrokerError",
    "LLMConfig",
    "LLMMetrics",
    "LLMRequestError",
    "LLMSnapshot",
    "LLMState",
    "LLMStats",
    "LLMTimeoutError",
    "MissingKeyError",
    "NoLLMAvailableError",
    "Optimizer",
    "PendingKey",
    "PoolModelError",
    "PoolSnapshot",
    "ProviderError",
    "RateLimitError",
    "SchemaVersionError",
    "StreamHandle",
    "StreamInterruptedError",
    "SyncRefusedError",
    "SyncReport",
    "ToolLoopLimitError",
    "UnknownCallError",
    "UnknownModelError",
    "Result",
    "Secrets",
    "Usage",
    "arun_tool_loop",
    "format_report",
    "run_tool_loop",
]
