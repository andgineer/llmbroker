"""Zero-dependency storage core: declarative table spec, the ``Driver`` protocol,
generic ports written once against any ``Driver``, and an in-memory reference driver.

Importable by a bare ``import llmbroker`` — no driver package required. Each DB
backend package (``sqlite``, ``postgres``, ``mongodb``) supplies one ``Driver``
implementation and wraps the generic ports in its facade classes.
"""
