"""Alembic coexistence hook — autogenerate ignores every ``llmbroker_*`` object.
Wiring is in ``docs/`` "Servers & clusters"; nothing is imported from Alembic, the
hook only inspects the object name."""


def include_object(
    _obj,
    name,
    _type,
    _reflected,
    _compare_to,
) -> bool:
    return not (name and name.startswith("llmbroker_"))
