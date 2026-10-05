"""Тести клієнта Temu без мережі. Відповіді шлюзу взяті з живих запитів до EU 05.10.2026."""

import json

import httpx
import pytest

from temu_client import MAX_RETRIES, TemuClient, TemuError, sign

DOC_PARAMS = {
    "access_token": "2nifvmpyymvypwmcms5ct4uqqudrwgpmzbcnmkt1jzjkuaf3x56iixym",
    "app_key": "f9d5cc9313893a20d5aa85c654e8f503",
    "data_type": "JSON",
    "sendRequestList": [{"orderSendInfoList": [{"quantity": 1, "orderSn": "211-21905473070712792",
                                                "parentOrderSn": "PO-211-21905452099192792",
                                                "goodsId": 601099548666279, "skuId": 17592352673534}],
                         "carrierId": "699272611", "trackingNumber": "270324232756"}],
    "sendType": 0,
    "timestamp": 1711009072,
    "type": "bg.logistics.shipment.confirm",
}
DOC_SECRET = "c7e0a1a63542be4de3cb5488f9fba8149e8fc290"

FAKE_APP = {"success": False, "requestId": "eu-13b96df3", "errorCode": 4000000,
            "errorMsg": "The application information query is abnormal"}
RATE_LIMIT = {"success": False, "requestId": "eu-x", "errorCode": 4000004, "errorMsg": "RATE_LIMIT_EXCEED_EXCEPTION"}
OK = {"success": True, "requestId": "eu-y", "errorCode": 1000000, "result": {"totalItemNum": 0, "pageItems": []}}


def test_sign_matches_documented_example():
    assert sign(DOC_PARAMS, DOC_SECRET) == "4CCF219942D4180C6DDA3CE36C1B838F"


def test_sign_ignores_sign_field_and_key_order():
    shuffled = dict(reversed(list(DOC_PARAMS.items())))
    assert sign({**shuffled, "sign": "whatever"}, DOC_SECRET) == "4CCF219942D4180C6DDA3CE36C1B838F"


def test_sign_changes_with_secret_and_values():
    base = sign(DOC_PARAMS, DOC_SECRET)
    assert sign(DOC_PARAMS, DOC_SECRET[:-1] + "1") != base
    assert sign({**DOC_PARAMS, "sendType": 1}, DOC_SECRET) != base


def client(responses, sent=None):
    queue = list(responses)

    def handler(request):
        if sent is not None:
            sent.append(request.content.decode())
        return httpx.Response(200, json=queue.pop(0))
    return TemuClient("k" * 32, "s" * 40, "t" * 56, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleep=lambda s: None, clock=lambda: 1_790_000_000)


def test_sent_body_is_what_was_signed():
    sent = []
    client([OK], sent).call("bg.order.list.v2.get", pageNumber=1, pageSize=10)
    body = json.loads(sent[0])
    assert body["sign"] == sign(body, "s" * 40)
    assert body["type"] == "bg.order.list.v2.get" and body["data_type"] == "JSON" and body["pageSize"] == 10


def test_success_returns_result():
    assert client([OK]).call("bg.order.list.v2.get") == {"totalItemNum": 0, "pageItems": []}


def test_unknown_app_key_is_not_retried():
    sent = []
    with pytest.raises(TemuError) as e:
        client([FAKE_APP] * 5, sent).call("bg.order.list.v2.get")
    assert e.value.code == 4000000 and len(sent) == 1


def test_rate_limit_is_retried_then_succeeds():
    sent = []
    assert client([RATE_LIMIT, OK], sent).call("bg.order.list.v2.get")["totalItemNum"] == 0
    assert len(sent) == 2


def test_retries_are_bounded():
    sent = []
    with pytest.raises(TemuError):
        client([RATE_LIMIT] * 10, sent).call("bg.order.list.v2.get")
    assert len(sent) == MAX_RETRIES


@pytest.mark.parametrize("code,msg", [(3000001, "Sign invalid."), (3000003, "type not exists."),
                                      (3000034, "access_token is expired")])
def test_config_errors_raise_immediately(code, msg):
    sent = []
    with pytest.raises(TemuError) as e:
        client([{"success": False, "errorCode": code, "errorMsg": msg}] * 3, sent).call("x")
    assert e.value.code == code and len(sent) == 1
