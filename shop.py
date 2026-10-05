"""Імітація OpenCart + SAP Business One для типової схеми «SAP головний, OpenCart між SAP і маркетплейсом».

* SAP головний: `on_hand` (фізично на складі) і `drafts` (резерви з чернеток).
  Доступно = on_hand - сума резервів.
* `exchange()` = обмін, що в такій схемі йде ~кожні 15 хв: нові замовлення OpenCart
  стають чернетками SAP (резерв), потім залишки OpenCart перезаписуються значенням
  «доступно» з SAP. Саме тому просто зменшити залишок OpenCart при продажі на Temu
  не можна: наступний обмін поверне старе число.
* `write_off()` = відвантаження: резерв стає списанням (on_hand і резерв
  зменшуються разом, «доступно» не змінюється, подвійного зменшення немає).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OcOrder:
    order_id: int
    customer: str
    lines: dict[str, int]          # артикул -> кількість
    comment: str = ""
    status: str = "new"            # new -> in_sap -> shipped


@dataclass
class Shop:
    on_hand: dict[str, int]
    drafts: dict[int, dict[str, int]] = field(default_factory=dict)
    products: dict[str, int] = field(default_factory=dict)      # oc_product.quantity
    orders: dict[int, OcOrder] = field(default_factory=dict)
    _next: int = 1000

    def __post_init__(self) -> None:
        if not self.products:
            self.products = dict(self.on_hand)

    # --- SAP -------------------------------------------------------------------
    def reserved(self, sku: str) -> int:
        return sum(d.get(sku, 0) for d in self.drafts.values())

    def available(self, sku: str) -> int:
        return self.on_hand[sku] - self.reserved(sku)

    def exchange(self) -> None:
        for o in self.orders.values():
            if o.status == "new":
                self.drafts[o.order_id] = dict(o.lines)
                o.status = "in_sap"
        for sku in self.on_hand:
            self.products[sku] = max(0, self.available(sku))

    def write_off(self, order_id: int) -> None:
        lines = self.drafts.pop(order_id)
        for sku, q in lines.items():
            self.on_hand[sku] -= q
        self.orders[order_id].status = "shipped"

    def restock(self, sku: str, qty: int) -> None:
        self.on_hand[sku] += qty

    # --- OpenCart ----------------------------------------------------------------
    def create_order(self, customer: str, lines: dict[str, int], comment: str = "") -> int:
        self._next += 1
        self.orders[self._next] = OcOrder(self._next, customer, dict(lines), comment)
        return self._next

    def find_order_by_comment(self, text: str) -> int | None:
        for o in self.orders.values():
            if text in o.comment:
                return o.order_id
        return None

    def order_status(self, order_id: int) -> str:
        return self.orders[order_id].status
