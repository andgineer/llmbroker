"""HashiCorp Vault KV v2-backed mutable secrets store."""

import asyncio

import hvac
import hvac.exceptions

from llmbroker.exceptions import UserScopeError
from llmbroker.models import check_user_id


class Secrets:
    """HashiCorp Vault KV v2-backed mutable secrets store.

    ``hvac`` is sync-only; all calls run inside ``asyncio.to_thread``.
    ``aclose`` is a no-op.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        mount_point: str = "secret",
        require_user_id: bool = False,
    ) -> None:
        self._client = hvac.Client(url=url, token=token)
        self._mount_point = mount_point
        self._require_user_id = require_user_id

    def _path(self, ref: str, user_id: int | str | None) -> str:
        if user_id is None:
            return f"llmbroker/{ref}"
        return f"llmbroker/users/{user_id}/{ref}"

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "vault.Secrets: user_id is required (require_user_id=True) but received None",
            )
        try:
            response = await asyncio.to_thread(
                self._client.secrets.kv.v2.read_secret_version,
                path=self._path(ref, user_id),
                mount_point=self._mount_point,
            )
            return response["data"]["data"]["value"]
        except hvac.exceptions.InvalidPath as exc:
            raise KeyError(f"vault.Secrets: ref {ref!r} not found") from exc

    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "vault.Secrets: user_id is required (require_user_id=True) but received None",
            )
        await asyncio.to_thread(
            self._client.secrets.kv.v2.create_or_update_secret,
            path=self._path(ref, user_id),
            secret={"value": value},
            mount_point=self._mount_point,
        )

    async def aclose(self) -> None:
        return
