from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .utils import log
from modules.terminal_theme import install_print_theme


install_print_theme()


def _read_dotenv() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


DEFAULT_PHONE_COUNTRIES = [
    {
        "isoCode": "GB",
        "dialCode": "44",
        "name": "英国",
        "heroSmsCountry": 16,
        "aliases": ["United Kingdom", "UK", "Britain", "Great Britain", "England", "英格兰"],
    },
    {"isoCode": "US", "dialCode": "1", "name": "美国", "aliases": ["United States", "USA", "America"]},
    {"isoCode": "CA", "dialCode": "1", "name": "加拿大", "aliases": ["Canada"]},
    {"isoCode": "AU", "dialCode": "61", "name": "澳大利亚", "aliases": ["Australia"]},
    {"isoCode": "NZ", "dialCode": "64", "name": "新西兰", "aliases": ["New Zealand"]},
    {"isoCode": "IE", "dialCode": "353", "name": "爱尔兰", "aliases": ["Ireland"]},
    {"isoCode": "DE", "dialCode": "49", "name": "德国", "aliases": ["Germany", "Deutschland"]},
    {"isoCode": "FR", "dialCode": "33", "name": "法国", "aliases": ["France"]},
    {"isoCode": "ES", "dialCode": "34", "name": "西班牙", "aliases": ["Spain"]},
    {"isoCode": "IT", "dialCode": "39", "name": "意大利", "aliases": ["Italy"]},
    {"isoCode": "NL", "dialCode": "31", "name": "荷兰", "aliases": ["Netherlands", "Holland"]},
    {"isoCode": "BE", "dialCode": "32", "name": "比利时", "aliases": ["Belgium"]},
    {"isoCode": "AT", "dialCode": "43", "name": "奥地利", "aliases": ["Austria"]},
    {"isoCode": "CH", "dialCode": "41", "name": "瑞士", "aliases": ["Switzerland"]},
    {"isoCode": "SE", "dialCode": "46", "name": "瑞典", "aliases": ["Sweden"]},
    {"isoCode": "NO", "dialCode": "47", "name": "挪威", "aliases": ["Norway"]},
    {"isoCode": "DK", "dialCode": "45", "name": "丹麦", "aliases": ["Denmark"]},
    {"isoCode": "FI", "dialCode": "358", "name": "芬兰", "aliases": ["Finland"]},
    {"isoCode": "PL", "dialCode": "48", "name": "波兰", "aliases": ["Poland"]},
    {"isoCode": "PT", "dialCode": "351", "name": "葡萄牙", "aliases": ["Portugal"]},
    {"isoCode": "CZ", "dialCode": "420", "name": "捷克", "aliases": ["Czech Republic", "Czechia"]},
    {"isoCode": "GR", "dialCode": "30", "name": "希腊", "aliases": ["Greece"]},
    {"isoCode": "RO", "dialCode": "40", "name": "罗马尼亚", "aliases": ["Romania"]},
    {"isoCode": "HU", "dialCode": "36", "name": "匈牙利", "aliases": ["Hungary"]},
    {"isoCode": "TR", "dialCode": "90", "name": "土耳其", "aliases": ["Turkey", "Turkiye"]},
    {"isoCode": "IL", "dialCode": "972", "name": "以色列", "aliases": ["Israel"]},
    {"isoCode": "AE", "dialCode": "971", "name": "阿联酋", "aliases": ["UAE", "United Arab Emirates"]},
    {"isoCode": "SA", "dialCode": "966", "name": "沙特阿拉伯", "aliases": ["Saudi Arabia"]},
    {"isoCode": "SG", "dialCode": "65", "name": "新加坡", "aliases": ["Singapore"]},
    {"isoCode": "MY", "dialCode": "60", "name": "马来西亚", "aliases": ["Malaysia"]},
    {"isoCode": "TH", "dialCode": "66", "name": "泰国", "aliases": ["Thailand"]},
    {"isoCode": "VN", "dialCode": "84", "name": "越南", "aliases": ["Vietnam"]},
    {"isoCode": "PH", "dialCode": "63", "name": "菲律宾", "aliases": ["Philippines"]},
    {"isoCode": "ID", "dialCode": "62", "name": "印度尼西亚", "aliases": ["Indonesia"]},
    {"isoCode": "IN", "dialCode": "91", "name": "印度", "aliases": ["India"]},
    {"isoCode": "JP", "dialCode": "81", "name": "日本", "aliases": ["Japan"]},
    {"isoCode": "KR", "dialCode": "82", "name": "韩国", "aliases": ["South Korea", "Korea Republic"]},
    {"isoCode": "HK", "dialCode": "852", "name": "中国香港", "heroSmsCountry": 14, "aliases": ["Hong Kong"]},
    {"isoCode": "TW", "dialCode": "886", "name": "中国台湾", "aliases": ["Taiwan"]},
    {"isoCode": "BR", "dialCode": "55", "name": "巴西", "aliases": ["Brazil"]},
    {"isoCode": "MX", "dialCode": "52", "name": "墨西哥", "aliases": ["Mexico"]},
    {"isoCode": "AR", "dialCode": "54", "name": "阿根廷", "aliases": ["Argentina"]},
    {"isoCode": "CL", "dialCode": "56", "name": "智利", "aliases": ["Chile"]},
    {"isoCode": "CO", "dialCode": "57", "name": "哥伦比亚", "aliases": ["Colombia"]},
    {"isoCode": "PE", "dialCode": "51", "name": "秘鲁", "aliases": ["Peru"]},
    {"isoCode": "ZA", "dialCode": "27", "name": "南非", "aliases": ["South Africa"]},
    {"isoCode": "EG", "dialCode": "20", "name": "埃及", "aliases": ["Egypt"]},
    {"isoCode": "NG", "dialCode": "234", "name": "尼日利亚", "aliases": ["Nigeria"]},
]


