"""Сценарії інтеграції Temu -> OpenCart -> SAP на макеті Temu і імітації OpenCart+SAP."""

import sqlite3

import httpx
import pytest

from shop import Shop
from sync import TECH_CUSTOMER, Crash, Sync, TemuSkuRef
from temu_client import TemuClient, TemuError
from temu_mock import MockTemu, TemuSku


class World:
    def __init__(self, on_hand=None, skus=None):
        self.t = [1_790_000_000.0]
        self.clock = lambda: self.t[0]
        self.skus = skus or [TemuSku(9001, 601, "KT-01", 1), TemuSku(9002, 602, "KT-02", 1),
                             TemuSku(9102, 699, "KT-02", 2)]
        self.shop = Shop(on_hand=dict(on_hand or {"KT-01": 20, "KT-02": 20}))
        self.temu = MockTemu("app", "secret", "token", self.skus, self.clock)
        self.client = TemuClient("app", "secret", "token", http=httpx.Client(transport=self.temu.transport()),
                                 sleep=lambda s: None, clock=self.clock)
        self.db = sqlite3.connect(":memory:")
        self.refs = [TemuSkuRef(s.sku_id, s.goods_id, s.ext_code, s.sold_factor) for s in self.skus]
        self.sync = self.new_sync()
        self.sync.push_stock()             # початкові залишки на Temu

    def new_sync(self, **kw):
        return Sync(self.client, self.shop, self.db, self.refs, self.clock, **kw)

    def tick(self, s=60):
        self.t[0] += s

    def buy(self, sku, qty, status="unshipped"):
        po = self.temu.buy(sku, qty)
        assert po is not None
        self.tick()
        if status != "pending":
            self.temu.set_status(po.parent_sn, status)
        return po

    def tech_orders(self):
        return [o for o in self.shop.orders.values() if o.customer == TECH_CUSTOMER]


def test_three_orders_become_one_summary_line_of_five():
    w = World()
    for q in (2, 2, 1):
        w.buy(9001, q)
    w.tick()
    w.sync.cycle()
    orders = w.tech_orders()
    assert len(orders) == 1 and orders[0].lines == {"KT-01": 5}


def test_bundle_is_multiplied_by_sold_factor():
    w = World()
    w.buy(9102, 3)
    w.tick()
    w.sync.cycle()
    assert w.tech_orders()[0].lines == {"KT-02": 6}


def test_only_new_sales_go_into_the_next_summary():
    w = World()
    w.buy(9001, 1)
    w.tick()
    w.sync.cycle()
    w.buy(9001, 2)
    w.tick()
    w.sync.cycle()
    assert [o.lines for o in w.tech_orders()] == [{"KT-01": 1}, {"KT-01": 2}]


def test_overlapping_polls_do_not_double_count():
    w = World()
    w.buy(9001, 2)
    for _ in range(4):                 # вікна перекриваються на OVERLAP_S
        w.tick(120)
        w.sync.cycle()
    assert sum(o.lines["KT-01"] for o in w.tech_orders()) == 2


def test_pending_is_held_but_not_summarized_until_unshipped():
    w = World()
    po = w.buy(9001, 3, status="pending")
    w.tick()
    w.sync.cycle()
    assert w.tech_orders() == []
    assert w.sync.held_units("KT-01") == 3
    w.temu.change_pending_qty(po.parent_sn, 1)
    w.temu.set_status(po.parent_sn, "unshipped")
    w.tick()
    w.sync.cycle()
    assert w.tech_orders()[0].lines == {"KT-01": 1}


def test_cancel_before_summary_is_dropped():
    w = World()
    po = w.buy(9001, 2)
    w.temu.set_status(po.parent_sn, "canceled")
    w.tick()
    w.sync.cycle()
    assert w.tech_orders() == [] and w.sync.report()["lines_by_state"] == {"canceled": 1}


def test_cancel_after_summary_goes_to_review_and_reservation_stays():
    w = World()
    po = w.buy(9001, 2)
    w.tick()
    w.sync.cycle()
    w.shop.exchange()
    w.temu.set_status(po.parent_sn, "canceled")
    w.tick()
    w.sync.cycle()
    rep = w.sync.report()
    assert rep["lines_by_state"] == {"review": 1}
    assert rep["needs_attention"][0]["summarized_units"] == 2
    assert w.shop.reserved("KT-01") == 2         # знімає менеджер після перевірки, не код


def test_unknown_ext_code_is_quarantined():
    w = World(skus=[TemuSku(9001, 601, "KT-01", 1), TemuSku(9050, 650, "NO-SUCH", 1)])
    w.temu.skus[9050].stock = 5
    w.buy(9050, 1)
    w.tick()
    w.sync.cycle()
    assert w.sync.report()["lines_by_state"] == {"unmapped": 1} and w.tech_orders() == []


def test_crash_after_journal_resumes_without_duplicate():
    w = World()
    w.buy(9001, 2)
    w.tick()
    w.sync.crash_after_journal = True
    with pytest.raises(Crash):
        w.sync.cycle()
    assert w.tech_orders() == []
    w.sync = w.new_sync()                         # перезапуск процесу
    w.tick()
    w.sync.cycle()
    w.sync.cycle()
    assert [o.lines for o in w.tech_orders()] == [{"KT-01": 2}]


