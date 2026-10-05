"""Інтеграція Temu -> OpenCart -> SAP для магазину, де головний облік у SAP.

Один прохід `run_once()`:
1. `pull()`: замовлення Temu, змінені з минулого проходу (з перекриттям вікна),
   усі сторінки, ліміт сторінок. Кожен рядок Temu (orderSn) у журналі SQLite.
2. `summarize()`: усі ще не враховані продажі -> ОДНЕ замовлення OpenCart від
   технічного клієнта, однакові артикули сумуються, комплекти множаться на soldFactor.
   Ідемпотентно: спершу запис зведення з ключем у журнал, потім створення в OpenCart
   з ключем у коментарі; після збою прохід знаходить замовлення за ключем, а не створює друге.
3. `push_stock()`: залишок на Temu = залишок OpenCart (з SAP) мінус продажі Temu,
   яких SAP ще не зарезервував, мінус страховий запас; ділиться на soldFactor.

Стани рядка в журналі:
  waiting     Pending на Temu: кількість ще може змінитись, в OpenCart не йде, але
              залишок для Temu вже зменшує;
  pending     Unshipped і далі: готовий до зведення;
  summarized  потрапив у зведене замовлення;
  canceled    скасований до зведення: просто не рахується;
  review      скасований або зменшений ПІСЛЯ зведення: резерв у SAP уже стоїть, автоматично
              його не знімаємо (повернення не збільшують склад до перевірки товару),
              рішення за менеджером, рядок видно у звіті;
  unmapped    extCode не знайдено в OpenCart: в облік не йде, видно у звіті.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from shop import Shop
from temu_client import RETRYABLE as RETRYABLE_CODES, TemuClient, TemuError
from temu_mock import STATUS

TECH_CUSTOMER = "Temu — зведені продажі"
PAGE_SIZE = 100
MAX_PAGES = 50
OVERLAP_S = 600          # перекриття вікна опитування: запізнілі оновлення не губляться

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
    order_sn TEXT PRIMARY KEY, parent_sn TEXT NOT NULL, temu_sku INTEGER NOT NULL,
    ext_code TEXT NOT NULL, factor INTEGER NOT NULL, qty INTEGER NOT NULL,
    units INTEGER NOT NULL, state TEXT NOT NULL, summary_id INTEGER,
    summarized_units INTEGER, temu_updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, lines TEXT NOT NULL,
    oc_order_id INTEGER, state TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pushed (temu_sku INTEGER PRIMARY KEY, target INTEGER NOT NULL, sold_at_push INTEGER NOT NULL);
-- намір змінити залишок: пишеться ДО запиту, знімається після відповіді
CREATE TABLE IF NOT EXISTS push_intent (temu_sku INTEGER PRIMARY KEY, req_key TEXT NOT NULL, payload TEXT NOT NULL,
    target INTEGER NOT NULL, sold INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS log (ts INTEGER, level TEXT, msg TEXT);
"""


@dataclass
class TemuSkuRef:
    temu_sku: int
    goods_id: int
    ext_code: str
    factor: int


class Crash(Exception):
    """Для тестів: обрив процесу в найгіршому місці."""