@dataclass(frozen=True)
class PhoneCountry:
    iso_code: str
    dial_code: str
    name: str
    hero_sms_country: int
    aliases: tuple[str, ...] = ()
    price: float | None = None
    count: int | None = None


@dataclass(frozen=True)
class OperatorQuote:
    operator: str
    label: str
    price: float | None
    count: int | None
    note: str = ""


# 自动选价：0=最低价，1=倒数第二低价（避免最低价无号/质量差）
SMS_AUTO_PRICE_RANK = 1


def _price_sort_key(price: float | None, count: int | None = None) -> tuple[float, int]:
    return (price if price is not None else float("inf"), -(count or 0))


def pick_country_by_price_rank(
    countries: list[PhoneCountry],
    rank: int = SMS_AUTO_PRICE_RANK,
) -> PhoneCountry:
    if not countries:
        raise ValueError("no countries to pick")
    ordered = sorted(countries, key=lambda row: _price_sort_key(row.price, row.count))
    index = min(max(0, rank), len(ordered) - 1)
    return ordered[index]


def pick_operator_quote_by_price_rank(
    quotes: list[OperatorQuote],
    rank: int = SMS_AUTO_PRICE_RANK,
    *,
    max_price: float | None = None,
) -> OperatorQuote | None:
    eligible = [
        row
        for row in quotes
        if row.price is not None
        and (max_price is None or row.price <= max_price)
        and (row.count is None or row.count > 0)
    ]
    if not eligible:
        return None
    ordered = sorted(eligible, key=lambda row: _price_sort_key(row.price, row.count))
    index = min(max(0, rank), len(ordered) - 1)
    return ordered[index]


def resolve_hero_sms_operator_for_purchase(
    provider: "HeroSMSProvider",
    service: str,
    country_id: int,
    max_price: float,
    prefix: str,
    *,
    price_rank: int = SMS_AUTO_PRICE_RANK,
) -> str:
    """未指定运营商时，按价格档位自动选择（默认倒数第二低价）。"""
    quotes = provider.get_operator_quote_options(service, country_id)
    picked = pick_operator_quote_by_price_rank(quotes, rank=price_rank, max_price=max_price)
    if picked is None:
        log(f"{prefix} [SMS] 无满足上限的运营商报价，使用 any（由平台分配）")
        return ""
    rank_label = "最低" if price_rank == 0 else f"第{price_rank + 1}低"
    log(
        f"{prefix} [SMS] 自动选择运营商({rank_label}价): {picked.label} "
        f"${picked.price} 库存={picked.count if picked.count is not None else '-'}"
    )
    return picked.operator


