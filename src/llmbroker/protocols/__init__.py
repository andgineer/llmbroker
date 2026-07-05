"""Backend contracts: what a registry / secrets / knowledge store must do.

Implement these to add a custom backend. The zero-dependency implementations
ship in ``llmbroker.standalone``; dependency-carrying ones in per-driver
packages (``llmbroker.sqlite``, …).
"""
