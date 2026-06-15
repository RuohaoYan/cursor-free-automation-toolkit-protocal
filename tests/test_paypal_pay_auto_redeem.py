import asyncio
from pathlib import Path

from modules import paypal_card_redeem, paypal_pay, utils


def test_run_paypal_pay_auto_redeems_when_card_pool_is_empty(monkeypatch, tmp_path: Path) -> None:
    cards_file = tmp_path / "cards.txt"
    codes_file = tmp_path / "card_codes.txt"
    used_file = tmp_path / "card_codes_used.txt"
    failed_file = tmp_path / "card_codes_failed.txt"
    phones_file = tmp_path / "phones.txt"
    links_file = tmp_path / "links.txt"

    codes_file.write_text("CODE-1\n", encoding="utf-8")
    cards_file.write_text("", encoding="utf-8")
    used_file.write_text("", encoding="utf-8")
    failed_file.write_text("", encoding="utf-8")
    phones_file.write_text("15555550123|https://sms.example.test/get\n", encoding="utf-8")
    links_file.write_text("user@example.com----query-code----https://pay.example.test/session\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PAYPAL_CARD_REDEEM_ENABLED=true",
                f"PAYPAL_CARDS_FILE={cards_file}",
                f"PAYPAL_CARD_CODES_FILE={codes_file}",
                f"PAYPAL_CARD_CODES_USED_FILE={used_file}",
                f"PAYPAL_CARD_CODES_FAILED_FILE={failed_file}",
                f"PAYPAL_PHONES_FILE={phones_file}",
                "PAYPAL_USE_PROXY=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_redeem(code, cfg):
        return (
            True,
            f"{code}----4111111111111111----2030/4----123----15555550123----JOHN DOE----1 Main St, New York NY 10001, US----https://sms.example.test/get",
        )

    async def fake_pay_one(*args, **kwargs):
        return False

    monkeypatch.setattr(utils, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paypal_pay, "LINK_POOL_FILE", links_file)
    monkeypatch.setattr(paypal_card_redeem, "redeem_code_once", fake_redeem)
    monkeypatch.setattr(paypal_pay, "pay_one", fake_pay_one)

    result = asyncio.run(paypal_pay.run_paypal_pay({}, count=1, workers=1))

    assert result == 0
    assert "4111111111111111" in cards_file.read_text(encoding="utf-8")
    assert used_file.read_text(encoding="utf-8").strip() == "CODE-1"
    assert codes_file.read_text(encoding="utf-8").strip() == ""


def test_redeem_code_once_uses_bom_stripped_proxy(monkeypatch, tmp_path: Path) -> None:
    proxy_file = tmp_path / "proxies_us.txt"
    proxy_file.write_text("\ufeffhttp://user:pass@proxy.example.test:3010\n", encoding="utf-8")
    cfg = paypal_card_redeem.RedeemConfig(
        enabled=True,
        api_url="https://card.example.test/api/exchange/verify",
        api_key="",
        timeout_sec=20,
        code_field="key",
        codes_file=tmp_path / "codes.txt",
        cards_file=tmp_path / "cards.txt",
        used_file=tmp_path / "used.txt",
        failed_file=tmp_path / "failed.txt",
        append_when_status_used=True,
        max_auto_fetch=20,
        retry_per_code=2,
        use_proxy=True,
        proxy_file=proxy_file,
        stop_on_request_error=True,
    )
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "content": {
                    "card_number": "4111111111111111",
                    "expiry_date": "2030/4",
                    "cvv": "123",
                }
            }

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(paypal_card_redeem.requests, "post", fake_post)

    ok, _ = paypal_card_redeem.redeem_code_once("CODE-1", cfg)

    assert ok
    assert captured["proxies"] == {
        "http": "http://user:pass@proxy.example.test:3010",
        "https": "http://user:pass@proxy.example.test:3010",
    }


