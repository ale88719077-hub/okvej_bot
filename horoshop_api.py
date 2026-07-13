import time
import aiohttp


class HoroshopAPI:
    def __init__(self, domain, login, password):
        if not domain:
            domain = "okvej.com.ua"

        domain = domain.replace("https://", "").replace("http://", "").strip("/")

        self.base_url = f"https://{domain}/api"
        self.login = login
        self.password = password
        self.token = None
        self.token_until = 0

    async def _post(self, endpoint: str, payload: dict):
        url = f"{self.base_url}/{endpoint.strip('/')}/"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                return await response.json()

    async def get_token(self):
        if self.token and time.time() < self.token_until:
            return self.token

        if not self.login or not self.password:
            raise RuntimeError("HOROSHOP_LOGIN or HOROSHOP_PASSWORD is not set")

        data = await self._post("auth", {
            "login": self.login,
            "password": self.password,
        })

        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop auth error: {data}")

        self.token = data["response"]["token"]
        self.token_until = time.time() + 540
        return self.token

    async def get_products(self, limit=500, offset=0):
        token = await self.get_token()

        data = await self._post("catalog/export", {
            "token": token,
            "offset": offset,
            "limit": limit,
            "expr": {
                "display_in_showcase": 1
            },
            "includedParams": [
                "title",
                "price",
                "presence",
                "link",
                "images",
                "article",
                "quantity",
                "stock",
                "category"
            ]
        })

        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop catalog error: {data}")

        return data.get("response", {}).get("products", [])
