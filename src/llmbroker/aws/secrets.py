"""AWS Secrets Manager-backed mutable secrets store."""

import aioboto3
from botocore.exceptions import ClientError

from llmbroker.exceptions import UserScopeError
from llmbroker.models import check_user_id


class Secrets:
    """AWS Secrets Manager-backed mutable secrets store.

    A secretsmanager client is opened as an async context manager per call;
    there is no shared client state, so ``aclose`` is a no-op. ``endpoint_url``
    targets a non-default endpoint (e.g. LocalStack or a VPC endpoint).
    """

    def __init__(
        self,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        prefix: str = "llmbroker/",
        require_user_id: bool = False,
    ) -> None:
        self._session = aioboto3.Session()
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        self._prefix = prefix
        self._require_user_id = require_user_id

    def _name(self, ref: str, user_id: int | str | None) -> str:
        if user_id is None:
            return f"{self._prefix}{ref}"
        return f"{self._prefix}{ref}/{user_id}"

    def _client(self):
        return self._session.client(
            "secretsmanager",
            region_name=self._region_name,
            endpoint_url=self._endpoint_url,
        )

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "aws.Secrets: user_id is required (require_user_id=True) but received None",
            )
        async with self._client() as client:
            try:
                response = await client.get_secret_value(SecretId=self._name(ref, user_id))
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                raise KeyError(f"aws.Secrets: ref {ref!r} not found") from exc
            return response["SecretString"]

    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "aws.Secrets: user_id is required (require_user_id=True) but received None",
            )
        name = self._name(ref, user_id)
        async with self._client() as client:
            try:
                await client.put_secret_value(SecretId=name, SecretString=value)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                await client.create_secret(
                    Name=name,
                    SecretString=value,
                    Tags=[{"Key": "llmbroker", "Value": "1"}],
                )

    async def aclose(self) -> None:
        return