class Sync:
    def __init__(self, client: TemuClient, shop: Shop, db: sqlite3.Connection, skus: list[TemuSkuRef],
                 clock, safety_units: int = 0, stock_mode: str = "diff", hold_unreserved: bool = True) -> None:
        self.client, self.shop, self.db, self.clock = client, shop, db, clock
        self.skus = {s.temu_sku: s for s in skus}
        self.safety = safety_units
        self.stock_mode = stock_mode      # diff: різниця від очікуваного на Temu; full: перезапис
        self.hold_unreserved = hold_unreserved  # False = наївно: залишок Temu = залишок OpenCart
        self.crash_after_journal = False  # для тестів
        db.executescript(SCHEMA)

    # --- журнал ----------------------------------------------------------------
    def log(self, level: str, msg: str) -> None:
        self.db.execute("INSERT INTO log VALUES (?,?,?)", (int(self.clock()), level, msg))

    def _kv(self, k: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def _set_kv(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # --- 1. замовлення ------------------------------------------------------------
    def pull(self) -> int:
        now = int(self.clock())
        cursor = int(self._kv("cursor", "0"))
        start = max(0, cursor - OVERLAP_S)
        seen, newest = 0, cursor
        for page in range(1, MAX_PAGES + 1):
            res = self.client.call("bg.order.list.v2.get", pageNumber=page, pageSize=PAGE_SIZE,
                                   updateAtStart=start, updateAtEnd=now)
            items = res.get("pageItems") or []
            for item in items:
                po = item["parentOrderMap"]
                newest = max(newest, po["updateTime"])
                for ln in item["orderList"]:
                    self._upsert(po, ln)
                    seen += 1
            if len(items) < PAGE_SIZE:
                break
        else:
            # Сторінок більше за ліміт: курсор ставимо на найновіше з прочитаного, а не на now,
            # щоб наступний прохід дочитав решту, а не перестрибнув її.
            self.log("error", f"order list exceeded {MAX_PAGES} pages, cursor kept at {newest}")
            self._set_kv("cursor", str(newest))
            self.db.commit()
            return seen
        self._set_kv("cursor", str(now))
        self.db.commit()
        return seen

    def _upsert(self, po: dict, ln: dict) -> None:
        prod = (ln.get("productList") or [{}])[0]
        ext, factor = prod.get("extCode", ""), int(prod.get("soldFactor") or 1)
        qty = int(ln["quantity"]) - int(ln.get("canceledQuantityBeforeShipment") or 0)
        units = max(0, qty) * factor
        status = int(ln.get("orderStatus", po["parentOrderStatus"]))
        row = self.db.execute("SELECT state, units, summarized_units FROM lines WHERE order_sn=?",
                              (ln["orderSn"],)).fetchone()
        old_state = row[0] if row else None

        if ext not in self.shop.products:
            state = "unmapped"
            if old_state != "unmapped":
                self.log("error", f"{ln['orderSn']}: extCode {ext!r} not in OpenCart")
        elif status == STATUS["canceled"] or units == 0:
            state = "review" if old_state in ("summarized", "review") else "canceled"
        elif status == STATUS["pending"]:
            state = old_state if old_state in ("summarized", "review") else "waiting"
        else:
            state = old_state if old_state in ("pending", "summarized", "review") else "pending"
        if old_state == "summarized" and state == "summarized" and row[2] is not None and units < row[2]:
            state = "review"     # часткове скасування після зведення
        if state == "review" and old_state != "review":
            self.log("warning", f"{ln['orderSn']}: changed after summary, needs manager decision")

        self.db.execute(
            """INSERT INTO lines (order_sn, parent_sn, temu_sku, ext_code, factor, qty, units, state, temu_updated)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_sn) DO UPDATE SET qty=excluded.qty, units=excluded.units,
                   state=excluded.state, temu_updated=excluded.temu_updated""",
            (ln["orderSn"], po["parentOrderSn"], ln["skuId"], ext, factor, max(0, qty), units, state,
             po["updateTime"]))

    # --- 2. зведене замовлення ------------------------------------------------------
    def summarize(self) -> int | None:
        self._resume_unfinished()
        rows = self.db.execute("SELECT order_sn, ext_code, units FROM lines WHERE state='pending' ORDER BY order_sn").fetchall()
        if not rows:
            return None
        key = "TEMU-SUM-" + hashlib.sha1(",".join(r[0] for r in rows).encode()).hexdigest()[:12]
        totals: dict[str, int] = defaultdict(int)
        for _, ext, units in rows:
            totals[ext] += units
        with self.db:
            cur = self.db.execute("INSERT INTO summaries (key, lines, state, created) VALUES (?,?,?,?)",
                                  (key, json.dumps(dict(sorted(totals.items()))), "creating", int(self.clock())))
            sid = cur.lastrowid
            self.db.executemany("UPDATE lines SET state='summarized', summary_id=?, summarized_units=units WHERE order_sn=?",
                                [(sid, r[0]) for r in rows])
        if self.crash_after_journal:
            raise Crash("after journal, before OpenCart")
        return self._create_in_opencart(sid)

    def _create_in_opencart(self, sid: int) -> int:
        key, lines = self.db.execute("SELECT key, lines FROM summaries WHERE id=?", (sid,)).fetchone()
        oc_id = self.shop.find_order_by_comment(key)
        if oc_id is None:
            oc_id = self.shop.create_order(TECH_CUSTOMER, json.loads(lines), comment=key)
        with self.db:
            self.db.execute("UPDATE summaries SET oc_order_id=?, state='created' WHERE id=?", (oc_id, sid))
        self.log("info", f"summary {key} -> OpenCart #{oc_id}")
        return oc_id

    def _resume_unfinished(self) -> None:
        for (sid,) in self.db.execute("SELECT id FROM summaries WHERE state='creating'").fetchall():
            self._create_in_opencart(sid)

    # --- 3. залишки на Temu -------------------------------------------------------------
    def held_units(self, ext: str) -> int:
        """Продажі Temu, яких ще немає в резерві SAP, а отже і в залишку OpenCart."""
        if not self.hold_unreserved:
            return 0
        held = 0
        # Для зведених рядків тримаємо те, що пішло у зведення: саме це зарезервує SAP,
        # навіть якщо покупець потім частково скасував (рядок у 'review').
        for state, units, oc_id in self.db.execute(
                """SELECT l.state, COALESCE(l.summarized_units, l.units), s.oc_order_id
                   FROM lines l LEFT JOIN summaries s ON s.id = l.summary_id
                   WHERE l.ext_code=? AND l.state IN ('waiting','pending','summarized','review')""", (ext,)):
            if state in ("waiting", "pending"):
                held += units
            elif oc_id is None or self.shop.order_status(oc_id) == "new":
                held += units           # зведення ще не дійшло до SAP
        return held

    def _sold_temu_units(self, temu_sku: int) -> int:
        """Скільки одиниць Temu (не наших) продано на цьому SKU за журналом, без скасувань."""
        return self.db.execute(
            "SELECT COALESCE(SUM(qty),0) FROM lines WHERE temu_sku=? AND state NOT IN ('canceled','unmapped')",
            (temu_sku,)).fetchone()[0]

    def targets(self) -> dict[int, int]:
        """Скільки показати на кожному SKU Temu.

        Якщо один наш артикул продається кількома SKU (поштучно і комплектом), кожен SKU
        не може показувати весь склад: покупці розберуть його двічі. Вільні одиниці
        діляться пропорційно продажам SKU за 7 днів (+1, щоб новий SKU не отримав 0),
        і сума показаного в наших одиницях ніколи не перевищує вільного.
        """
        groups: dict[str, list[TemuSkuRef]] = defaultdict(list)
        for ref in self.skus.values():
            groups[ref.ext_code].append(ref)
        since = int(self.clock()) - 7 * 86400
        out: dict[int, int] = {}
        for ext, refs in groups.items():
            if ext not in self.shop.products:
                # Один SKU з помилковим extCode не має зупиняти залишки решти товарів.
                self.log("error", f"extCode {ext!r} of Temu SKU {[r.temu_sku for r in refs]} not in OpenCart, stock not sent")
                continue
            free = max(0, self.shop.products[ext] - self.held_units(ext) - self.safety)
            if len(refs) == 1:
                out[refs[0].temu_sku] = free // refs[0].factor
                continue
            weights = {r.temu_sku: 1 + self.db.execute(
                "SELECT COALESCE(SUM(units),0) FROM lines WHERE temu_sku=? AND temu_updated>=? AND state!='canceled'",
                (r.temu_sku, since)).fetchone()[0] for r in refs}
            total_w = sum(weights.values())
            left = free
            for r in sorted(refs, key=lambda r: -r.factor):     # спершу комплекти, залишок поштучним
                share = free * weights[r.temu_sku] // total_w
                out[r.temu_sku] = min(share, left) // r.factor
                left -= out[r.temu_sku] * r.factor
            single = min(refs, key=lambda r: r.factor)
            out[single.temu_sku] += left // single.factor    # залишок від округлень
        return out

    def target(self, ref: TemuSkuRef) -> int:
        return self.targets().get(ref.temu_sku, 0)

    def _temu_stock(self) -> dict[int, int]:
        res = self.client.call("temu.local.goods.sku.stock.query", skuIdList=sorted(self.skus))
        return {i["skuId"]: i["selfOrdinaryStock"]["stock"]
                for g in res.get("stockList", []) for i in g["skuStockInfoList"]}

    def push_stock(self, actual: dict[int, int] | None = None) -> int:
        """`actual` = залишки Temu, прочитані ДО читання замовлень (див. cycle)."""
        changed = 0
        # 1) незавершені наміри з минулого проходу (обрив після запиту): той самий ключ,
        #    Temu не застосує його вдруге
        for ref in self.skus.values():
            pending = self.db.execute("SELECT req_key, payload, target, sold FROM push_intent WHERE temu_sku=?",
                                      (ref.temu_sku,)).fetchone()
            if pending:
                self._send(ref, pending[0], json.loads(pending[1]), pending[2], pending[3])
        # 2) фактичний залишок на Temu зараз; різниця рахується від нього, а не від нашого
        #    очікування: так розходження не накопичуються, а продаж між цим читанням і
        #    записом не перезаписується (Temu сам уже зменшив своє число)
        if actual is None:
            actual = self._temu_stock()
        targets = self.targets()
        for ref in self.skus.values():
            if self.db.execute("SELECT 1 FROM push_intent WHERE temu_sku=?", (ref.temu_sku,)).fetchone():
                continue                      # невизначений попередній запит: не накладаємо новий
            if ref.temu_sku not in targets:
                continue
            tgt = targets[ref.temu_sku]
            sold = self._sold_temu_units(ref.temu_sku)
            prev = self.db.execute("SELECT target, sold_at_push FROM pushed WHERE temu_sku=?", (ref.temu_sku,)).fetchone()
            now = actual.get(ref.temu_sku)
            if now is None:
                self.log("error", f"sku {ref.temu_sku} not returned by stock query")
                continue
            if prev is not None and prev[0] - (sold - prev[1]) != now:
                self.log("warning", f"sku {ref.temu_sku}: Temu {now}, expected {prev[0] - (sold - prev[1])}")
            if now == tgt:
                continue
            if self.stock_mode == "full":
                payload = {"skuStockTargetList": [{"skuId": ref.temu_sku, "stockTarget": tgt}]}
            else:
                payload = {"skuStockChangeList": [{"skuId": ref.temu_sku, "stockDiff": tgt - now}]}
            seq = int(self._kv("push_seq", "0")) + 1
            key = f"ps-{ref.temu_sku}-{seq}"          # унікальний на кожну зміну, сталий на повторах
            with self.db:
                self._set_kv("push_seq", str(seq))
                self.db.execute("INSERT OR REPLACE INTO push_intent VALUES (?,?,?,?,?)",
                                (ref.temu_sku, key, json.dumps(payload), tgt, sold))
            if self._send(ref, key, payload, tgt, sold):
                changed += 1
        return changed

    def _send(self, ref: TemuSkuRef, key: str, payload: dict, tgt: int, sold: int) -> bool:
        try:
            self.client.call("bg.local.goods.stock.edit", goodsId=ref.goods_id, requestUniqueKey=key, **payload)
        except TemuError as e:
            if e.code == 150013002 and "skuStockChangeList" in payload:
                # Різниця вивела б за межі 0..1e6: облік і Temu розійшлись. Перезапис повним значенням.
                self.log("warning", f"sku {ref.temu_sku}: diff out of range, full resync to {tgt}")
                with self.db:
                    self.db.execute("DELETE FROM push_intent WHERE temu_sku=?", (ref.temu_sku,))
                self.client.call("bg.local.goods.stock.edit", goodsId=ref.goods_id, requestUniqueKey=key + "-full",
                                 skuStockTargetList=[{"skuId": ref.temu_sku, "stockTarget": tgt}])
            elif e.code in RETRYABLE_CODES:
                # Невідомо, чи застосовано: намір лишається, наступний прохід повторить той самий ключ.
                self.log("error", f"stock edit {ref.temu_sku} uncertain: {e}")
                return False
            else:
                self.log("error", f"stock edit {ref.temu_sku} rejected: {e}")
                with self.db:
                    self.db.execute("DELETE FROM push_intent WHERE temu_sku=?", (ref.temu_sku,))
                return False
        with self.db:
            self.db.execute("INSERT INTO pushed VALUES (?,?,?) ON CONFLICT(temu_sku) DO UPDATE SET "
                            "target=excluded.target, sold_at_push=excluded.sold_at_push", (ref.temu_sku, tgt, sold))
            self.db.execute("DELETE FROM push_intent WHERE temu_sku=?", (ref.temu_sku,))
        return True

    # --- прохід і звіт --------------------------------------------------------------------
    def cycle(self, summarize: bool = True, after_pull=None) -> dict:
        """Порядок важливий: залишки Temu -> замовлення -> зведення -> різниця залишків.

        Продаж після читання замовлень: Temu вже зменшив число, різниця від прочитаного
        раніше залишку його не повертає. Продаж між двома читаннями: зменшено двічі,
        тобто тимчасово менше, ніж можна, а не більше; наступний прохід вирівнює.
        """
        try:
            actual = self._temu_stock()
            pulled = self.pull()
        except TemuError as e:
            self.log("error", f"read failed: {e}")
            self.db.commit()
            return {"ok": False, "error": str(e)}
        if after_pull:
            after_pull()
        oc = self.summarize() if summarize else None
        pushed = self.push_stock(actual)
        self.db.commit()
        return {"ok": True, "pulled": pulled, "summary_order": oc, "stock_updates": pushed}

    def run_once(self) -> dict:
        return self.cycle(summarize=True)

    def report(self) -> dict:
        by_state = dict(self.db.execute("SELECT state, COUNT(*) FROM lines GROUP BY state").fetchall())
        review = [dict(zip(("order_sn", "ext_code", "units", "summarized_units"), r)) for r in self.db.execute(
            "SELECT order_sn, ext_code, units, summarized_units FROM lines WHERE state IN ('review','unmapped')")]
        return {"lines_by_state": by_state, "needs_attention": review,
                "summaries": self.db.execute("SELECT COUNT(*) FROM summaries WHERE state='created'").fetchone()[0]}
