"""Макет Temu Open API для продавця: замовлення, залишки, покупці, збої.

Відтворює те, що відомо з документації і з живого шлюзу EU (див. README):
* POST JSON на router, перевірка `sign` (3000001), `timestamp` (±300 с),
  невідомий `type` (3000003), відповідь завжди HTTP 200 з `success/errorCode`;
* `bg.order.list.v2.get`: фільтр `updateAtStart/updateAtEnd` (секунди),
  `pageNumber/pageSize`, структура parentOrderMap + orderList з `productList`
  (`extCode` = артикул продавця, `soldFactor` = одиниць у комплекті),
  `canceledQuantityBeforeShipment`;
* `bg.local.goods.stock.edit`: або повне значення (`skuStockTargetList`),
  або різниця (`skuStockChangeList`), не обидва (150013003), без дублів SKU
  (150013001), 0..1 000 000 (150013002), `requestUniqueKey` для ідемпотентності;
* `temu.local.goods.sku.stock.query`.

Числові коди статусів у документації не вказані, тут вони умовні (STATUS).
Покупці купують, лише поки залишок на Temu > 0; продаж понад реальний
залишок складу рахується як oversell (це й є те, від чого захищає інтеграція).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

import httpx

from temu_client import sign

STATUS = {"pending": 1, "unshipped": 2, "shipped": 3, "delivered": 4, "canceled": 5}
TS_WINDOW = 300
MAX_PAGE = 100


@dataclass
class TemuSku:
    sku_id: int
    goods_id: int
    ext_code: str        # наш артикул
    sold_factor: int     # скільки наших одиниць в одній одиниці Temu (комплект)
    stock: int = 0       # залишок, який бачить покупець на Temu


@dataclass
class Line:
    order_sn: str
    sku_id: int
    quantity: int
    canceled: int = 0
    status: int = STATUS["pending"]


@dataclass
class ParentOrder:
    parent_sn: str
    created: int
    updated: int
    status: int
    lines: list[Line] = field(default_factory=list)


class MockTemu:
    def __init__(self, app_key: str, app_secret: str, access_token: str, skus: list[TemuSku],
                 clock, seed: int = 1) -> None:
        self.app_key, self.app_secret, self.access_token = app_key, app_secret, access_token
        self.skus = {s.sku_id: s for s in skus}
        self.orders: dict[str, ParentOrder] = {}
        self.clock = clock
        self.rng = random.Random(seed)
        self.seen_keys: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_next: list[int] = []      # коди помилок, які віддати наступними запитами
        self._n = 0

    # --- поведінка покупців і Temu ------------------------------------------------

    def buy(self, sku_id: int, qty: int) -> ParentOrder | None:
        """Покупець купує, якщо на Temu є залишок. Temu сам зменшує свій залишок."""
        sku = self.skus[sku_id]
        if sku.stock < qty:
            return None
        sku.stock -= qty
        self._n += 1
        now = int(self.clock())
        po = ParentOrder(f"PO-186-{self._n:011d}", now, now, STATUS["pending"],
                         [Line(f"186-{self._n:011d}-1", sku_id, qty)])
        self.orders[po.parent_sn] = po
        return po

    def set_status(self, parent_sn: str, status: str) -> None:
        po = self.orders[parent_sn]
        po.status = STATUS[status]
        for ln in po.lines:
            ln.status = po.status
            if status == "canceled":
                ln.canceled = ln.quantity
                self.skus[ln.sku_id].stock += ln.quantity   # Temu повертає товар у продаж
        po.updated = int(self.clock())

    def change_pending_qty(self, parent_sn: str, new_qty: int) -> None:
        """У Pending покупець може змінити кількість (з документації Temu)."""
        po = self.orders[parent_sn]
        assert po.status == STATUS["pending"]
        ln = po.lines[0]
        self.skus[ln.sku_id].stock += ln.quantity - new_qty
        ln.quantity = new_qty
        po.updated = int(self.clock())

    # --- HTTP -----------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _err(self, code: int, msg: str) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "requestId": "eu-mock", "errorCode": code, "errorMsg": msg})

    def _ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "requestId": "eu-mock", "errorCode": 1000000,
                                         "errorMsg": "", "result": result})

    def handle(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        body = json.loads(raw or "{}")
        self.calls.append(body.get("type", ""))
        if "type" not in body:
            return self._err(3000002, "there is no type in body.")
        if body.get("app_key") != self.app_key:
            return self._err(4000000, "The application information query is abnormal")
        if "sign" not in body:
            return self._err(3000040, "there is no sign in body.")
        if body["sign"] != sign(body, self.app_secret):
            return self._err(3000001, "Sign invalid.")
        if abs(int(body.get("timestamp", 0)) - int(self.clock())) > TS_WINDOW:
            return self._err(3000012, "timestamp is expired.")
        if body.get("access_token") != self.access_token:
            return self._err(3000034, "access_token is expired or have been refreshed")
        if self.fail_next:
            code = self.fail_next.pop(0)
            return self._err(code, {4000004: "RATE_LIMIT_EXCEED_EXCEPTION"}.get(code, "SYSTEM_EXCEPTION"))
        route = {"bg.order.list.v2.get": self._order_list, "bg.local.goods.stock.edit": self._stock_edit,
                 "temu.local.goods.sku.stock.query": self._stock_query}.get(body["type"])
        if route is None:
            return self._err(3000003, "type not exists.")
        return route(body)

    def _order_list(self, b: dict) -> httpx.Response:
        start, end = b.get("updateAtStart"), b.get("updateAtEnd")
        if start is not None and end is not None and end < start:
            return self._err(140020012, "updateAtEnd needs to be greater than the updateAtStart")
        size = max(1, min(int(b.get("pageSize", 10)), MAX_PAGE))
        page = max(1, int(b.get("pageNumber", 1)))
        rows = sorted((po for po in self.orders.values()
                       if (start is None or po.updated >= start) and (end is None or po.updated <= end)),
                      key=lambda po: (po.updated, po.parent_sn))
        items = []
        for po in rows[(page - 1) * size: page * size]:
            items.append({
                "parentOrderMap": {"parentOrderSn": po.parent_sn, "parentOrderStatus": po.status,
                                   "parentOrderTime": po.created, "updateTime": po.updated, "regionId": 186},
                "orderList": [{
                    "orderSn": ln.order_sn, "skuId": ln.sku_id, "goodsId": self.skus[ln.sku_id].goods_id,
                    "quantity": ln.quantity, "originalOrderQuantity": ln.quantity,
                    "canceledQuantityBeforeShipment": ln.canceled, "orderStatus": ln.status,
                    "productList": [{"productSkuId": ln.sku_id, "extCode": self.skus[ln.sku_id].ext_code,
                                     "soldFactor": self.skus[ln.sku_id].sold_factor, "productId": 1}],
                } for ln in po.lines],
            })
        return self._ok({"totalItemNum": len(rows), "pageItems": items})

    def _stock_edit(self, b: dict) -> httpx.Response:
        target, change = b.get("skuStockTargetList"), b.get("skuStockChangeList")
        if target and change:
            return self._err(150013003, "Only one stock adjustment method can be active at a time.")
        rows = target or change or []
        ids = [r["skuId"] for r in rows]
        if len(ids) != len(set(ids)):
            return self._err(150013001, "Duplicate SKUs detected. Please remove them and try again")
        key = b.get("requestUniqueKey")
        if key and key in self.seen_keys:
            return self._ok(self.seen_keys[key])      # повтор того самого запиту нічого не змінює
        for r in rows:
            sku = self.skus.get(r["skuId"])
            if sku is None:
                return self._err(150010003, "Invalid Request Parameters")
            new = r["stockTarget"] if target else sku.stock + r["stockDiff"]
            if not 0 <= new <= 1_000_000:
                return self._err(150013002, "Quantity must be between 0 and 1000000")
        for r in rows:
            sku = self.skus[r["skuId"]]
            sku.stock = r["stockTarget"] if target else sku.stock + r["stockDiff"]
        result = {"goodsId": b.get("goodsId"), "operateResult": True,
                  "skuStockEditStatusInfoList": [{"skuId": i, "stockEditStatus": True} for i in ids]}
        if key:
            self.seen_keys[key] = result
        return self._ok(result)

    def _stock_query(self, b: dict) -> httpx.Response:
        ids = b.get("skuIdList") or list(self.skus)
        return self._ok({"stockList": [{"goodsId": self.skus[i].goods_id, "skuStockInfoList": [
            {"skuId": i, "outSkuSn": self.skus[i].ext_code,
             "selfOrdinaryStock": {"stock": self.skus[i].stock, "stockType": 1}}]} for i in ids if i in self.skus]})