@dataclass(frozen=True)
class SmsActivation:
    activation_id: int
    phone_number: str
    activation_cost: float | None = None


@dataclass(frozen=True)
class SmsCodeStatus:
    received: bool
    code: str
    received_at: datetime | None = None


def parse_sms_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        ts = int(text)
        if ts > 1e12:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_sms_received_at(data: Any) -> datetime | None:
    if not isinstance(data, dict):
        return None
    sms = data.get("sms")
    if isinstance(sms, list) and sms:
        for item in reversed(sms):
            if not isinstance(item, dict):
                continue
            for key in ("receivedAt", "received_at", "date", "createdAt", "created_at", "time"):
                parsed = parse_sms_timestamp(item.get(key))
                if parsed:
                    return parsed
    if isinstance(sms, dict):
        for key in ("receivedAt", "received_at", "date", "createdAt", "created_at", "time"):
            parsed = parse_sms_timestamp(sms.get(key))
            if parsed:
                return parsed
    for key in ("receivedAt", "received_at", "date", "createdAt", "created_at", "time"):
        parsed = parse_sms_timestamp(data.get(key))
        if parsed:
            return parsed
    return None


def _normalize_status_code(raw: str) -> str:
    code = str(raw or "").strip()
    if not code:
        return ""
    if re.fullmatch(r"\d{4,8}", code):
        return code
    from .utils import extract_code

    parsed = extract_code(code)
    if parsed:
        return parsed
    digits = re.sub(r"\D+", "", code)
    if 4 <= len(digits) <= 8:
        return digits
    return ""


def _parse_status_payload(data: Any) -> SmsCodeStatus:
    if isinstance(data, str):
        if data == "STATUS_WAIT_CODE":
            return SmsCodeStatus(False, "")
        if data == "STATUS_CANCEL":
            raise RuntimeError("激活已被取消")
        if data.startswith("STATUS_OK:"):
            code = _normalize_status_code(data.split(":", 1)[1].strip())
            return SmsCodeStatus(bool(code), code, None)
        return SmsCodeStatus(False, "")
    received_at = extract_sms_received_at(data if isinstance(data, dict) else {})
    code = str(((data or {}).get("sms") or {}).get("code") or "").strip()
    code = _normalize_status_code(code)
    if not code and isinstance(data, dict):
        sms = data.get("sms") or {}
        if isinstance(sms, dict):
            text = str(sms.get("text") or sms.get("message") or "").strip()
            code = _normalize_status_code(text)
    return SmsCodeStatus(bool(code), code, received_at)


def parse_number(value: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value if value is not None else ""))
    return float(match.group(0)) if match else None


def parse_integer(value: Any) -> int | None:
    match = re.search(r"-?\d+", str(value if value is not None else ""))
    return int(match.group(0)) if match else None


def normalize_phone_countries(items: list[dict[str, Any]]) -> list[PhoneCountry]:
    countries: list[PhoneCountry] = []
    for item in items:
        iso = str(item.get("isoCode") or item.get("iso") or "").strip().upper()
        dial = str(item.get("dialCode") or item.get("phoneCode") or "").strip().lstrip("+")
        name = str(item.get("name") or item.get("country") or "").strip()
        hero_id = parse_integer(item.get("heroSmsCountry"))
        aliases = tuple(str(v).strip() for v in item.get("aliases", []) if str(v).strip())
        if iso and dial and name:
            countries.append(PhoneCountry(iso, dial, name, hero_id or 0, aliases))
    return countries


def configured_country_catalog() -> list[PhoneCountry]:
    return normalize_phone_countries(DEFAULT_PHONE_COUNTRIES)


