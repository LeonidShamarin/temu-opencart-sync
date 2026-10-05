"""Мінімальний клієнт Temu Open API: підпис запиту і виклик.

Формат за документацією партнерського порталу (partner.temu.com, розділ
"Signature Method for API Request", оновлено 2025-01-26):
* лише POST на `<host>/openapi/router`, тіло JSON;
* у тілі загальні параметри `type`, `app_key`, `access_token`, `timestamp`,
  `data_type` і параметри методу на тому ж рівні;
* `sign` = MD5(app_secret + k1v1k2v2... + app_secret).upper(), ключі за ASCII,
  значення-об'єкти серіалізуються JSON рівно так, як ідуть у тілі.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

HOSTS = {
    "us": "https://openapi-b-us.temu.com/openapi/router",
    "eu": "https://openapi-b-eu.temu.com/openapi/router",      # DE, IT, FR, ES, UK...
    "global": "https://openapi-b-global.temu.com/openapi/router",  # MX, JP...
}
MAX_RETRIES = 3
RETRYABLE = {4000000, 4000004}   # SYSTEM_EXCEPTION, RATE_LIMIT_EXCEED_EXCEPTION
# Перевірено на живому шлюзі EU 05.10.2026: невідомий app_key повертає не 3000026 з
# документації, а 4000000 з цим текстом. Це помилка налаштувань, повтор нічого не дасть.
NOT_RETRYABLE_MSGS = ("application information query is abnormal",)


def _value(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def sign(params: dict[str, Any], app_secret: str) -> str:
    """Підпис для всіх параметрів тіла, крім самого `sign`."""
    body = "".join(f"{k}{_value(v)}" for k, v in sorted(params.items()) if k != "sign")
    return hashlib.md5(f"{app_secret}{body}{app_secret}".encode("utf-8")).hexdigest().upper()


class TemuError(Exception):
    def __init__(self, code: int | None, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg


class TemuClient:
    def __init__(self, app_key: str, app_secret: str, access_token: str, region: str = "eu",
                 http: httpx.Client | None = None, sleep=time.sleep, clock=time.time) -> None:
        self.app_key, self.app_secret, self.access_token = app_key, app_secret, access_token
        self.url = HOSTS[region]
        self.http = http or httpx.Client(timeout=20.0)
        self.sleep, self.clock = sleep, clock

    def build(self, api_type: str, **params: Any) -> dict[str, Any]:
        body = {"type": api_type, "app_key": self.app_key, "access_token": self.access_token,
                "timestamp": int(self.clock()), "data_type": "JSON", **params}
        body["sign"] = sign(body, self.app_secret)
        return body

    def call(self, api_type: str, **params: Any) -> Any:
        """Виклик з повтором лише на системну помилку й перевищення ліміту, не більше MAX_RETRIES."""
        last: TemuError | None = None
        for attempt in range(MAX_RETRIES):
            body = self.build(api_type, **params)   # новий timestamp і sign на кожну спробу
            # Тіло має бути тим самим рядком, з якого рахувався підпис.
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            r = self.http.post(self.url, content=raw.encode("utf-8"),
                               headers={"Content-Type": "application/json"})
            data = r.json()
            if data.get("success"):
                return data.get("result")
            last = TemuError(data.get("errorCode"), data.get("errorMsg", ""))
            if last.code not in RETRYABLE or any(m in last.msg.lower() for m in NOT_RETRYABLE_MSGS):
                raise last
            self.sleep(min(2 ** attempt, 8))
        raise last  # type: ignore[misc]
