"""HashiCorp Vault KV v2-backed mutable secrets store."""

import asyncio

import hvac
import hvac.exceptions

# A slash is a directory separator in a KV path, so a scoped ref stored raw would
# make a LIST answer with folder names instead of refs. Flattened, it is one segment.
_SEPARATOR = "__"


class Secrets:
    """HashiCorp Vault KV v2-backed mutable secrets store.

    ``hvac`` is sync-only; all calls run inside ``asyncio.to_thread``.
    ``aclose`` is a no-op.
    """

    def __init__(self, url: str, token: str, *, mount_point: str = "secret") -> None:
        self._client = hvac.Client(url=url, token=token)
        self._mount_point = mount_point

    def _path(self, ref: str) -> str:
        if _SEPARATOR in ref:
            raise ValueError(
                f"vault.Secrets: ref {ref!r} contains {_SEPARATOR!r}, which this backend"
                " uses to keep a scoped ref inside one KV path segment — rename the ref"
                " or the scope",
            )
        return f"llmbroker/{ref.replace('/', _SEPARATOR)}"

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

    async def refs(self, prefix: str = "") -> frozenset[str]:
        """Every ref under llmbroker's own path, narrowed by ``prefix``. An empty
        path is not an error here — nothing has been stored yet."""
        try:
            response = await asyncio.to_thread(
                self._client.secrets.kv.v2.list_secrets,
                path="llmbroker",
                mount_point=self._mount_point,
            )
        except hvac.exceptions.InvalidPath:
            return frozenset()
        stored = (str(key).replace(_SEPARATOR, "/") for key in response["data"]["keys"])
        return frozenset(ref for ref in stored if ref.startswith(prefix))

    async def aclose(self) -> None:
        return