class HeroSMSProvider:
    def __init__(self, api_key: str, *, base_url: str = "https://hero-sms.com/stubs/handler_api.php", timeout: int = 30) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.proxies = self._configured_proxies()

    @staticmethod
    def _configured_proxies() -> dict[str, str] | None:
        env = _read_dotenv()
        enabled = str(env.get("HERO_SMS_USE_PROXY") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return None
        proxy = (
            env.get("HERO_SMS_PROXY")
            or env.get("HTTPS_PROXY")
            or env.get("HTTP_PROXY")
            or ""
        ).strip()
        if not proxy:
            proxy_file = Path(env.get("HERO_SMS_PROXY_FILE") or env.get("PROXY_FILE") or "data/proxies/proxies.txt")
            if not proxy_file.is_absolute():
                proxy_file = Path(__file__).resolve().parents[1] / proxy_file
            if proxy_file.exists():
                for raw in proxy_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                    line = raw.strip()
                    if line and not line.startswith("#"):
                        proxy = line
                        break
        if not proxy:
            return None
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        return {"http": proxy, "https": proxy}

    def request(self, action: str, **params: Any) -> Any:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            self.base_url,
            params={"api_key": self.api_key, "action": action, **params},
            timeout=self.timeout,
            proxies=self.proxies,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return response.text.strip()

    def _extract_http_business_error(self, exc: Exception) -> str | None:
        if not isinstance(exc, requests.HTTPError):
            return None
        response = exc.response
        if response is None:
            return None
        text = (response.text or "").strip()
        title = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                title = str(payload.get("title") or payload.get("error") or "").strip().upper()
        except Exception:
            title = ""
        merged = f"{title} {text}".upper()
        if "NO_BALANCE" in merged or "PAYMENT REQUIRED" in merged:
            return "NO_BALANCE"
        if "NO_NUMBERS" in merged:
            return "NO_NUMBERS"
        if "BAD_KEY" in merged or "UNAUTHORIZED" in merged:
            return "BAD_KEY"
        return None

    def _normalize_get_number_payload(self, data: Any) -> SmsActivation | None:
        if isinstance(data, str):
            # Legacy getNumber returns ACCESS_NUMBER:<id>:<phone>
            if data.startswith("ACCESS_NUMBER:"):
                parts = data.split(":")
                if len(parts) >= 3:
                    activation_id = parse_integer(parts[1])
                    phone_number = str(parts[2] or "").strip()
                    if activation_id is not None and phone_number:
                        if not phone_number.startswith("+"):
                            phone_number = f"+{phone_number}"
                        return SmsActivation(activation_id=activation_id, phone_number=phone_number, activation_cost=None)
            return None
        activation_id = parse_integer(data.get("activationId") if isinstance(data, dict) else None)
        phone_number = str((data or {}).get("phoneNumber") or "").strip()
        if activation_id is None or not phone_number:
            return None
        if not phone_number.startswith("+"):
            phone_number = f"+{phone_number}"
        cost = parse_number((data or {}).get("activationCost"))
        return SmsActivation(activation_id=activation_id, phone_number=phone_number, activation_cost=cost)

    def get_countries(self) -> list[dict[str, Any]]:
        for action in ("getCountries", "getCountriesList"):
            try:
                data = self.request(action)
                countries = parse_countries_response(data)
                if countries:
                    return countries
            except Exception:
                continue
        return []

    def get_top_countries_by_service(self, service: str = "dr") -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                parsed = parse_top_countries_response(self.request(action, service=service))
                if parsed:
                    return sorted(parsed, key=lambda row: (row["price"], -(row.get("count") or 0)))
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("未能获取 Top Countries 列表")

    def get_price_matrix(self, service: str = "dr") -> Any:
        last_error: Exception | None = None
        for action in ("getPricesVerification", "getPrices"):
            try:
                return self.request(action, service=service)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("未能获取 HeroSMS 价格列表")

    def list_country_prices(self, service: str, countries: list[PhoneCountry]) -> list[PhoneCountry]:
        matrix = self.get_price_matrix(service)
        priced: list[PhoneCountry] = []
        for country in countries:
            if country.hero_sms_country <= 0:
                continue
            parsed = extract_country_price(matrix, country.hero_sms_country, service)
            if not parsed or parsed.get("price") is None:
                continue
            priced.append(replace(country, price=parsed.get("price"), count=parsed.get("count")))
        return sorted(priced, key=lambda row: ((row.price if row.price is not None else 999999), -(row.count or 0)))

    def quote_service_country(self, service: str, country_id: int) -> dict[str, Any] | None:
        return extract_country_price(self.get_price_matrix(service), country_id, service)

    def get_operators(self, country: int) -> list[str]:
        data = self.request("getOperators", country=country)
        raw = []
        if isinstance(data, dict):
            operators = data.get("countryOperators") or {}
            raw = operators.get(str(country)) or operators.get(country) or []
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def get_operator_quote_options(self, service: str, country: int) -> list[OperatorQuote]:
        result: list[OperatorQuote] = []
        for operator in self.get_operators(country):
            try:
                parsed = extract_country_price(self.request("getPrices", service=service, country=country, operator=operator), country, service)
                result.append(
                    OperatorQuote(
                        operator=operator,
                        label=operator,
                        price=parsed.get("price") if parsed else None,
                        count=parsed.get("count") if parsed else None,
                    )
                )
            except Exception as exc:
                result.append(OperatorQuote(operator=operator, label=operator, price=None, count=None, note=str(exc)[:80]))
        return sorted(result, key=lambda row: _price_sort_key(row.price, row.count))

    def get_number(
        self,
        service: str = "dr",
        country: int = 16,
        *,
        operator: str = "",
        max_retries: int = 5,
        max_price: float | None = None,
    ) -> SmsActivation:
        # 英国 dr 经常无库存，允许自动尝试 tg 兜底。
        service_candidates: list[str] = [service]
        if service.strip().lower() == "dr":
            service_candidates.append("tg")

        for service_index, service_name in enumerate(service_candidates, start=1):
            if service_index > 1:
                print(f"[SMS] 当前 service={service} 未取到号，自动回退 service={service_name} 再试", flush=True)
            for attempt in range(1, max_retries + 1):
                try:
                    params: dict[str, Any] = {"service": service_name, "country": country}
                    if operator:
                        params["operator"] = operator
                    if max_price is not None:
                        params["maxPrice"] = max_price
                    operator_text = f", operator={operator}" if operator else ", operator=任何运营商"
                    max_price_text = f", maxPrice={max_price}" if max_price is not None else ""
                    print(
                        f"[SMS] 请求 HeroSMS 号码: service={service_name}, country={country}{operator_text}{max_price_text} ({attempt}/{max_retries})",
                        flush=True,
                    )
                    data = self.request("getNumberV2", **params)
                except Exception as exc:
                    business_error = self._extract_http_business_error(exc)
                    if business_error == "NO_BALANCE":
                        raise RuntimeError("HeroSMS 余额不足") from exc
                    if business_error == "BAD_KEY":
                        raise RuntimeError("HeroSMS API Key 无效") from exc
                    if business_error == "NO_NUMBERS":
                        if attempt < max_retries:
                            print(f"[SMS] 暂无可用号码，3秒后重试... ({attempt}/{max_retries})", flush=True)
                            time.sleep(3)
                            continue
                        break
                    if attempt < max_retries:
                        print(f"[SMS] API 请求失败: {exc}，5秒后重试... ({attempt}/{max_retries})", flush=True)
                        time.sleep(5)
                        continue
                    break

                if isinstance(data, str):
                    if data == "NO_BALANCE":
                        raise RuntimeError("HeroSMS 余额不足")
                    if data == "BAD_KEY":
                        raise RuntimeError("HeroSMS API Key 无效")
                    if data == "NO_NUMBERS" and attempt < max_retries:
                        print(f"[SMS] 暂无可用号码，3秒后重试... ({attempt}/{max_retries})", flush=True)
                        time.sleep(3)
                        continue
                    if data == "NO_NUMBERS":
                        break
                    raise RuntimeError(f"获取号码失败: {data}")

                activation = self._normalize_get_number_payload(data)
                if not activation:
                    raise RuntimeError(f"获取号码失败: {data}")
                if (
                    max_price is not None
                    and activation.activation_cost is not None
                    and activation.activation_cost > max_price
                ):
                    try:
                        self.cancel(activation.activation_id)
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"取号费用 ${activation.activation_cost} 超过上限 ${max_price}，已取消"
                    )
                print(
                    f"[SMS] 获取号码成功: {activation.phone_number} (activation={activation.activation_id}, 费用=${activation.activation_cost if activation.activation_cost is not None else '-'})",
                    flush=True,
                )
                return activation

            # v2 连续失败后，尝试 legacy 接口一次（部分账号在旧接口上仍可取号）
            try:
                params = {"service": service_name, "country": country}
                if operator:
                    params["operator"] = operator
                if max_price is not None:
                    params["maxPrice"] = max_price
                legacy = self.request("getNumber", **params)
                activation = self._normalize_get_number_payload(legacy)
                if activation:
                    if (
                        max_price is not None
                        and activation.activation_cost is not None
                        and activation.activation_cost > max_price
                    ):
                        try:
                            self.cancel(activation.activation_id)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"取号费用 ${activation.activation_cost} 超过上限 ${max_price}，已取消"
                        )
                    print(f"[SMS] 通过 legacy getNumber 取号成功: {activation.phone_number} (activation={activation.activation_id})", flush=True)
                    return activation
                if isinstance(legacy, str) and legacy == "NO_BALANCE":
                    raise RuntimeError("HeroSMS 余额不足")
                if isinstance(legacy, str) and legacy == "BAD_KEY":
                    raise RuntimeError("HeroSMS API Key 无效")
            except RuntimeError:
                raise
            except Exception:
                pass

        raise RuntimeError("当前无可用号码（已尝试全部 service）")

    def mark_ready(self, activation_id: int) -> None:
        print(f"[SMS] 通知 HeroSMS 准备接收短信: activation={activation_id}", flush=True)
        self.request("setStatus", id=activation_id, status=1)
        print("[SMS] 已标记为准备接收短信", flush=True)

    def get_status_detail(self, activation_id: int) -> SmsCodeStatus:
        data = self.request("getStatusV2", id=activation_id)
        return _parse_status_payload(data)

    def get_status(self, activation_id: int) -> tuple[bool, str]:
        status = self.get_status_detail(activation_id)
        return status.received, status.code

    def poll_for_code(self, activation_id: int, *, interval: float = 5.0, max_attempts: int = 60) -> str:
        for attempt in range(1, max_attempts + 1):
            print(f"[SMS] 拉取短信验证码: activation={activation_id} ({attempt}/{max_attempts})", flush=True)
            received, code = self.get_status(activation_id)
            if received and code:
                print(f"[SMS] 拉取到短信验证码: {code}", flush=True)
                return code
            print(f"[SMS] 暂未收到验证码，{interval:g}s 后继续拉取", flush=True)
            time.sleep(max(1.0, interval))
        self.cancel(activation_id)
        raise TimeoutError(f"短信验证码超时（等待 {int(interval * max_attempts)} 秒），已取消激活")

    def complete(self, activation_id: int) -> None:
        print(f"[SMS] 确认验证码已使用，完成激活: activation={activation_id}", flush=True)
        self.request("setStatus", id=activation_id, status=6)
        print("[SMS] 激活已完成", flush=True)

    def cancel(self, activation_id: int) -> None:
        try:
            print(f"[SMS] 取消激活并尝试退款: activation={activation_id}", flush=True)
            self.request("setStatus", id=activation_id, status=8)
            print("[SMS] 激活已取消（退款）", flush=True)
        except Exception as exc:
            print(f"[SMS] 取消失败: {exc}（号码将在超时后自动退款）", flush=True)


