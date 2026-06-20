"""Alembic coexistence hook — autogenerate ignores every ``llmbroker_*`` object.

Wire into ``alembic/env.py``::

    import llmbroker.alembic
    context.configure(..., include_object=llmbroker.alembic.include_object)

Imports nothing from Alembic — it only inspects the object name.
"""


def include_object(
    _obj,
    name,
    _type,
    _reflected,
    _compare_to,
) -> bool:
    return not (name and name.startswith("llmbroker_"))
