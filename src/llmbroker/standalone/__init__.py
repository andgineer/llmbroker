"""Zero-dependency implementations that work without any external backend.

The simplest way to use llmbroker (the path the README advertises): a config
file, env-var secrets, logging telemetry — no database, no integration code.
Available with a bare ``import llmbroker`` (re-exported as ``llmbroker.Registry``
etc.). Dependency-carrying backends live in per-driver packages instead.
"""
