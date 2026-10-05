"""30 днів магазину з кроком 15 хв: чи схема тримає залишки й облік.

Міряє:
* oversold    одиниць, проданих на Temu понад реальну наявність (SAP мінус продажі
              Temu, яких SAP ще не бачить); саме від цього захищає інтеграція;
* accounted   чи кожна неcкасована продана одиниця Temu потрапила в OpenCart рівно раз;
* idle        середня частка «закритого» товару: на Temu показано менше, ніж можна було.

Запуск: python simulate.py  (детерміновано, ~секунди)
"""

from __future__ import annotations

import random
import sqlite3
import sys
from collections import defaultdict

from shop import Shop
from sync import TECH_CUSTOMER, Crash, Sync, TemuSkuRef
from temu_client import TemuClient
import httpx
from temu_mock import STATUS, MockTemu, TemuSku

TICK = 15 * 60
DAYS = 30
SUMMARY_EVERY = 32        # 8 годин: 3 зведення на день
SHIP_AFTER = 8            # відвантаження через 2 години після резерву


def catalogue():
    # 12 артикулів; KT-03 продається і поштучно, і комплектом по 2 (спільний склад)
    on_hand = {f"KT-{i:02d}": 6 + 3 * (i % 5) for i in range(1, 13)}
    skus = [TemuSku(9000 + i, 600 + i, f"KT-{i:02d}", 1) for i in range(1, 13)]
    skus.append(TemuSku(9100, 699, "KT-03", 2))
    return on_hand, skus