def parse_countries_response(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def push(country_id: Any, payload: Any) -> None:
        hero_id = parse_integer(country_id)
        if hero_id is None:
            return
        if isinstance(payload, str):
            result.append({"heroSmsCountry": hero_id, "apiName": payload.strip()})
            return
        if not isinstance(payload, dict):
            return
        result.append(
            {
                "heroSmsCountry": hero_id,
                "apiName": str(payload.get("name") or payload.get("country") or payload.get("title") or payload.get("eng") or payload.get("en") or payload.get("label") or "").strip(),
                "isoCode": str(payload.get("isoCode") or payload.get("iso") or payload.get("code") or payload.get("iso2") or "").strip().upper(),
                "dialCode": str(payload.get("dialCode") or payload.get("phoneCode") or payload.get("prefix") or "").strip().lstrip("+"),
            }
        )

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                push(item.get("id") or item.get("countryId") or item.get("country_id"), item)
        return result
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        if re.fullmatch(r"\d+", str(key)):
            push(key, value)
        elif isinstance(value, dict):
            nested_id = value.get("id") or value.get("countryId") or value.get("country_id")
            if nested_id is not None:
                payload = dict(value)
                payload["name"] = value.get("name") or value.get("chn") or value.get("eng") or value.get("rus") or key
                push(nested_id, payload)
    if result:
        return result
    for value in data.values():
        if isinstance(value, (dict, list)):
            nested = parse_countries_response(value)
            if nested:
                result.extend(nested)
    return result


def parse_top_countries_response(data: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def push(item: Any) -> None:
        if not isinstance(item, dict):
            return
        hero_id = parse_integer(item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id"))
        price = parse_number(item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice"))
        count = parse_integer(item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total"))
        if hero_id is None or price is None:
            return
        rows.append(
            {
                "heroSmsCountry": hero_id,
                "price": price,
                "count": count,
                "apiName": str(item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or item.get("text") or item.get("label") or "").strip(),
                "isoCode": str(item.get("isoCode") or item.get("iso") or item.get("code") or item.get("iso2") or "").strip().upper(),
                "dialCode": str(item.get("dialCode") or item.get("phoneCode") or item.get("prefix") or item.get("phone_prefix") or "").strip().lstrip("+"),
            }
        )

    if isinstance(data, list):
        for item in data:
            push(item)
        return rows
    if not isinstance(data, dict):
        return rows
    for key, value in data.items():
        if re.fullmatch(r"\d+", str(key)) and isinstance(value, dict):
            push(value)
    if rows:
        return rows
    for key in ("data", "result", "response"):
        nested = data.get(key)
        if isinstance(nested, (dict, list)):
            parsed = parse_top_countries_response(nested)
            if parsed:
                return parsed
    return rows


def unwrap_price_matrix(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    for key in ("data", "result", "prices", "countries", "response"):
        value = raw.get(key)
        if isinstance(value, dict):
            return unwrap_price_matrix(value)
    return raw


def extract_price_from_node(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None
    price = parse_number(node.get("cost") or node.get("price") or node.get("activationCost") or node.get("amount") or node.get("rate"))
    count = parse_integer(node.get("count") or node.get("qty") or node.get("available") or node.get("stock") or node.get("total"))
    if price is None and count is None:
        return None
    return {"price": price, "count": count}


def extract_country_price(raw: Any, country_id: int, service: str) -> dict[str, Any] | None:
    matrix = unwrap_price_matrix(raw)
    if isinstance(matrix, list):
        for item in matrix:
            if not isinstance(item, dict):
                continue
            item_country = parse_integer(item.get("countryId") or item.get("country_id") or item.get("country") or item.get("id"))
            if item_country != int(country_id):
                continue
            direct = extract_price_from_node(item)
            if direct:
                return direct
            service_node = item.get(str(service)) or item.get("serviceData") or item.get("data")
            parsed = extract_price_from_node(service_node)
            if parsed:
                return parsed
        return None

    if not isinstance(matrix, dict):
        return None
    country_key = str(country_id)
    service_key = str(service)
    candidates = [
        (matrix.get(service_key) or {}).get(country_key) if isinstance(matrix.get(service_key), dict) else None,
        (matrix.get(country_key) or {}).get(service_key) if isinstance(matrix.get(country_key), dict) else None,
        (matrix.get(country_key) or {}).get("default") if isinstance(matrix.get(country_key), dict) else None,
        matrix.get(country_key),
        matrix.get(service_key),
    ]
    for candidate in candidates:
        parsed = extract_price_from_node(candidate)
        if parsed:
            return parsed
    return None


def enrich_countries_with_api(catalog: list[PhoneCountry], api_countries: list[dict[str, Any]]) -> list[PhoneCountry]:
    by_id = {country.hero_sms_country: country for country in catalog}
    by_iso = {country.iso_code.upper(): country for country in catalog}
    sms_activate_ids = {
        0: "RU",
        1: "UA",
        2: "KZ",
        3: "CN",
        4: "PH",
        6: "ID",
        7: "MY",
        10: "VN",
        12: "US",
        13: "IL",
        14: "HK",
        15: "PL",
        16: "GB",
        36: "CA",
        39: "AR",
        43: "DE",
        44: "LT",
        45: "HR",
        46: "SE",
        50: "TH",
        52: "MX",
        53: "TW",
        54: "ES",
        56: "FR",
        73: "BR",
        78: "NL",
        86: "IT",
        87: "PY",
        117: "PT",
        175: "AU",
        187: "US",
    }
    by_name: dict[str, PhoneCountry] = {}
    for country in catalog:
        for name in (country.name, *country.aliases):
            if name:
                by_name[name.strip().lower()] = country

    enriched: list[PhoneCountry] = []
    seen: set[int] = set()
    seen_iso: set[str] = set()
    for item in api_countries:
        hero_id = parse_integer(item.get("heroSmsCountry"))
        if hero_id is None:
            continue
        api_iso = str(item.get("isoCode") or "").strip().upper()
        api_name = str(item.get("apiName") or "").strip()
        api_dial = str(item.get("dialCode") or "").strip().lstrip("+")
        base = (
            by_id.get(hero_id)
            or (by_iso.get(api_iso) if api_iso else None)
            or by_iso.get(sms_activate_ids.get(hero_id, ""))
            or by_name.get(api_name.lower())
        )
        if base:
            enriched.append(replace(base, hero_sms_country=hero_id))
            seen.add(hero_id)
            seen_iso.add(base.iso_code.upper())
        elif api_iso and api_dial and api_name:
            enriched.append(PhoneCountry(api_iso, api_dial, api_name, hero_id))
            seen.add(hero_id)
            seen_iso.add(api_iso.upper())
    for country in catalog:
        if country.hero_sms_country not in seen and country.iso_code.upper() not in seen_iso:
            enriched.append(country)
    return enriched


def match_country(value: str, rows: list[PhoneCountry]) -> PhoneCountry | None:
    """Resolve a user-provided country descriptor to a PhoneCountry.

    Precedence for digit input:
      1. Explicit hero_sms_country (platform country id)
      2. Row index (1-based) — only as a fallback, since a tiny number would
         otherwise collide with country ids like "6" (Indonesia) that also
         happen to fall within [1, len(rows)].
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        # 优先按 HeroSMS 国家 ID 匹配（避免 "6"=印尼 被当成列表序号 6）
        by_country_id = next((row for row in rows if row.hero_sms_country == number), None)
        if by_country_id:
            return by_country_id
        if 1 <= number <= len(rows):
            return rows[number - 1]
        return None
    lowered = text.lower()
    return next(
        (
            row
            for row in rows
            if row.iso_code.lower() == lowered
            or row.name.lower() == lowered
            or any(alias.lower() == lowered for alias in row.aliases)
        ),
        None,
    )


def local_phone_number(phone: str, country: PhoneCountry) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    dial = re.sub(r"\D+", "", country.dial_code)
    if dial and digits.startswith(dial):
        return digits[len(dial):]
    return digits