def test_crash_after_opencart_before_journal_update_finds_existing_order():
    w = World()
    w.buy(9001, 2)
    w.tick()
    w.sync.crash_after_journal = True
    with pytest.raises(Crash):
        w.sync.cycle()
    # OpenCart устиг створити замовлення, а журнал ні: ключ у коментарі рятує від дубля
    key, lines = w.db.execute("SELECT key, lines FROM summaries").fetchone()
    w.shop.create_order(TECH_CUSTOMER, {"KT-01": 2}, comment=key)
    w.sync = w.new_sync()
    w.tick()
    w.sync.cycle()
    assert len(w.tech_orders()) == 1


def test_stock_target_subtracts_sales_not_yet_reserved_in_sap():
    w = World(skus=[TemuSku(9001, 601, "KT-01", 1)])
    w.buy(9001, 3)
    w.tick()
    w.sync.cycle(summarize=False)
    assert w.temu.skus[9001].stock == 17          # OpenCart ще 20, але 3 вже продано на Temu
    w.sync.cycle()                                 # зведення створено, у SAP ще не дійшло
    assert w.temu.skus[9001].stock == 17
    w.shop.exchange()                              # SAP зарезервував і оновив OpenCart до 17
    w.tick()
    w.sync.cycle()
    assert w.shop.products["KT-01"] == 17 and w.temu.skus[9001].stock == 17


def test_sap_overwrite_does_not_return_sold_units():
    """Головний ризик схеми: наступна вигрузка з SAP перезапише залишок старим числом."""
    w = World(skus=[TemuSku(9001, 601, "KT-01", 1)], on_hand={"KT-01": 5})
    w.buy(9001, 2)
    w.tick()
    w.sync.cycle(summarize=False)
    w.shop.exchange()                              # SAP ще не знає про Temu: OpenCart знову 5
    w.tick()
    w.sync.cycle(summarize=False)
    assert w.temu.skus[9001].stock == 3


def test_sale_after_reading_orders_is_not_overwritten():
    w = World(skus=[TemuSku(9001, 601, "KT-01", 1)], on_hand={"KT-01": 5})
    w.tick()
    w.sync.cycle(summarize=False, after_pull=lambda: w.temu.buy(9001, 1))
    assert w.temu.skus[9001].stock == 4            # 5 - продаж, що стався між читанням і записом


def test_shared_stock_never_shows_more_than_free():
    w = World(on_hand={"KT-01": 20, "KT-02": 7})
    w.tick()
    w.sync.cycle()
    single, bundle = w.temu.skus[9002].stock, w.temu.skus[9102].stock
    assert single + 2 * bundle <= 7 and single > 0 and bundle > 0


def test_unfinished_push_is_replayed_with_same_key():
    w = World(skus=[TemuSku(9001, 601, "KT-01", 1)])
    before = w.temu.skus[9001].stock
    # запит дійшов до Temu, а процес упав до запису відповіді
    w.client.call("bg.local.goods.stock.edit", goodsId=601, requestUniqueKey="ps-9001-77",
                  skuStockChangeList=[{"skuId": 9001, "stockDiff": -3}])
    w.db.execute("INSERT INTO push_intent VALUES (9001, 'ps-9001-77', ?, ?, 0)",
                 ('{"skuStockChangeList": [{"skuId": 9001, "stockDiff": -3}]}', before - 3))
    w.db.commit()
    w.sync.push_stock()
    assert w.db.execute("SELECT COUNT(*) FROM push_intent").fetchone()[0] == 0
    assert w.temu.skus[9001].stock == before       # повтор не зняв ще 3, ціль повернула 20


def test_rate_limit_during_pull_is_retried():
    w = World()
    w.buy(9001, 1)
    w.temu.fail_next = [4000004, 4000004]
    w.tick()
    assert w.sync.cycle()["ok"]


def test_many_pages_are_read_fully():
    w = World(on_hand={"KT-01": 500, "KT-02": 20})
    w.sync.push_stock()
    for _ in range(250):
        w.buy(9001, 1)
    w.tick()
    w.sync.cycle()
    assert sum(o.lines["KT-01"] for o in w.tech_orders()) == 250


def test_mock_rejects_bad_sign_and_mixed_stock_modes():
    w = World()
    body = w.client.build("bg.order.list.v2.get")
    body["pageSize"] = 5                           # змінено після підпису
    r = w.temu.handle(httpx.Request("POST", "http://x", json=body))
    assert r.json()["errorCode"] == 3000001
    with pytest.raises(TemuError) as e:
        w.client.call("bg.local.goods.stock.edit", goodsId=601,
                      skuStockTargetList=[{"skuId": 9001, "stockTarget": 1}],
                      skuStockChangeList=[{"skuId": 9001, "stockDiff": 1}])
    assert e.value.code == 150013003


def test_short_simulation_keeps_accounting_exact_and_beats_naive():
    import simulate
    simulate.DAYS = 3
    ours = simulate.run(seed=7, mode="diff")
    naive = simulate.run(seed=7, mode="full", hold=False)
    assert ours["accounting_mismatch_units"] == 0
    assert ours["oversold_units"] < naive["oversold_units"]
