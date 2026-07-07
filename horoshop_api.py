import os
import time
import aiohttp


class HoroshopAPI:
    def __init__(self, domain, login, password):
        domain = domain.replace("https://", "").replace("http://", "").strip("/")

        self.base_url = f"https://{domain}/api"
        self.login = login
        self.password = password

        self.token = None
        self.token_until = 0
       

    async def _post(self, endpoint: str, payload: dict):
        url = f"{self.base_url}/{endpoint.strip('/')}/"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}) as response:
                return await response.json()

    async def get_token(self):
        if self.token and time.time() < self.token_until:
            return self.token
        print("DOMAIN:", self.base_url)
        print("LOGIN:", repr(self.login))
        print("PASSWORD:", repr(self.password))

        data = await self._post("auth", {
            "login": self.login,
            "password": self.password,
        })

        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop auth error: {data}")

        self.token = data["response"]["token"]
        self.token_until = time.time() + 540
        return self.token

    async def get_products(self, limit=5, offset=0):
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
            "article"
        ]
    })

    if data.get("status") != "OK":
        raise RuntimeError(f"Horoshop catalog error: {data}")

    products = data.get("response", {}).get("products", [])

    print("========== FIRST PRODUCT ==========")
    if products:
        print(products[0])
    else:
        print("NO PRODUCTS")
    print("===================================")

    return products
