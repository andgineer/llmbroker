"""The async broker engine: the AsyncBroker façade plus its live-pool collaborators.

Implementation lives in focused sibling modules — broker / catalog / router /
pool_view / pool / result / state. Request exceptions live in ``llmbroker.exceptions``
and the optimizer knob in ``llmbroker.optimizer``.
"""