def test_ensure_card_supply_stops_after_network_request_error(monkeypatch, tmp_path: Path) -> None:
    cards_file = tmp_path / "cards.txt"
    codes_file = tmp_path / "codes.txt"
    used_file = tmp_path / "used.txt"
    failed_file = tmp_path / "failed.txt"
    cards_file.write_text("", encoding="utf-8")
    codes_file.write_text("CODE-1\nCODE-2\n", encoding="utf-8")
    calls = []

    def fake_redeem(code, cfg):
        calls.append(code)
        return False, "request_error: tls failed"

    monkeypatch.setattr(paypal_card_redeem, "redeem_code_once", fake_redeem)

    paypal_card_redeem.ensure_card_supply(
        {
            "PAYPAL_CARD_REDEEM_ENABLED": "true",
            "PAYPAL_CARDS_FILE": str(cards_file),
            "PAYPAL_CARD_CODES_FILE": str(codes_file),
            "PAYPAL_CARD_CODES_USED_FILE": str(used_file),
            "PAYPAL_CARD_CODES_FAILED_FILE": str(failed_file),
            "PAYPAL_CARD_REDEEM_STOP_ON_REQUEST_ERROR": "true",
        },
        1,
    )

    assert calls == ["CODE-1"]
    assert codes_file.read_text(encoding="utf-8").splitlines() == ["CODE-1", "CODE-2"]


def test_run_paypal_pay_local_random_card_mode_works_without_card_pool(monkeypatch, tmp_path: Path) -> None:
    cards_file = tmp_path / "cards.txt"
    phones_file = tmp_path / "phones.txt"
    links_file = tmp_path / "links.txt"
    cards_file.write_text("", encoding="utf-8")
    phones_file.write_text("15555550123|https://sms.example.test/get\n", encoding="utf-8")
    links_file.write_text("user@example.com----query-code----https://pay.example.test/session\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PAYPAL_CARD_SOURCE=local_random",
                f"PAYPAL_CARDS_FILE={cards_file}",
                f"PAYPAL_PHONES_FILE={phones_file}",
                "PAYPAL_USE_PROXY=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    captured = {}

    async def fake_pay_one(item, card, *args, **kwargs):
        captured["email"] = item["email"]
        captured["card_number"] = card.number
        captured["zip"] = card.zip_code
        return False

    monkeypatch.setattr(utils, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paypal_pay, "LINK_POOL_FILE", links_file)
    monkeypatch.setattr(paypal_pay, "pay_one", fake_pay_one)

    result = asyncio.run(paypal_pay.run_paypal_pay({}, count=1, workers=1))

    assert result == 0
    assert captured["email"] == "user@example.com"
    assert len(captured["card_number"]) >= 13
    assert captured["card_number"][0] in {"2", "4", "5"}
    assert captured["zip"]


def test_run_paypal_pay_local_random_override_works_without_env_switch(monkeypatch, tmp_path: Path) -> None:
    cards_file = tmp_path / "cards.txt"
    phones_file = tmp_path / "phones.txt"
    links_file = tmp_path / "links.txt"
    cards_file.write_text("", encoding="utf-8")
    phones_file.write_text("15555550123|https://sms.example.test/get\n", encoding="utf-8")
    links_file.write_text("user2@example.com----query-code----https://pay.example.test/session\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"PAYPAL_CARDS_FILE={cards_file}",
                f"PAYPAL_PHONES_FILE={phones_file}",
                "PAYPAL_USE_PROXY=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    hit = {"ok": False}

    async def fake_pay_one(*args, **kwargs):
        hit["ok"] = True
        return False

    monkeypatch.setattr(utils, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paypal_pay, "LINK_POOL_FILE", links_file)
    monkeypatch.setattr(paypal_pay, "pay_one", fake_pay_one)

    result = asyncio.run(paypal_pay.run_paypal_pay({}, count=1, workers=1, card_source_mode="local_random"))

    assert result == 0
    assert hit["ok"]
