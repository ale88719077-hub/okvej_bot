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
                "category",
                "description",
                "short_description",
                "weight",
                "packing",
                "unit"
            ]
        })

        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop catalog error: {data}")

        return data.get("response", {}).get("products", [])
    async def get_orders(self, *, date_from=None, date_to=None, statuses=None, ids=None, limit=100, offset=0, additional_data=True):
        token = await self.get_token()
        payload = {
            "token": token,
            "offset": int(offset),
            "limit": int(limit),
            "additionalData": bool(additional_data),
        }
        if date_from:
            payload["from"] = date_from
        if date_to:
            payload["to"] = date_to
        if statuses is not None:
            payload["status"] = statuses
        if ids:
            payload["ids"] = [int(x) for x in ids]

        data = await self._post("orders/get", payload)
        if data.get("status") == "EMPTY":
            return []
        if data.get("status") != "OK":
            raise RuntimeError(f"Horoshop orders error: {data}")
        return data.get("response", {}).get("orders", [])

    async def update_orders(self, orders):
        token = await self.get_token()
        data = await self._post("orders/update", {
            "token": token,
            "orders": orders,
        })
        if data.get("status") not in ("OK", "WARNING"):
            raise RuntimeError(f"Horoshop order update error: {data}")
        return data

    async def update_order(self, order_id, *, status=None, payed=None, tracking_code=None):
        item = {"order_id": int(order_id)}
        if status is not None:
            item["status"] = int(status)
        if payed is not None:
            item["payed"] = 1 if bool(payed) else 0
        if tracking_code:
            item["tracking_code"] = str(tracking_code)
        return await self.update_orders([item])