def run(seed: int = 1, mode: str = "diff", safety: int = 0, hold: bool = True, crash_rate: float = 0.05,
        rate_limit_rate: float = 0.03, race: bool = True) -> dict:
    rng = random.Random(seed)
    t = [1_790_000_000.0]
    clock = lambda: t[0]  # noqa: E731
    on_hand, skus = catalogue()
    shop = Shop(on_hand=dict(on_hand))
    temu = MockTemu("app", "secret", "token", skus, clock, seed)
    client = TemuClient("app", "secret", "token", http=httpx.Client(transport=temu.transport()),
                        sleep=lambda s: None, clock=clock)
    db = sqlite3.connect(":memory:")
    refs = [TemuSkuRef(s.sku_id, s.goods_id, s.ext_code, s.sold_factor) for s in skus]
    make = lambda: Sync(client, shop, db, refs, clock, safety_units=safety, stock_mode=mode, hold_unreserved=hold)  # noqa: E731
    sync = make()
    sync.run_once()

    def temu_units(ext):          # чинні продажі Temu (без скасованого) у наших одиницях
        total = 0
        for po in temu.orders.values():
            for ln in po.lines:
                s = temu.skus[ln.sku_id]
                if s.ext_code == ext:
                    total += (ln.quantity - ln.canceled) * s.sold_factor
        return total

    def need_not_in_sap() -> dict[str, int]:
        """Скільки проданого на Temu ще не стоїть у резерві SAP, по кожному рядку окремо.

        Рядок «в SAP», якщо зведення з ним уже дійшло до SAP. Сумою по артикулу рахувати
        не можна: резерв під скасований після зведення рядок не покриває новий продаж,
        ним розпоряджається менеджер після перевірки товару.
        """
        oc_of = dict(db.execute("SELECT l.order_sn, s.oc_order_id FROM lines l JOIN summaries s ON s.id=l.summary_id"))
        need: dict[str, int] = defaultdict(int)
        for po in temu.orders.values():
            for ln in po.lines:
                s = temu.skus[ln.sku_id]
                units = (ln.quantity - ln.canceled) * s.sold_factor
                oc = oc_of.get(ln.order_sn)
                if units and not (oc is not None and shop.order_status(oc) in ("in_sap", "shipped")):
                    need[s.ext_code] += units
        return need

    def real_free(ext):
        return shop.available(ext) - need_not_in_sap()[ext]

    oversold = 0
    shared: dict[str, int] = defaultdict(int)
    for r_ in refs:
        shared[r_.ext_code] += 1
    over_by_ext: dict[str, int] = defaultdict(int)
    crashes = 0
    idle_sum = idle_n = 0
    states: dict[str, int] = {}
    reserved_at: dict[int, int] = {}

    def buy_some(n):
        nonlocal oversold
        for _ in range(n):
            sku = rng.choice(skus)
            qty = rng.choice([1, 1, 1, 2])
            before = real_free(sku.ext_code)
            if temu.buy(sku.sku_id, qty):
                after = real_free(sku.ext_code)
                oversold += max(0, -after) - max(0, -before)
                over_by_ext[sku.ext_code] += max(0, -after) - max(0, -before)

    for tick in range(DAYS * 96):
        t[0] += TICK
        # життя замовлень на Temu
        for po in list(temu.orders.values()):
            if po.status == STATUS["pending"] and t[0] - po.created >= TICK:
                r = rng.random()
                if r < 0.04:
                    temu.set_status(po.parent_sn, "canceled")
                elif r < 0.08 and po.lines[0].quantity > 1:
                    temu.change_pending_qty(po.parent_sn, 1)
                else:
                    temu.set_status(po.parent_sn, "unshipped")
            elif po.status == STATUS["unshipped"] and rng.random() < 0.001:
                temu.set_status(po.parent_sn, "canceled")      # скасування вже після обліку
            elif po.status == STATUS["unshipped"] and t[0] - po.created > 8 * 3600:
                temu.set_status(po.parent_sn, "shipped")
        buy_some(rng.choice([0, 0, 1, 1, 2]))
        # оптові замовлення на сайті, якщо OpenCart показує залишок
        if rng.random() < 0.15:
            ext = rng.choice(list(on_hand))
            if shop.products[ext] >= 2:
                shop.create_order("wholesale", {ext: 2})
                shop.products[ext] -= 2
        # SAP: обмін кожні 15 хв, відвантаження, поставки
        shop.exchange()
        for oid, o in list(shop.orders.items()):
            if o.status == "in_sap":
                reserved_at.setdefault(oid, tick)
                if tick - reserved_at[oid] >= SHIP_AFTER:
                    shop.write_off(oid)
        if tick % 96 == 0:
            for ext in on_hand:
                if shop.available(ext) < 6:
                    shop.restock(ext, 10)
        # інтеграція: замовлення й залишки щопроходу, зведення 3 рази на день
        if rng.random() < rate_limit_rate:
            temu.fail_next.append(4000004)
        try:
            sync.crash_after_journal = tick % SUMMARY_EVERY == 0 and rng.random() < crash_rate
            r = sync.cycle(summarize=tick % SUMMARY_EVERY == 0,
                           after_pull=(lambda: buy_some(rng.choice([0, 1]))) if race else None)
        except Crash:
            crashes += 1
            sync = make()                     # «перезапуск процесу» з тим самим журналом
        need = need_not_in_sap()
        for ref in refs:
            if shared[ref.ext_code] > 1:
                continue              # спільний склад кількох SKU: «ідеал» для одного SKU не визначений
            free = shop.available(ref.ext_code) - need[ref.ext_code]
            ideal = max(0, (free - safety) // ref.factor)
            if ideal:
                idle_sum += max(0, ideal - temu.skus[ref.temu_sku].stock) / ideal
                idle_n += 1

    # фінал: Pending стають Unshipped, усе дочитати й звести
    t[0] += TICK
    for po in list(temu.orders.values()):
        if po.status == STATUS["pending"]:
            temu.set_status(po.parent_sn, "unshipped")
    sync.pull()
    sync.summarize()
    shop.exchange()
    sync.push_stock()
    states = sync.report()["lines_by_state"]

    # облік: кожна чинна одиниця Temu в OpenCart рівно раз (рядки 'review' мають надлишок,
    # це свідомо: резерв знімає менеджер)
    mismatch = 0
    for ext in on_hand:
        in_oc = sum(o.lines.get(ext, 0) for o in shop.orders.values() if o.customer == TECH_CUSTOMER)
        review_extra = sum(r[0] for r in db.execute(
            "SELECT summarized_units - units FROM lines WHERE ext_code=? AND state='review'", (ext,)))
        mismatch += abs(in_oc - review_extra - temu_units(ext))
    sold = sum((ln.quantity - ln.canceled) * temu.skus[ln.sku_id].sold_factor
               for po in temu.orders.values() for ln in po.lines)
    return {"mode": mode if hold else "naive", "safety": safety, "temu_units_sold": sold, "oversold_units": oversold,
            "accounting_mismatch_units": mismatch, "summary_orders": sum(1 for o in shop.orders.values() if o.customer == TECH_CUSTOMER),
            "crashes_recovered": crashes, "idle_pct": round(idle_sum / max(1, idle_n) * 100, 1),
            "review": states.get("review", 0), "api_calls": len(temu.calls),
            "oversold_by_sku": {k: v for k, v in sorted(over_by_ext.items()) if v}}


if __name__ == "__main__":
    rows = []
    for name, kw in [("naive", dict(hold=False, mode="full")), ("full", dict(mode="full")),
                     ("diff", dict(mode="diff")), ("diff+safety1", dict(mode="diff", safety=1))]:
        agg = defaultdict(int)
        for seed in range(1, 6):
            r = run(seed=seed, **kw)
            for k in ("temu_units_sold", "oversold_units", "accounting_mismatch_units", "summary_orders",
                      "crashes_recovered", "review", "api_calls"):
                agg[k] += r[k]
            agg["idle_pct_x5"] += r["idle_pct"]
        rows.append((name, agg))
    print("config        sold  oversold  mismatch  summaries  crashes  review  idle%  api_calls  (5 seeds x 30 days)")
    for name, a in rows:
        print(f"{name:12} {a['temu_units_sold']:5} {a['oversold_units']:9} {a['accounting_mismatch_units']:9} "
              f"{a['summary_orders']:10} {a['crashes_recovered']:8} {a['review']:7} {a["idle_pct_x5"] / 5:6.1f} {a['api_calls']:10}")
    sys.exit(0)
