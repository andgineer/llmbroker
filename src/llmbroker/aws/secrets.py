"""AWS Secrets Manager-backed mutable secrets store."""

import aioboto3
from botocore.exceptions import ClientError


class Secrets:
    """AWS Secrets Manager-backed mutable secrets store. A client is opened per
    call, so there is no shared state and ``aclose`` is a no-op."""

    def __init__(
        self,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        prefix: str = "llmbroker/",
    ) -> None:
        self._session = aioboto3.Session()
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        self._prefix = prefix

    def _name(self, ref: str) -> str:
        return f"{self._prefix}{ref}"

    def _client(self):
        return self._session.client(
            "secretsmanager",
            region_name=self._region_name,
            endpoint_url=self._endpoint_url,
        )

    async def resolve(self, ref: str) -> str:
        async with self._client() as client:
            try:
                response = await client.get_secret_value(SecretId=self._name(ref))
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                raise KeyError(f"aws.Secrets: ref {ref!r} not found") from exc
            return response["SecretString"]

    async def set(self, ref: str, value: str) -> None:
        name = self._name(ref)
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
