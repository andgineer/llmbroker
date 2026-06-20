import pytest
import llmbroker


_REAL_BROKER_SYNC_CONFIGS = llmbroker.AsyncBroker.sync_configs


@pytest.fixture
def real_broker_sync(monkeypatch):
    """Restore the real ``AsyncBroker.sync_configs`` for tests that exercise it directly.

    ``_disable_llm_broker_sync`` stubs it out to prevent lifespan seeding from
    interfering with other tests; tests that specifically validate ``sync_configs``
    behaviour declare this fixture to get the original back.
    """
    monkeypatch.setattr(llmbroker.AsyncBroker, "sync_configs", _REAL_BROKER_SYNC_CONFIGS)
