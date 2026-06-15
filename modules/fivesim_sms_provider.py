"""5sim.net SMS provider.

API ref: https://5sim.net/docs

5sim 的 API 与 HeroSMS/Grizzly(SMS-Activate 协议) 差异很大：
- 基于 REST + Bearer JWT
- 国家用英文 slug（如 indonesia / philippines / colombia），不用数字 ID
- 产品用名称（openai / facebook），不用字母缩写（dr）
- 价格矩阵按 country → product → operator 三层嵌套

本模块提供与 HeroSMSProvider 相同的接口签名，便于在
authorization_flow / free_register 里统一调用：
    get_number / mark_ready / get_status / complete / cancel
    get_countries / get_price_matrix / list_country_prices / get_operator_quote_options
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import requests

from modules.hero_sms_provider import (
    DEFAULT_PHONE_COUNTRIES,
    OperatorQuote,
    PhoneCountry,
    SMS_AUTO_PRICE_RANK,
    SmsActivation,
    parse_integer,
    parse_number,
)
from modules.terminal_theme import install_print_theme


install_print_theme()


# 5sim 没有"国家 ID"概念，全用英文 slug。把 ISO 映射到 5sim 的 country slug。
FIVESIM_ISO_TO_COUNTRY = {
    "AF": "afghanistan", "AL": "albania", "DZ": "algeria", "AO": "angola",
    "AR": "argentina", "AM": "armenia", "AW": "aruba", "AU": "australia",
    "AT": "austria", "AZ": "azerbaijan", "BS": "bahamas", "BH": "bahrain",
    "BD": "bangladesh", "BB": "barbados", "BE": "belgium", "BZ": "belize",
    "BJ": "benin", "BT": "bhutane", "BA": "bih", "BO": "bolivia",
    "BW": "botswana", "BR": "brazil", "BG": "bulgaria", "BF": "burkinafaso",
    "BI": "burundi", "KH": "cambodia", "CM": "cameroon", "CA": "canada",
    "CV": "capeverde", "TD": "chad", "CL": "chile", "CO": "colombia",
    "KM": "comoros", "CG": "congo", "CR": "costarica", "HR": "croatia",
    "CY": "cyprus", "CZ": "czech", "DK": "denmark", "DJ": "djibouti",
    "DO": "dominicana", "TL": "easttimor", "EC": "ecuador", "EG": "egypt",
    "GB": "england", "UK": "england", "GQ": "equatorialguinea", "EE": "estonia",
    "ET": "ethiopia", "FI": "finland", "FR": "france", "GF": "frenchguiana",
    "GA": "gabon", "GM": "gambia", "GE": "georgia", "DE": "germany",
    "GH": "ghana", "GR": "greece", "GP": "guadeloupe", "GT": "guatemala",
    "GN": "guinea", "GW": "guineabissau", "GY": "guyana", "HT": "haiti",
    "HN": "honduras", "HK": "hongkong", "HU": "hungary", "IN": "india",
    "ID": "indonesia", "IE": "ireland", "IL": "israel", "IT": "italy",
    "CI": "ivorycoast", "JM": "jamaica", "JP": "japan", "JO": "jordan",
    "KZ": "kazakhstan", "KE": "kenya", "KW": "kuwait", "KG": "kyrgyzstan",
    "LA": "laos", "LV": "latvia", "LS": "lesotho", "LR": "liberia",
    "LT": "lithuania", "LU": "luxembourg", "MO": "macau", "MG": "madagascar",
    "MW": "malawi", "MY": "malaysia", "MV": "maldives", "MR": "mauritania",
    "MU": "mauritius", "MX": "mexico", "MD": "moldova", "MN": "mongolia",
    "ME": "montenegro", "MA": "morocco", "MZ": "mozambique", "NA": "namibia",
    "NP": "nepal", "NL": "netherlands", "NC": "newcaledonia", "NI": "nicaragua",
    "NG": "nigeria", "MK": "northmacedonia", "NO": "norway", "OM": "oman",
    "PK": "pakistan", "PA": "panama", "PG": "papuanewguinea", "PY": "paraguay",
    "PE": "peru", "PH": "philippines", "PL": "poland", "PT": "portugal",
    "PR": "puertorico", "RE": "reunion", "RO": "romania", "RW": "rwanda",
    "SV": "salvador", "WS": "samoa", "SA": "saudiarabia", "SN": "senegal",
    "RS": "serbia", "SC": "seychelles", "SL": "sierraleone", "SK": "slovakia",
    "SI": "slovenia", "SB": "solomonislands", "ZA": "southafrica", "ES": "spain",
    "LK": "srilanka", "SR": "suriname", "SZ": "swaziland", "SE": "sweden",
    "TW": "taiwan", "TJ": "tajikistan", "TZ": "tanzania", "TH": "thailand",
    "TT": "tit", "TG": "togo", "TN": "tunisia", "TM": "turkmenistan",
    "UG": "uganda", "UY": "uruguay", "US": "usa", "UZ": "uzbekistan",
    "VE": "venezuela", "VN": "vietnam", "ZM": "zambia",
}

FIVESIM_COUNTRY_TO_ISO = {slug: iso for iso, slug in FIVESIM_ISO_TO_COUNTRY.items()}

# 常用拨号码（5sim 有时不返回 prefix，就用这张表兜底）
FIVESIM_NAME_TO_DIAL = {
    "afghanistan": "93", "albania": "355", "algeria": "213", "angola": "244",
    "argentina": "54", "armenia": "374", "aruba": "297", "australia": "61",
    "austria": "43", "azerbaijan": "994", "bahamas": "1242", "bahrain": "973",
    "bangladesh": "880", "barbados": "1246", "belgium": "32", "belize": "501",
    "benin": "229", "bhutane": "975", "bih": "387", "bolivia": "591",
    "botswana": "267", "brazil": "55", "bulgaria": "359", "burkinafaso": "226",
    "burundi": "257", "cambodia": "855", "cameroon": "237", "canada": "1",
    "capeverde": "238", "chad": "235", "chile": "56", "colombia": "57",
    "comoros": "269", "congo": "242", "costarica": "506", "croatia": "385",
    "cyprus": "357", "czech": "420", "denmark": "45", "djibouti": "253",
    "dominicana": "1809", "easttimor": "670", "ecuador": "593", "egypt": "20",
    "england": "44", "equatorialguinea": "240", "estonia": "372", "ethiopia": "251",
    "finland": "358", "france": "33", "frenchguiana": "594", "gabon": "241",
    "gambia": "220", "georgia": "995", "germany": "49", "ghana": "233",
    "greece": "30", "guadeloupe": "590", "guatemala": "502", "guinea": "224",
    "guineabissau": "245", "guyana": "592", "haiti": "509", "honduras": "504",
    "hongkong": "852", "hungary": "36", "india": "91", "indonesia": "62",
    "ireland": "353", "israel": "972", "italy": "39", "ivorycoast": "225",
    "jamaica": "1876", "japan": "81", "jordan": "962", "kazakhstan": "7",
    "kenya": "254", "kuwait": "965", "kyrgyzstan": "996", "laos": "856",
    "latvia": "371", "lesotho": "266", "liberia": "231", "lithuania": "370",
    "luxembourg": "352", "macau": "853", "madagascar": "261", "malawi": "265",
    "malaysia": "60", "maldives": "960", "mauritania": "222", "mauritius": "230",
    "mexico": "52", "moldova": "373", "mongolia": "976", "montenegro": "382",
    "morocco": "212", "mozambique": "258", "namibia": "264", "nepal": "977",
    "netherlands": "31", "newcaledonia": "687", "nicaragua": "505", "nigeria": "234",
    "northmacedonia": "389", "norway": "47", "oman": "968", "pakistan": "92",
    "panama": "507", "papuanewguinea": "675", "paraguay": "595", "peru": "51",
    "philippines": "63", "poland": "48", "portugal": "351", "puertorico": "1787",
    "reunion": "262", "romania": "40", "rwanda": "250", "salvador": "503",
    "samoa": "685", "saudiarabia": "966", "senegal": "221", "serbia": "381",
    "seychelles": "248", "sierraleone": "232", "slovakia": "421", "slovenia": "386",
    "solomonislands": "677", "southafrica": "27", "spain": "34", "srilanka": "94",
    "suriname": "597", "swaziland": "268", "sweden": "46", "taiwan": "886",
    "tajikistan": "992", "tanzania": "255", "thailand": "66", "tit": "1868",
    "togo": "228", "tunisia": "216", "turkmenistan": "993", "uganda": "256",
    "uruguay": "598", "usa": "1", "uzbekistan": "998", "venezuela": "58",
    "vietnam": "84", "zambia": "260",
}

# OpenAI/ChatGPT 产品在 5sim 上的 slug
FIVESIM_OPENAI_PRODUCT = "openai"

# HeroSMS service=dr 在 5sim 上对应 openai
DEFAULT_SERVICE = FIVESIM_OPENAI_PRODUCT


class FiveSimProvider:
    """Adapter exposing HeroSMS-compatible interface against 5sim.net REST API."""

    def __init__(self, api_key: str, *, base_url: str = "https://5sim.net/v1", timeout: int = 30) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 5sim 约束：每个激活只有一个 order id，用这个 mapping 维护 id → 使用状态
        self._completed: set[int] = set()

    # ------------- low-level helpers -------------
    def _headers(self, authed: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if authed:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, *, authed: bool = True, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = requests.request(
            method,
            url,
            headers=self._headers(authed=authed),
            params=params,
            timeout=self.timeout,
        )
        # 5sim 会在 200 里返回错误字符串（如 "no free phones" / "not enough user balance"）
        text = (response.text or "").strip()
        if response.status_code >= 400 and response.status_code != 200:
            # Some endpoints like ban/cancel return 400 with plain text error
            raise RuntimeError(f"5sim {response.status_code}: {text[:200] or 'empty'}")
        if not text:
            return {}
        try:
            return response.json()
        except ValueError:
            return text  # plain-text error like "no free phones"

    # ------------- public API: profile / balance -------------
    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/user/profile")

    def balance(self) -> float | None:
        payload = self.profile()
        if not isinstance(payload, dict):
            return None
        return parse_number(payload.get("balance"))

    # ------------- public API: countries / prices -------------
    def get_countries(self) -> list[dict[str, Any]]:
        """Returns list shaped compatible with parse_countries_response upstream.
        Each item: {heroSmsCountry: synthetic_id, apiName, isoCode, dialCode}.
        5sim 没有数字 ID，这里用 slug 的哈希值的后 6 位当"合成 ID"，以便和 PhoneCountry
        结构兼容；真正的调用方应使用 iso_code / slug。
        """
        data = self._request("GET", "/guest/countries", authed=False)
        if not isinstance(data, dict):
            return []
        rows: list[dict[str, Any]] = []
        for slug, meta in data.items():
            if not isinstance(meta, dict):
                continue
            iso_map = meta.get("iso") or {}
            prefix_map = meta.get("prefix") or {}
            iso_code = next(iter(iso_map.keys()), "").upper() if isinstance(iso_map, dict) else ""
            dial = next(iter(prefix_map.keys()), "").lstrip("+") if isinstance(prefix_map, dict) else ""
            if not dial:
                dial = FIVESIM_NAME_TO_DIAL.get(slug, "")
            if not iso_code:
                iso_code = FIVESIM_COUNTRY_TO_ISO.get(slug, "")
            name = str(meta.get("text_en") or slug).strip()
            synthetic_id = _slug_to_synth_id(slug)
            rows.append({
                "heroSmsCountry": synthetic_id,
                "apiName": name,
                "isoCode": iso_code,
                "dialCode": dial,
                "fiveSimCountry": slug,
            })
        return rows

    def list_country_prices(self, service: str, countries: list[PhoneCountry]) -> list[PhoneCountry]:
        """Fetch price per country for the given 5sim product (e.g. 'openai')."""
        product = _normalize_product(service)
        data = self._request("GET", "/guest/prices", authed=False, params={"product": product})
        if not isinstance(data, dict):
            return []
        product_map = data.get(product) or {}
        if not isinstance(product_map, dict):
            return []
        priced: list[PhoneCountry] = []
        for country in countries:
            slug = self._country_slug(country)
            operators = product_map.get(slug)
            if not isinstance(operators, dict) or not operators:
                continue
            costs: list[float] = []
            total_count = 0
            for op_name, op_meta in operators.items():
                if not isinstance(op_meta, dict):
                    continue
                cost = parse_number(op_meta.get("cost"))
                count = parse_integer(op_meta.get("count"))
                if count:
                    total_count += count
                if cost is not None:
                    costs.append(cost)
            costs.sort()
            best_cost = (
                costs[SMS_AUTO_PRICE_RANK]
                if len(costs) > SMS_AUTO_PRICE_RANK
                else (costs[0] if costs else None)
            )
            if best_cost is None:
                continue
            priced.append(replace(country, price=best_cost, count=total_count or None))
        return sorted(priced, key=lambda row: ((row.price if row.price is not None else 1e9), -(row.count or 0)))

    def get_operator_quote_options(self, service: str, country: Any) -> list[OperatorQuote]:
        """Return operators available for the given country + product."""
        slug = country if isinstance(country, str) else self._country_slug(country)
        product = _normalize_product(service)
        data = self._request(
            "GET",
            "/guest/prices",
            authed=False,
            params={"country": slug, "product": product},
        )
        if not isinstance(data, dict):
            return []
        country_map = data.get(slug) or {}
        if not isinstance(country_map, dict):
            return []
        product_map = country_map.get(product) or {}
        if not isinstance(product_map, dict):
            return []
        result: list[OperatorQuote] = []
        for op_name, op_meta in product_map.items():
            if not isinstance(op_meta, dict):
                continue
            cost = parse_number(op_meta.get("cost"))
            count = parse_integer(op_meta.get("count"))
            rate = parse_number(op_meta.get("rate"))
            note = f"rate={rate}%" if rate is not None else ""
            result.append(
                OperatorQuote(operator=op_name, label=op_name, price=cost, count=count, note=note)
            )
        # 价格升序、库存降序
        return sorted(result, key=lambda r: ((r.price if r.price is not None else 1e9), -(r.count or 0)))

    # 为了兼容 authorization_flow 里 enrich_countries_with_api 的调用，
    # 即使 5sim 国家无数字 ID，也提供一个 resolve_openai_service 空壳。
    def resolve_openai_service(self, configured: str = "") -> str:
        if configured and configured.lower() not in {"auto", "dr"}:
            return configured
        return DEFAULT_SERVICE

    # ------------- public API: activation -------------
    def get_number(
        self,
        service: str = DEFAULT_SERVICE,
        country: Any = "any",
        *,
        operator: str = "any",
        max_retries: int = 5,
    ) -> SmsActivation:
        """Buy an activation number. country 可以是 slug 字符串或 PhoneCountry。"""
        slug = country if isinstance(country, str) and country else self._country_slug(country)
        product = _normalize_product(service)
        operator_value = (operator or "any").strip() or "any"
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                print(
                    f"[SMS] 请求 5sim 号码: country={slug}, operator={operator_value}, product={product} ({attempt}/{max_retries})",
                    flush=True,
                )
                data = self._request("GET", f"/user/buy/activation/{slug}/{operator_value}/{product}")
            except Exception as exc:
                last_error = exc
                print(f"[SMS] 5sim 请求失败: {exc}，3s 后重试", flush=True)
                time.sleep(3)
                continue

            if isinstance(data, str):
                if attempt < max_retries and data.lower() in {"no free phones", "select operator", "select country"}:
                    print(f"[SMS] 5sim 返回 {data}，3s 后重试", flush=True)
                    time.sleep(3)
                    continue
                raise RuntimeError(f"5sim 获取号码失败: {data}")

            if not isinstance(data, dict):
                raise RuntimeError(f"5sim 获取号码失败: 返回格式异常 {type(data).__name__}")
            order_id = parse_integer(data.get("id"))
            phone_number = str(data.get("phone") or "").strip()
            price = parse_number(data.get("price"))
            if order_id is None or not phone_number:
                raise RuntimeError(f"5sim 获取号码失败: {data}")
            if not phone_number.startswith("+"):
                phone_number = f"+{phone_number}"
            print(
                f"[SMS] 获取号码成功: {phone_number} (order={order_id}, 费用={price if price is not None else '-'})",
                flush=True,
            )
            return SmsActivation(activation_id=order_id, phone_number=phone_number, activation_cost=price)
        if last_error:
            raise last_error
        raise RuntimeError("5sim 获取号码失败（重试耗尽）")

    def mark_ready(self, activation_id: int) -> None:
        """5sim 购买成功后自动进入 RECEIVED 状态，无需显式通知。保留方法以兼容接口。"""
        print(f"[SMS] 5sim order {activation_id} 已自动待收码，无需 mark_ready", flush=True)

    def get_status_detail(self, activation_id: int):
        from .hero_sms_provider import SmsCodeStatus, parse_sms_timestamp

        data = self._request("GET", f"/user/check/{activation_id}")
        if isinstance(data, str):
            return SmsCodeStatus(False, "", None)
        if not isinstance(data, dict):
            return SmsCodeStatus(False, "", None)
        status = str(data.get("status") or "").upper()
        sms = data.get("sms") or []
        if isinstance(sms, list) and sms:
            for entry in reversed(sms):
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("code") or "").strip()
                if not code:
                    continue
                received_at = None
                for key in ("date", "created_at", "createdAt", "time"):
                    received_at = parse_sms_timestamp(entry.get(key))
                    if received_at:
                        break
                return SmsCodeStatus(True, code, received_at)
        if status in {"CANCELED", "BANNED"}:
            raise RuntimeError(f"5sim order {activation_id} 状态={status}")
        return SmsCodeStatus(False, "", None)

    def get_status(self, activation_id: int) -> tuple[bool, str]:
        status = self.get_status_detail(activation_id)
        return status.received, status.code

    def poll_for_code(self, activation_id: int, *, interval: float = 5.0, max_attempts: int = 60) -> str:
        for attempt in range(1, max_attempts + 1):
            print(f"[SMS] 拉取 5sim 验证码: order={activation_id} ({attempt}/{max_attempts})", flush=True)
            received, code = self.get_status(activation_id)
            if received and code:
                print(f"[SMS] 拉取到短信验证码: {code}", flush=True)
                return code
            print(f"[SMS] 暂未收到验证码，{interval:g}s 后继续", flush=True)
            time.sleep(max(1.0, interval))
        self.cancel(activation_id)
        raise TimeoutError(f"5sim 短信验证码超时（等待 {int(interval * max_attempts)} 秒），已取消激活")

    def complete(self, activation_id: int) -> None:
        """Mark order as finished and unblock rating rewards."""
        if activation_id in self._completed:
            return
        try:
            print(f"[SMS] 完成 5sim 激活: order={activation_id}", flush=True)
            self._request("GET", f"/user/finish/{activation_id}")
            self._completed.add(activation_id)
            print("[SMS] 5sim 激活已完成", flush=True)
        except Exception as exc:
            print(f"[SMS] 5sim 完成激活失败: {exc}", flush=True)

    def cancel(self, activation_id: int) -> None:
        if activation_id in self._completed:
            return
        try:
            print(f"[SMS] 取消 5sim 激活: order={activation_id}", flush=True)
            self._request("GET", f"/user/cancel/{activation_id}")
            print("[SMS] 5sim 激活已取消", flush=True)
        except Exception as exc:
            # 若号码已经收到短信，5sim 不允许 cancel，只能 ban
            print(f"[SMS] 5sim 取消失败: {exc}（若已收码可改为 ban）", flush=True)

    def ban(self, activation_id: int) -> None:
        try:
            print(f"[SMS] 标记 5sim 号码不可用: order={activation_id}", flush=True)
            self._request("GET", f"/user/ban/{activation_id}")
        except Exception as exc:
            print(f"[SMS] 5sim ban 失败: {exc}", flush=True)

    # ------------- helpers -------------
    def _country_slug(self, country: Any) -> str:
        if isinstance(country, str):
            text = country.strip()
            # 先检查是否是 ISO 代码
            slug = FIVESIM_ISO_TO_COUNTRY.get(text.upper(), "")
            if slug:
                return slug
            # 否则当作已经是 slug
            return text.lower() or "any"
        iso = str(getattr(country, "iso_code", "") or "").strip().upper()
        slug = FIVESIM_ISO_TO_COUNTRY.get(iso, "")
        if slug:
            return slug
        name = str(getattr(country, "name", "") or "").strip().lower()
        # Fall back to lowercase english name if present
        for en_name in getattr(country, "aliases", ()):
            candidate = str(en_name or "").strip().lower().replace(" ", "")
            if candidate in FIVESIM_COUNTRY_TO_ISO:
                return candidate
        return name or "any"


def _slug_to_synth_id(slug: str) -> int:
    """Produce a stable synthetic integer id from a country slug.

    Chosen to avoid collisions with existing HeroSMS 0..200 range by staying
    above 1_000_000. Only used where upstream interface expects an int.
    """
    value = 0
    for ch in slug.lower():
        value = (value * 31 + ord(ch)) & 0xFFFFFF
    return 1_000_000 + value


def _normalize_product(service: str) -> str:
    text = str(service or "").strip().lower()
    if not text or text in {"auto", "dr", "ot", "openai", "chatgpt"}:
        return FIVESIM_OPENAI_PRODUCT
    return text


def configured_fivesim_countries() -> list[PhoneCountry]:
    """Build PhoneCountry list from the shared DEFAULT_PHONE_COUNTRIES catalog,
    tagged with a synthetic hero_sms_country value so downstream code
    (print_country_price_table etc.) can still render them.
    """
    countries: list[PhoneCountry] = []
    for item in DEFAULT_PHONE_COUNTRIES:
        iso = str(item.get("isoCode") or "").strip().upper()
        dial = str(item.get("dialCode") or "").strip().lstrip("+")
        name = str(item.get("name") or "").strip()
        if not (iso and dial and name):
            continue
        slug = FIVESIM_ISO_TO_COUNTRY.get(iso)
        if not slug:
            continue
        synthetic = _slug_to_synth_id(slug)
        aliases = tuple(str(v).strip() for v in item.get("aliases", []) if str(v).strip())
        countries.append(PhoneCountry(iso, dial, name, synthetic, aliases))
    return countries
