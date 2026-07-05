"""HashiCorp Vault KV v2-backed mutable secrets store."""

import asyncio

import hvac
import hvac.exceptions


class Secrets:
    """HashiCorp Vault KV v2-backed mutable secrets store.

    ``hvac`` is sync-only; all calls run inside ``asyncio.to_thread``.
    ``aclose`` is a no-op.
    """

    def __init__(self, url: str, token: str, *, mount_point: str = "secret") -> None:
        self._client = hvac.Client(url=url, token=token)
        self._mount_point = mount_point

    def _path(self, ref: str) -> str:
        return f"llmbroker/{ref}"

    async def resolve(self, ref: str) -> str:
        try:
            response = await asyncio.to_thread(
                self._client.secrets.kv.v2.read_secret_version,
                path=self._path(ref),
                mount_point=self._mount_point,
            )
            return response["data"]["data"]["value"]
        except hvac.exceptions.InvalidPath as exc:
            raise KeyError(f"vault.Secrets: ref {ref!r} not found") from exc

    async def set(self, ref: str, value: str) -> None:
        await asyncio.to_thread(
            self._client.secrets.kv.v2.create_or_update_secret,
            path=self._path(ref),
            secret={"value": value},
            mount_point=self._mount_point,
        )

    async def aclose(self) -> None:
        return
