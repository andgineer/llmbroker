"""A host tells failure states apart by type, so every exception it may catch has to
be reachable on the package itself — not only from the module that defines it."""

import inspect

import llmbroker
from llmbroker import exceptions


def _defined_here() -> set[str]:
    return {
        name
        for name, obj in vars(exceptions).items()
        if inspect.isclass(obj)
        and issubclass(obj, Exception)
        and obj.__module__ == exceptions.__name__
    }


def test_every_exception_is_exported_from_the_package():
    defined = _defined_here()
    assert defined
    missing = defined - set(llmbroker.__all__)
    assert not missing, f"not exported from llmbroker: {sorted(missing)}"
    for name in defined:
        assert getattr(llmbroker, name) is getattr(exceptions, name)


def test_a_stall_and_a_mid_stream_death_are_different_states():
    """ "The stream died" and "my own gap fired" are different things to handle, so
    neither type is the other's subclass; a host wanting both catches the base."""
    assert not issubclass(llmbroker.StreamStalledError, llmbroker.StreamInterruptedError)
    assert not issubclass(llmbroker.StreamInterruptedError, llmbroker.StreamStalledError)
    for name in ("StreamStalledError", "StreamInterruptedError"):
        assert issubclass(getattr(llmbroker, name), llmbroker.LLMRequestError)


def test_the_lifecycle_errors_a_host_catches_at_startup():
    """The three that stop a broker before it serves, all one catch away."""
    for name in ("EmptyRegistryError", "SyncRefusedError", "SchemaVersionError"):
        assert issubclass(getattr(llmbroker, name), llmbroker.LLMBrokerError)
    assert issubclass(llmbroker.LLMBrokerError, RuntimeError)
