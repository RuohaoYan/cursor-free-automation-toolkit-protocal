from __future__ import annotations

import re
import time
from dataclasses import replace
from typing import Any

import requests

from modules.hero_sms_provider import (
    OperatorQuote,
    PhoneCountry,
    SmsActivation,
    extract_country_price,
    parse_countries_response,
    parse_integer,
    parse_number,
)
from modules.terminal_theme import install_print_theme


install_print_theme()


class GrizzlySMSProvider:
    def __init__(self, api_key: str, *, base_url: str = "https://api.grizzlysms.com/stubs/handler_api.php", timeout: int = 30) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    def request(self, action: str, **params: Any) -> Any:
        response = requests.get(
            self.base_url,
            params={"api_key": self.api_key, "action": action, **params},
            headers={"User-Agent": "LoucerLongLink/1.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        text = response.text.strip()
        try:
            return response.json()
        except ValueError:
            return text

    def get_countries(self) -> list[dict[str, Any]]:
        try:
            return parse_countries_response(self.request("getCountries"))
        except Exception:
            return []

    def get_services(self) -> dict[str, str]:
        data = self.request("getServicesList")
        result: dict[str, str] = {}
        if isinstance(data, dict):
            candidates = data.get("services") or data.get("data") or data.get("result") or data
            if isinstance(candidates, dict):
                for key, value in candidates.items():
                    code = str(key).strip()
                    if isinstance(value, dict):
                        name = str(value.get("name") or value.get("title") or value.get("label") or value.get("service") or code).strip()
                    else:
                        name = str(value).strip()
                    if code:
                        result[code] = name or code
            elif isinstance(candidates, list):
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code") or item.get("id") or item.get("service") or "").strip()
                    name = str(item.get("name") or item.get("title") or item.get("label") or code).strip()
                    if code:
                        result[code] = name or code
        return result

    def resolve_openai_service(self, configured: str = "") -> str:
        configured = str(configured or "").strip()
        if configured and configured.lower() != "auto":
            return configured
        try:
            services = self.get_services()
        except Exception:
            return "dr"
        for code, name in services.items():
            haystack = f"{code} {name}".lower()
            if "openai" in haystack or "chatgpt" in haystack or "chat gpt" in haystack:
                print(f"[SMS] GrizzlySMS 自动识别服务: {name} ({code})", flush=True)
                return code
        return "dr"

    def get_price_matrix(self, service: str = "dr") -> Any:
        last_error: Exception | None = None
        for action in ("getPricesV3", "getPricesV2", "getPrices"):
            try:
                return self.request(action, service=service)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("未能获取 GrizzlySMS 价格列表")

    def list_country_prices(self, service: str, countries: list[PhoneCountry]) -> list[PhoneCountry]:
        matrix = self.get_price_matrix(service)
        priced: list[PhoneCountry] = []
        known_ids = {country.hero_sms_country for country in countries if country.hero_sms_country > 0}
        by_id = {country.hero_sms_country: country for country in countries if country.hero_sms_country > 0}
        for country in countries:
            if country.hero_sms_country <= 0:
                continue
            parsed = extract_country_price(matrix, country.hero_sms_country, service)
            if not parsed or parsed.get("price") is None:
                continue
            priced.append(replace(country, price=parsed.get("price"), count=parsed.get("count")))

        for row in extract_price_rows(matrix, service):
            country_id = parse_integer(row.get("country") or row.get("countryId") or row.get("country_id") or row.get("id"))
            price = parse_number(row.get("price") or row.get("cost") or row.get("activationCost") or row.get("amount"))
            count = parse_integer(row.get("count") or row.get("qty") or row.get("available") or row.get("stock") or row.get("total"))
            if country_id is None or country_id in known_ids or price is None:
                continue
            base = by_id.get(country_id) or PhoneCountry(
                iso_code=str(row.get("isoCode") or row.get("iso") or "").strip().upper(),
                dial_code=str(row.get("dialCode") or row.get("phoneCode") or row.get("prefix") or "").strip().lstrip("+"),
                name=str(row.get("name") or row.get("countryName") or row.get("country") or country_id).strip(),
                hero_sms_country=country_id,
            )
            priced.append(replace(base, price=price, count=count))
            known_ids.add(country_id)

        return sorted(priced, key=lambda row: ((row.price if row.price is not None else 999999), -(row.count or 0)))

    def get_operator_quote_options(self, service: str, country: int) -> list[OperatorQuote]:
        rows: list[OperatorQuote] = []
        for action in ("getPricesV3", "getPricesV2"):
            try:
                data = self.request(action, service=service, country=country)
            except Exception:
                continue
            rows.extend(extract_provider_quotes(data, service, country))
            if rows:
                break
        return rows

    def get_number(self, service: str = "dr", country: int | str = "any", *, operator: str = "", max_retries: int = 5) -> SmsActivation:
        for attempt in range(1, max_retries + 1):
            params: dict[str, Any] = {"service": service}
            if str(country).strip():
                params["country"] = country
            if operator:
                params["providerIds"] = operator
            operator_text = f", providerIds={operator}" if operator else ", providerIds=自动"
            print(f"[SMS] 请求 GrizzlySMS 号码: service={service}, country={country}{operator_text} ({attempt}/{max_retries})", flush=True)
            try:
                data = self.request("getNumberV2", **params)
                if parse_error_text(data) in {"BAD_ACTION", "NO_ACTION", "WRONG_ACTION", "ERROR_SQL"}:
                    data = self.request("getNumber", **params)
            except Exception:
                data = self.request("getNumber", **params)

            activation = parse_activation_response(data)
            if activation:
                phone = activation.phone_number if activation.phone_number.startswith("+") else f"+{activation.phone_number}"
                print(
                    f"[SMS] 获取号码成功: {phone} (activation={activation.activation_id}, 费用=${activation.activation_cost if activation.activation_cost is not None else '-'})",
                    flush=True,
                )
                return SmsActivation(activation.activation_id, phone, activation.activation_cost)

            error = parse_error_text(data)
            if error in {"NO_NUMBERS", "SERVICE_UNAVAILABLE_REGION"} and attempt < max_retries:
                print(f"[SMS] {error}，3秒后重试... ({attempt}/{max_retries})", flush=True)
                time.sleep(3)
                continue
            if error == "NO_BALANCE":
                raise RuntimeError("GrizzlySMS 余额不足")
            if error == "BAD_KEY":
                raise RuntimeError("GrizzlySMS API Key 无效")
            raise RuntimeError(f"获取号码失败: {data}")
        raise RuntimeError("获取号码失败")

    def mark_ready(self, activation_id: int) -> None:
        print(f"[SMS] 通知 GrizzlySMS 准备接收短信: activation={activation_id}", flush=True)
        try:
            self.request("setStatus", id=activation_id, status=1)
        except Exception as exc:
            print(f"[SMS] 标记准备接收失败，继续等待验证码: {exc}", flush=True)

    def get_status_detail(self, activation_id: int) -> "SmsCodeStatus":
        from .hero_sms_provider import SmsCodeStatus, _parse_status_payload

        try:
            data = self.request("getStatusV2", id=activation_id)
            if parse_error_text(data) in {"BAD_ACTION", "NO_ACTION", "WRONG_ACTION", "ERROR_SQL"}:
                data = self.request("getStatus", id=activation_id)
        except Exception:
            data = self.request("getStatus", id=activation_id)
        if isinstance(data, str):
            return _parse_status_payload(data)
        code = extract_sms_code(data)
        received_at = None
        if isinstance(data, dict):
            from .hero_sms_provider import extract_sms_received_at

            received_at = extract_sms_received_at(data)
        return SmsCodeStatus(bool(code), code, received_at)

    def get_status(self, activation_id: int) -> tuple[bool, str]:
        status = self.get_status_detail(activation_id)
        return status.received, status.code

    def poll_for_code(self, activation_id: int, *, interval: float = 5.0, max_attempts: int = 60) -> str:
        for attempt in range(1, max_attempts + 1):
            print(f"[SMS] 拉取 GrizzlySMS 验证码: activation={activation_id} ({attempt}/{max_attempts})", flush=True)
            received, code = self.get_status(activation_id)
            if received and code:
                print(f"[SMS] 拉取到短信验证码: {code}", flush=True)
                return code
            print(f"[SMS] 暂未收到验证码，{interval:g}s 后继续拉取", flush=True)
            time.sleep(max(1.0, interval))
        self.cancel(activation_id)
        raise TimeoutError(f"短信验证码超时（等待 {int(interval * max_attempts)} 秒），已取消激活")

    def complete(self, activation_id: int) -> None:
        print(f"[SMS] 确认验证码已使用，完成 GrizzlySMS 激活: activation={activation_id}", flush=True)
        self.request("setStatus", id=activation_id, status=6)
        print("[SMS] 激活已完成", flush=True)

    def cancel(self, activation_id: int) -> None:
        try:
            print(f"[SMS] 取消 GrizzlySMS 激活并尝试退款: activation={activation_id}", flush=True)
            self.request("setStatus", id=activation_id, status=8)
            print("[SMS] 激活已取消（退款）", flush=True)
        except Exception as exc:
            print(f"[SMS] 取消失败: {exc}（号码将在超时后自动退款）", flush=True)


def parse_activation_response(data: Any) -> SmsActivation | None:
    if isinstance(data, str):
        match = re.match(r"ACCESS_NUMBER:(\d+):(\+?\d+)(?::([\d.]+))?$", data.strip())
        if match:
            return SmsActivation(int(match.group(1)), match.group(2), parse_number(match.group(3)))
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    activation_id = parse_integer(payload.get("activationId") or payload.get("activation_id") or payload.get("id"))
    phone = str(payload.get("phoneNumber") or payload.get("phone") or payload.get("number") or "").strip()
    if activation_id is None or not phone:
        return None
    return SmsActivation(activation_id, phone, parse_number(payload.get("activationCost") or payload.get("cost") or payload.get("price")))


def parse_error_text(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("error", "status", "message"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return str(data)


def extract_sms_code(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("code", "smsCode"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
        sms = data.get("sms")
        if isinstance(sms, dict):
            value = str(sms.get("code") or sms.get("text") or "").strip()
            if value:
                match = re.search(r"\d{4,8}", value)
                return match.group(0) if match else value
        for key in ("data", "result"):
            nested = data.get(key)
            value = extract_sms_code(nested)
            if value:
                return value
    if isinstance(data, list):
        for item in data:
            value = extract_sms_code(item)
            if value:
                return value
    return ""


def extract_price_rows(data: Any, service: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return rows
    for key, value in data.items():
        if re.fullmatch(r"\d+", str(key)) and isinstance(value, dict):
            row = dict(value)
            row.setdefault("country", key)
            service_node = value.get(service)
            if isinstance(service_node, dict):
                row.update(service_node)
            rows.append(row)
    for key in ("data", "result", "prices", "countries"):
        nested = data.get(key)
        if isinstance(nested, (dict, list)):
            rows.extend(extract_price_rows(nested, service))
    return rows


def extract_provider_quotes(data: Any, service: str, country: int) -> list[OperatorQuote]:
    quotes: list[OperatorQuote] = []
    for row in extract_price_rows(data, service):
        row_country = parse_integer(row.get("country") or row.get("countryId") or row.get("country_id") or row.get("id"))
        if row_country is not None and row_country != int(country):
            continue
        providers = row.get("providers") or row.get("providerMap") or row.get("providerPrices") or row.get("provider")
        if isinstance(providers, dict):
            for provider_id, payload in providers.items():
                if not isinstance(payload, dict):
                    payload = {"count": payload}
                pid = str(payload.get("provider_id") or payload.get("providerId") or payload.get("id") or provider_id).strip()
                if not pid:
                    continue
                quotes.append(
                    OperatorQuote(
                        operator=pid,
                        label=str(payload.get("name") or payload.get("providerName") or f"服务商 {pid}").strip(),
                        price=parse_number(payload.get("price") or payload.get("cost")),
                        count=parse_integer(payload.get("count") or payload.get("qty") or payload.get("available") or payload.get("stock")),
                    )
                )
        elif isinstance(providers, list):
            for payload in providers:
                if not isinstance(payload, dict):
                    continue
                pid = str(payload.get("id") or payload.get("providerId") or "").strip()
                if not pid:
                    continue
                quotes.append(
                    OperatorQuote(
                        operator=pid,
                        label=str(payload.get("name") or payload.get("providerName") or f"服务商 {pid}").strip(),
                        price=parse_number(payload.get("price") or payload.get("cost")),
                        count=parse_integer(payload.get("count") or payload.get("qty") or payload.get("available") or payload.get("stock")),
                    )
                )
    return sorted(quotes, key=lambda row: ((row.price if row.price is not None else 999999), -(row.count or 0)))
