"""All exceptions a caller of llmbroker may catch, in one place."""


class LLMRequestError(Exception):
    """Base: this request could not be completed."""


class NoLLMAvailableError(LLMRequestError):
    """No LLM slot came free within ``wait``."""


class AllLLMsFailedError(LLMRequestError):
    """A slot was obtained but every tried LLM errored."""
