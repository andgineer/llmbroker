"""Zero-dependency storage core: the table spec, the ``Driver`` protocol, the ports
written once against it, and an in-memory reference driver. A DB backend package
supplies one ``Driver`` and wraps these ports in its facade classes."""
