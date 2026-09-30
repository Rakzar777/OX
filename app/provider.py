import re

import httpx


class ProviderError(Exception):
    def __init__(self, message="ЮKassa временно недоступна", definite=False):
        super().__init__(message)
        self.definite = definite


class YooKassa:
    def __init__(self, settings):
        self.client = httpx.AsyncClient(base_url="https://api.yookassa.ru/v3/",
            auth=(settings.shop_id, settings.secret_key), timeout=20, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, payload=None, key=None):
        try:
            result = await self.client.request(method, path, json=payload,
                headers={"Idempotence-Key": key} if key else {})
        except httpx.HTTPError:
            raise ProviderError() from None
        if result.is_error:
            # Never log responses: they can contain buyer details.
            raise ProviderError(f"Ошибка ЮKassa HTTP {result.status_code}",
                                definite=result.status_code in (400, 401, 403, 404, 422))
        try:
            return result.json()
        except ValueError:
            raise ProviderError() from None

    async def create_payment(self, payload, key):
        return await self.request("POST", "payments", payload, key)

    async def get_payment(self, id):
        self.validate_id(id)
        return await self.request("GET", "payments/" + id)

    async def create_refund(self, payload, key):
        return await self.request("POST", "refunds", payload, key)

    async def get_refund(self, id):
        self.validate_id(id)
        return await self.request("GET", "refunds/" + id)

    @staticmethod
    def validate_id(id):
        if not isinstance(id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", id):
            raise ValueError("Некорректный идентификатор")
