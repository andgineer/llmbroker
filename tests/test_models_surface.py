"""The package root is the application's surface: a DTO an application receives from a
public call or hands to one is one import away, and a DTO only a backend author builds
stays in its module, out of the autocomplete a host reads."""

import inspect

import llmbroker
from llmbroker import models

HOST_FACING = {
    "Call",
    "CallStatus",
    "KeyInfo",
    "LifecyclePhase",
    "LLMConfig",
    "LLMMetrics",
    "LLMSnapshot",
    "LLMState",
    "LLMStats",
    "ModelList",
    "PendingKey",
    "PoolSnapshot",
    "SyncReport",
    "Usage",
}

INTERNAL = {
    "AsyncResourceProtocol",
    "DeclaredModels",
    "PoolHealth",
}


def _classes_defined_here() -> set[str]:
    return {
        name
        for name, obj in vars(models).items()
        if inspect.isclass(obj) and obj.__module__ == models.__name__ and not name.startswith("_")
    }


def test_every_host_facing_model_is_on_the_package():
    for name in HOST_FACING:
        assert name in llmbroker.__all__, f"not exported from llmbroker: {name}"
        assert getattr(llmbroker, name) is getattr(models, name)


def test_internal_models_stay_off_the_package():
    for name in INTERNAL:
        assert hasattr(models, name)
        assert not hasattr(llmbroker, name), f"internal DTO promoted to llmbroker: {name}"


def test_no_model_is_unclassified():
    """A new DTO must be placed on one side or the other, never left to drift."""
    assert _classes_defined_here() == HOST_FACING | INTERNAL
