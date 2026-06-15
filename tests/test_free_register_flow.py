from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from modules import free_register, storage
from modules.free_browser_flow import FreeBrowserFlow
from modules.storage import MailAccount


class FakePage:
    url = "https://chatgpt.com/"


class FakeCodePage:
    url = "https://auth.openai.com/"

    def __init__(self) -> None:
        self.filled_code = ""
        self.waits: list[int] = []

    async def evaluate(self, script: str, *args: object) -> dict[str, object]:
        self.filled_code = str(args[0])
        return {"filled": True, "mode": "single", "count": 1}

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


class FakeRouteErrorPage:
    url = "https://auth.openai.com/error"

    def __init__(self, text: str) -> None:
        self.text = text
        self.reloads = 0

    async def evaluate(self, script: str, *args: object) -> str:
        return self.text

    async def reload(self, *args: object, **kwargs: object) -> None:
        self.reloads += 1
        self.text = ""


class FakeEmptyLocator:
    @property
    def first(self) -> "FakeEmptyLocator":
        return self

    async def count(self) -> int:
        return 0


class FakeStateDetectPage:
    frames: list[object] = []
    main_frame = object()

    def __init__(self, text: str, url: str = "https://auth.openai.com/") -> None:
        self.text = text
        self.url = url

    async def title(self) -> str:
        return ""

    async def evaluate(self, script: str, *args: object) -> object:
        if args:
            return False
        if "innerText" in script:
            return self.text
        return False

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def locator(self, selector: str) -> FakeEmptyLocator:
        return FakeEmptyLocator()


class FakeCompletePage:
    url = "https://chatgpt.com/"

    def __init__(self, text: str) -> None:
        self.text = text

    async def evaluate(self, script: str, *args: object) -> str:
        return self.text


class FakeSmokeKeyboard:
    def __init__(self, page: "FakeSmokePage") -> None:
        self.page = page

    async def press(self, key: str) -> None:
        if key == "Enter":
            self.page.sent = True


class FakeSmokeLocator:
    def __init__(self, page: "FakeSmokePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeSmokeLocator":
        return self

    async def count(self) -> int:
        if self.selector == "#prompt-textarea" and self.page.waits >= 1:
            return 1
        if "send-button" in self.selector and self.page.filled:
            return 1
        return 0

    async def is_visible(self, *args: object, **kwargs: object) -> bool:
        return await self.count() > 0

    async def click(self, *args: object, **kwargs: object) -> None:
        if "send-button" in self.selector:
            self.page.sent = True

    async def fill(self, value: str, *args: object, **kwargs: object) -> None:
        self.page.filled = value

    async def evaluate(self, script: str, *args: object) -> str:
        return "textarea"


class FakeSmokePage:
    url = "https://chatgpt.com/"

    def __init__(self) -> None:
        self.filled = ""
        self.sent = False
        self.waits = 0
        self.keyboard = FakeSmokeKeyboard(self)

    async def wait_for_load_state(self, *args: object, **kwargs: object) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1

    async def evaluate(self, script: str, *args: object) -> object:
        if args:
            return {"count": 1 if self.sent else 0, "latest": "ok", "stopVisible": False}
        if "querySelectorAll('[data-message-author-role=\"assistant\"]')" in script:
            return 0
        return ""

    def locator(self, selector: str) -> FakeSmokeLocator:
        return FakeSmokeLocator(self, selector)


class FakeDomSmokePage(FakeSmokePage):
    async def evaluate(self, script: str, *args: object) -> object:
        if "chatgptComposerSmokeFill" in script:
            self.filled = str(args[0])
            return {"filled": True, "mode": "contenteditable", "text": self.filled}
        return await super().evaluate(script, *args)

    def locator(self, selector: str) -> FakeEmptyLocator:
        return FakeEmptyLocator()


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.entered = False
        self.exited = False
        FakeSession.instances.append(self)

    async def __aenter__(self) -> "FakeSession":
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited = True

    async def current_page(self) -> FakePage:
        return FakePage()


class FakeStore:
    def __init__(self) -> None:
        self.completed: list[str] = []
        self.returned: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def complete(self, email: str) -> None:
        self.completed.append(email)

    def return_to_pool(self, account: MailAccount) -> None:
        self.returned.append(account.email)

    def save_failed(self, email: str, reason: str) -> None:
        self.failed.append((email, reason))


class FakeAdaptivePage:
    url = "https://auth.openai.com/"


class FakeAdaptiveFlow:
    def __init__(self, states: list[str]) -> None:
        self.page = FakeAdaptivePage()
        self.states = states
        self.actions: list[tuple[str, object]] = []
        self.done = False

    async def goto_chatgpt_entry(self, timeout_ms: int = 60_000) -> None:
        self.actions.append(("goto", timeout_ms))

    async def wait_for_cloudflare(self, timeout_ms: int = 60_000) -> None:
        self.actions.append(("cloudflare", timeout_ms))

    async def detect_free_email_register_state(self) -> str:
        return self.states.pop(0) if self.states else "unknown"

    async def sleep(self, ms: int) -> None:
        self.actions.append(("sleep", ms))

    async def click_signup_email_entry(self) -> bool:
        self.actions.append(("entry", True))
        return True

    async def click_ready_continue(self) -> bool:
        self.actions.append(("ready_continue", True))
        self.done = True
        self.page.url = "https://chatgpt.com/"
        return True

    async def click_passkey_skip(self) -> bool:
        self.actions.append(("passkey_skip", True))
        return True

    async def click_images_upsell_dismiss(self) -> bool:
        self.actions.append(("images_dismiss", True))
        self.done = True
        self.page.url = "https://chatgpt.com/"
        return True

    async def click_usage_reason_skip(self) -> bool:
        self.actions.append(("usage_reason_skip", True))
        return True

    async def send_chatgpt_smoke_message(self, prompt: str = "浣犲ソ") -> str:
        self.actions.append(("chat_smoke", prompt))
        self.done = True
        self.page.url = "https://chatgpt.com/"
        return "浣犲ソ锛屾湁浠€涔堝彲浠ュ府浣狅紵"

    async def submit_email_if_present(self, email: str) -> bool:
        self.actions.append(("email", email))
        return True

    async def has_combined_verification_profile_page(self) -> bool:
        return False

    async def enter_email_verification_code(self, code: str) -> None:
        self.actions.append(("email_code", code))

    async def fill_password_if_shown(self, password: str) -> bool:
        self.actions.append(("password", password))
        return True

    async def fill_about_you_and_submit(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("about_you", kwargs.get("verification_code")))
        self.done = True
        self.page.url = "https://chatgpt.com/"

    async def wait_until_url_leaves(self, keyword: str, timeout_ms: int = 15_000) -> None:
        self.actions.append(("leave", keyword))

    async def click_resend_code(self) -> bool:
        self.actions.append(("resend", True))
        return True


class FakeMailProvider:
    def __init__(self, code: str = "654321") -> None:
        self.code = code
        self.calls = 0

    async def wait_code(self, account: MailAccount, since: object, exclude: set[str]) -> str:
        self.calls += 1
        return self.code


class TimeoutThenCodeMailProvider:
    def __init__(self) -> None:
        self.timeout_sec = 150
        self.calls: list[int] = []

    async def wait_code(self, account: MailAccount, since: object, exclude: set[str]) -> str:
        self.calls.append(self.timeout_sec)
        if len(self.calls) == 1:
            raise TimeoutError("娌℃湁鏂伴獙璇佺爜")
        return "112233"


def _cfg() -> dict[str, Any]:
    return {
        "browser": {"headless": True, "slow_mo": 0, "timeout_ms": 1000},
        "register_profile": {"age_min": 30, "age_max": 30},
        "free_register": {"keep_profile_on_failure": False},
    }


def _patch_common(monkeypatch: Any, tmp_path: Path, store: FakeStore, email: str) -> None:
    account = MailAccount(
        email=email,
        mail_url="query-code",
        raw=f"{email}----query-code",
    )

    async def fake_create_free_mail_account(cfg: dict[str, Any], env: dict[str, str], *, worker_id: int | str = 1):
        return free_register.FreeMailSource(
            source_key="icloud_query",
            account=account,
            mail_cfg={"source": "icloud_query", "code_timeout_sec": 1, "poll_interval_sec": 0},
            store=store,
        )

    def fake_resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return tmp_path / candidate

    FakeSession.instances.clear()
    monkeypatch.setattr(free_register, "BrowserSession", FakeSession)
    monkeypatch.setattr(free_register, "create_free_mail_account", fake_create_free_mail_account)
    monkeypatch.setattr(free_register, "generate_free_profile", lambda age_min, age_max: ("Test User", "30"))
    monkeypatch.setattr(free_register, "generate_free_password", lambda: "LcMockPassword9!")
    monkeypatch.setattr(free_register, "random_birth_date", lambda age: "1996-01-02")
    monkeypatch.setattr(free_register, "load_env", lambda path: {})
    monkeypatch.setattr(free_register, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(storage, "resolve_path", fake_resolve_path)


def test_complete_profile_accepts_skip_about_you() -> None:
    flow = FreeBrowserFlow(FakePage(), "[test]")
    calls: list[str] = []

    async def sms_code() -> str:
        return "123456"

    async def enter_sms_code(code: str) -> None:
        calls.append(f"sms:{code}")

    async def click_submit_button() -> None:
        calls.append("submit")

    async def wait_for_cloudflare(timeout_ms: int) -> None:
        calls.append(f"cloudflare:{timeout_ms}")

    async def fill_password_if_shown(password: str) -> None:
        calls.append(f"password:{password}")

    async def sleep(ms: int) -> None:
        calls.append(f"sleep:{ms}")

    async def fill_about_you_and_submit(*args: object, **kwargs: object) -> None:
        calls.append("about-you")

    flow.enter_sms_code = enter_sms_code  # type: ignore[method-assign]
    flow.click_submit_button = click_submit_button  # type: ignore[method-assign]
    flow.wait_for_cloudflare = wait_for_cloudflare  # type: ignore[method-assign]
    flow.fill_password_if_shown = fill_password_if_shown  # type: ignore[method-assign]
    flow.sleep = sleep  # type: ignore[method-assign]
    flow.fill_about_you_and_submit = fill_about_you_and_submit  # type: ignore[method-assign]

    ok = asyncio.run(flow.complete_profile(object(), sms_code, skip_about_you=True))

    assert ok is True
    assert calls == ["sms:123456", "submit", "cloudflare:30000"]


def test_enter_sms_code_prefers_dom_code_fill() -> None:
    page = FakeCodePage()
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]
    calls: list[int] = []

    async def wait_for_cloudflare(timeout_ms: int) -> None:
        calls.append(timeout_ms)

    flow.wait_for_cloudflare = wait_for_cloudflare  # type: ignore[method-assign]

    asyncio.run(flow.enter_sms_code("336716"))

    assert page.filled_code == "336716"
    assert calls == [90000]
    assert page.waits == [800]


def test_send_chatgpt_smoke_message_waits_for_late_composer() -> None:
    page = FakeSmokePage()
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    async def wait_for_cloudflare(timeout_ms: int) -> None:
        return None

    async def click_button_by_text(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no helper button")

    flow.wait_for_cloudflare = wait_for_cloudflare  # type: ignore[method-assign]
    flow.click_button_by_text = click_button_by_text  # type: ignore[method-assign]

    reply = asyncio.run(flow.send_chatgpt_smoke_message("浣犲ソ", timeout_ms=20_000))

    assert reply == "ok"
    assert page.filled == "浣犲ソ"
    assert page.sent is True
    assert page.waits >= 1


def test_send_chatgpt_smoke_message_uses_prosemirror_dom_fallback() -> None:
    page = FakeDomSmokePage()
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    async def wait_for_cloudflare(timeout_ms: int) -> None:
        return None

    async def click_button_by_text(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no helper button")

    flow.wait_for_cloudflare = wait_for_cloudflare  # type: ignore[method-assign]
    flow.click_button_by_text = click_button_by_text  # type: ignore[method-assign]

    reply = asyncio.run(flow.send_chatgpt_smoke_message("浣犲ソ", timeout_ms=20_000))

    assert reply == "ok"
    assert page.filled == "浣犲ソ"
    assert page.sent is True


def test_auth_route_error_reason_detects_invalid_content_type() -> None:
    page = FakeRouteErrorPage(
        '绯熺硶锛屽嚭閿欎簡锛?Route Error (400 Invalid content type: text/html; charset=UTF-8): '
        '"Invalid content type: text/html; charset=UTF-8" 閲嶈瘯'
    )
    flow = FreeBrowserFlow(page, "[test]")

    reason = asyncio.run(flow.auth_route_error_reason())

    assert reason == "route error"


def test_recover_auth_route_error_clicks_retry() -> None:
    page = FakeRouteErrorPage(
        '绯熺硶锛屽嚭閿欎簡锛?Route Error (400 Invalid content type: text/html; charset=UTF-8): '
        '"Invalid content type: text/html; charset=UTF-8" 閲嶈瘯'
    )
    flow = FreeBrowserFlow(page, "[test]")
    clicked: list[list[str]] = []

    async def find_clickable(candidates: list[str]) -> bool:
        clicked.append(candidates)
        page.text = "Email verification"
        return True

    async def sleep(ms: int) -> None:
        return None

    flow._find_clickable = find_clickable  # type: ignore[method-assign]
    flow.sleep = sleep  # type: ignore[method-assign]

    ok = asyncio.run(flow.recover_auth_route_error(max_attempts=1, wait_ms=100))

    assert ok is True
    assert clicked and "Retry" in clicked[0]
    assert page.reloads == 0


def test_recover_auth_route_error_ignores_normal_page() -> None:
    page = FakeRouteErrorPage("Email verification")
    flow = FreeBrowserFlow(page, "[test]")
    clicked: list[list[str]] = []

    async def find_clickable(candidates: list[str]) -> bool:
        clicked.append(candidates)
        return True

    flow._find_clickable = find_clickable  # type: ignore[method-assign]

    ok = asyncio.run(flow.recover_auth_route_error(max_attempts=1, wait_ms=100))

    assert ok is False
    assert clicked == []


def test_detect_free_email_register_state_detects_passkey_offer() -> None:
    page = FakeStateDetectPage("浣跨敤閫氳瀵嗛挜鍒涘缓璐︽埛\n璺宠繃")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "passkey"


def test_detect_free_email_register_state_detects_images_upsell() -> None:
    page = FakeStateDetectPage("鍏ㄦ柊 ChatGPT Images 2.0 閲嶇鐧诲満\n鏆備笉")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "images_upsell"


def test_detect_free_email_register_state_detects_usage_reason_survey() -> None:
    page = FakeStateDetectPage("鏄粈涔堜績浣夸綘浣跨敤 ChatGPT锛焅n璺宠繃")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "usage_reason"


def test_detect_chatgpt_root_with_register_is_entry() -> None:
    page = FakeStateDetectPage("娆㈣繋浣跨敤 ChatGPT\n娉ㄥ唽", url="https://chatgpt.com/")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "entry"


def test_detect_chatgpt_root_register_modal_with_email_input_is_email() -> None:
    page = FakeStateDetectPage("娆㈣繋浣跨敤 ChatGPT\n娉ㄥ唽", url="https://chatgpt.com/")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    async def fake_find_email_input() -> object:
        return object()

    async def fake_is_probable_auth_input(loc: object) -> tuple[bool, str]:
        return True, "email"

    flow._find_email_input = fake_find_email_input  # type: ignore[method-assign]
    flow._is_probable_auth_input = fake_is_probable_auth_input  # type: ignore[method-assign]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "email"


def test_detect_chatgpt_root_with_upgrade_is_complete_home() -> None:
    page = FakeStateDetectPage("ChatGPT\n鍗囩骇", url="https://chatgpt.com/")
    flow = FreeBrowserFlow(page, "[test]")  # type: ignore[arg-type]

    state = asyncio.run(flow.detect_free_email_register_state())

    assert state == "complete_home"


def test_registration_complete_waits_for_images_upsell_dismiss(monkeypatch: Any) -> None:
    page = FakeCompletePage("鍏ㄦ柊 ChatGPT Images 2.0 閲嶇鐧诲満\n鏆備笉")
    flow = type("Flow", (), {"page": page})()

    async def fake_logged_in_markers(*args: object) -> bool:
        return True

    monkeypatch.setattr(free_register, "chatgpt_logged_in_markers", fake_logged_in_markers)

    complete = asyncio.run(free_register._is_free_registration_complete(flow))  # type: ignore[arg-type]

    assert complete is False


def test_registration_complete_root_register_is_not_complete(monkeypatch: Any) -> None:
    page = FakeCompletePage("娆㈣繋浣跨敤 ChatGPT\n娉ㄥ唽")
    flow = type("Flow", (), {"page": page})()

    async def fake_logged_in_markers(*args: object) -> bool:
        return True

    monkeypatch.setattr(free_register, "chatgpt_logged_in_markers", fake_logged_in_markers)

    complete = asyncio.run(free_register._is_free_registration_complete(flow))  # type: ignore[arg-type]

    assert complete is False


def test_registration_complete_root_upgrade_is_complete(monkeypatch: Any) -> None:
    page = FakeCompletePage("ChatGPT\n鍗囩骇")
    flow = type("Flow", (), {"page": page})()

    async def fake_logged_in_markers(*args: object) -> bool:
        return False

    monkeypatch.setattr(free_register, "chatgpt_logged_in_markers", fake_logged_in_markers)

    complete = asyncio.run(free_register._is_free_registration_complete(flow))  # type: ignore[arg-type]

    assert complete is True


def test_email_only_adaptive_phase_follows_detected_states(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["entry", "email", "email_code", "password", "about_you"])
    mail_provider = FakeMailProvider()
    account = MailAccount(email="free-adaptive@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(
        full_name="Test User",
        age="30",
        password="LcMockPassword9!",
        birth_date="1996-01-02",
    )

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            mail_provider,  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("entry", True) in flow.actions
    assert ("email", "free-adaptive@example.test") in flow.actions
    assert ("email_code", "654321") in flow.actions
    assert ("password", "LcMockPassword9!") in flow.actions
    assert ("about_you", "654321") in flow.actions
    assert mail_provider.calls == 1


def test_email_only_adaptive_skips_passkey_offer(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["passkey", "ready_continue"])
    account = MailAccount(email="passkey@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("passkey_skip", True) in flow.actions


def test_email_only_adaptive_dismisses_images_upsell(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["images_upsell"])
    account = MailAccount(email="images@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("images_dismiss", True) in flow.actions


def test_email_only_adaptive_skips_usage_reason_survey(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["usage_reason", "ready_continue"])
    account = MailAccount(email="usage@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("usage_reason_skip", True) in flow.actions


def test_email_only_adaptive_sends_chat_smoke_message_on_complete_home(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["complete_home"])
    account = MailAccount(email="complete@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("chat_smoke", "浣犲ソ") in flow.actions
    assert flow.done is True


def test_email_only_adaptive_skips_smoke_when_composer_missing_but_logged_in(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["complete_home"])
    account = MailAccount(email="skip-smoke@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def failing_smoke(self: FakeAdaptiveFlow, prompt: str = "浣犲ソ") -> str:
        raise RuntimeError("鏈壘鍒?ChatGPT 杈撳叆妗嗭紝鏃犳硶鍙戦€佹帰娴嬫秷鎭? https://chatgpt.com/")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return True

    flow.send_chatgpt_smoke_message = failing_smoke.__get__(flow, FakeAdaptiveFlow)  # type: ignore[method-assign]
    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("chat_smoke", "浣犲ソ") not in flow.actions


def test_email_only_adaptive_clicks_ready_continue(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["ready_continue"])
    account = MailAccount(email="ready@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    asyncio.run(
        free_register._phase1_email_register_adaptive(
            flow,  # type: ignore[arg-type]
            account,
            FakeMailProvider(),  # type: ignore[arg-type]
            profile,
            "[test]",
        )
    )

    assert ("ready_continue", True) in flow.actions
    assert flow.done is True


def test_email_only_adaptive_stops_on_phone_required(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["phone_required"])
    account = MailAccount(email="phone-required@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    with pytest.raises(free_register.FreeRegisterError, match="鎵嬫満楠岃瘉"):
        asyncio.run(
            free_register._phase1_email_register_adaptive(
                flow,  # type: ignore[arg-type]
                account,
                FakeMailProvider(),  # type: ignore[arg-type]
                profile,
                "[test]",
            )
        )


def test_email_only_adaptive_unknown_state_does_not_guess_click_entry(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow(["unknown"] * 12)
    account = MailAccount(email="unknown@example.test", mail_url="query-code")
    profile = free_register.FreeProfile(full_name="Test User", age="30", password="LcMockPassword9!")

    async def fake_complete(candidate: FakeAdaptiveFlow) -> bool:
        return candidate.done

    monkeypatch.setattr(free_register, "_is_free_registration_complete", fake_complete)

    with pytest.raises(free_register.FreeRegisterError):
        asyncio.run(
            free_register._phase1_email_register_adaptive(
                flow,  # type: ignore[arg-type]
                account,
                FakeMailProvider(),  # type: ignore[arg-type]
                profile,
                "[test]",
            )
        )

    assert ("entry", True) not in flow.actions
    assert [action for action in flow.actions if action[0] == "sleep"] == [("sleep", 1000)] * 11


def test_phase1_email_code_short_wait_resends_before_full_timeout(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow([])
    provider = TimeoutThenCodeMailProvider()
    account = MailAccount(email="resend@example.test", mail_url="")
    monkeypatch.setattr(free_register, "load_env", lambda path: {"FREE_EMAIL_FIRST_CODE_WAIT_SEC": "3"})

    code = asyncio.run(
        free_register._wait_phase1_email_code(
            flow,  # type: ignore[arg-type]
            account,
            provider,  # type: ignore[arg-type]
            object(),
            set(),
            "[test]",
        )
    )

    assert code == "112233"
    assert provider.calls == [3, 150]
    assert flow.actions.count(("resend", True)) == 2


def test_phase1_email_code_clicks_send_before_fetch(monkeypatch: Any) -> None:
    flow = FakeAdaptiveFlow([])
    provider = FakeMailProvider("445566")
    account = MailAccount(email="send-first@example.test", mail_url="")
    monkeypatch.setattr(
        free_register,
        "load_env",
        lambda path: {"FREE_EMAIL_FIRST_CODE_WAIT_SEC": "20", "FREE_EMAIL_CLICK_SEND_CODE_FIRST": "true"},
    )

    code = asyncio.run(
        free_register._wait_phase1_email_code(
            flow,  # type: ignore[arg-type]
            account,
            provider,  # type: ignore[arg-type]
            object(),
            set(),
            "[test]",
        )
    )

    assert code == "445566"
    assert flow.actions[0] == ("resend", True)
    assert provider.calls == 1


def test_run_free_register_many_logs_email_progress(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return tmp_path / candidate

    pool_file = tmp_path / "data" / "hotmail" / "accounts_pool.txt"
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(
        "one@example.test----pw----client----refresh\n"
        "two@example.test----pw----client----refresh\n",
        encoding="utf-8",
    )
    cfg = {
        "browser": {"use_proxy": False, "proxy_file": "data/proxies/proxies.txt"},
        "mail": {"active_source": "hotmail", "source": "hotmail"},
        "mail_sources": {
            "hotmail": {
                "source": "hotmail_graph",
                "accounts_file": "data/hotmail/accounts_pool.txt",
                "raw_pool_file": "data/hotmail/accounts_pool.txt",
            }
        },
        "free_register": {"max_attempt_multiplier": 5},
    }
    logs: list[str] = []

    async def fake_run_once_email(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(free_register, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(storage, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(free_register, "load_env", lambda path: {"FREE_MAIL_SOURCE": "hotmail"})
    monkeypatch.setattr(free_register, "log", logs.append)
    monkeypatch.setattr(free_register, "run_free_register_once_email", fake_run_once_email)

    result = asyncio.run(
        free_register.run_free_register_many(
            cfg,
            count=5,
            workers=1,
            sms_selection=None,
            register_mode="email-only",
        )
    )

    assert result == 1
    assert sum(1 for item in logs if "1/2" in item and "0/5" in item) >= 1
    assert sum(1 for item in logs if "2/2" in item and "1/5" in item) >= 1
    assert not any("3/2" in item for item in logs)
    assert any("2/2" in item for item in logs)


def test_run_free_register_many_retries_same_email_three_times(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return tmp_path / candidate

    pool_file = tmp_path / "data" / "hotmail" / "accounts_pool.txt"
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("retry@example.test----pw----client----refresh\n", encoding="utf-8")
    cfg = {
        "browser": {"use_proxy": False, "proxy_file": "data/proxies/proxies.txt"},
        "mail": {"active_source": "hotmail", "source": "hotmail"},
        "mail_sources": {
            "hotmail": {
                "source": "hotmail_graph",
                "accounts_file": "data/hotmail/accounts_pool.txt",
                "raw_pool_file": "data/hotmail/accounts_pool.txt",
            }
        },
        "free_register": {"max_attempt_multiplier": 5},
    }
    logs: list[str] = []
    calls: list[tuple[str, bool]] = []

    async def fake_run_once_email(*args: object, **kwargs: object) -> bool:
        mail_source = kwargs["mail_source"]
        calls.append((mail_source.account.email, bool(kwargs["finalize_pool_on_failure"])))
        return len(calls) == 3

    monkeypatch.setattr(free_register, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(storage, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(free_register, "load_env", lambda path: {"FREE_MAIL_SOURCE": "hotmail"})
    monkeypatch.setattr(free_register, "log", logs.append)
    monkeypatch.setattr(free_register, "run_free_register_once_email", fake_run_once_email)

    result = asyncio.run(
        free_register.run_free_register_many(
            cfg,
            count=1,
            workers=1,
            sms_selection=None,
            register_mode="email-only",
        )
    )

    assert result == 0
    assert calls == [
        ("retry@example.test", False),
        ("retry@example.test", False),
        ("retry@example.test", True),
    ]
    assert any("1/3" in item for item in logs)
    assert any("2/3" in item for item in logs)
    assert any("3/3" in item for item in logs)


def test_reset_free_pool_claims_on_start_clears_interrupted_claims(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return tmp_path / candidate

    monkeypatch.setattr(free_register, "resolve_path", fake_resolve_path)
    logs: list[str] = []
    monkeypatch.setattr(free_register, "log", logs.append)

    cfg = {
        "mail": {"active_source": "hotmail", "source": "hotmail"},
        "mail_sources": {
            "hotmail": {
                "source": "hotmail_graph",
                "accounts_file": "data/hotmail/accounts_pool.txt",
                "raw_pool_file": "data/hotmail/accounts_pool.txt",
            }
        },
    }
    pool_file = tmp_path / "data" / "hotmail" / "accounts_pool.txt"
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("stale@example.test----pw----client----refresh\n", encoding="utf-8")

    store = free_register._build_free_pool_store("hotmail", cfg["mail_sources"]["hotmail"])
    store.in_progress_file.write_text(
        "2026-05-30 17:48:04\tworker-1\tstale@example.test\tstale@example.test----pw----client----refresh\n",
        encoding="utf-8",
    )

    cleared = free_register.reset_free_pool_claims_on_start(
        cfg,
        {"FREE_MAIL_SOURCE": "hotmail"},
        prefix="[test]",
    )

    assert cleared == 1
    assert store.in_progress_file.read_text(encoding="utf-8") == ""
    assert len(logs) >= 1


def test_reset_free_pool_claims_on_start_can_be_disabled(monkeypatch: Any, tmp_path: Path) -> None:
    def fake_resolve_path(path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return tmp_path / candidate

    monkeypatch.setattr(free_register, "resolve_path", fake_resolve_path)

    cfg = {
        "mail": {"active_source": "hotmail", "source": "hotmail"},
        "mail_sources": {
            "hotmail": {
                "source": "hotmail_graph",
                "accounts_file": "data/hotmail/accounts_pool.txt",
                "raw_pool_file": "data/hotmail/accounts_pool.txt",
            }
        },
    }
    store = free_register._build_free_pool_store("hotmail", cfg["mail_sources"]["hotmail"])
    store.in_progress_file.write_text(
        "2026-05-30 17:48:04\tworker-1\tstale@example.test\tstale@example.test----pw----client----refresh\n",
        encoding="utf-8",
    )

    cleared = free_register.reset_free_pool_claims_on_start(
        cfg,
        {"FREE_MAIL_SOURCE": "hotmail", "FREE_RESET_POOL_CLAIMS_ON_START": "false"},
    )

    assert cleared == 0
    assert "stale@example.test" in store.in_progress_file.read_text(encoding="utf-8")


def test_run_free_register_once_email_only_mocked_success_writes_outputs(monkeypatch: Any, tmp_path: Path) -> None:
    store = FakeStore()
    _patch_common(monkeypatch, tmp_path, store, "free-success@example.test")
    phase_calls: list[tuple[str, str, object | None]] = []

    async def fake_phase1(flow, account, mail_provider, sms_selection, profile, prefix):
        phase_calls.append(("phase1", account.email, sms_selection))
        assert profile.password == "LcMockPassword9!"

    monkeypatch.setattr(free_register, "phase1_email_register", fake_phase1)

    output_root = tmp_path / "out"
    ok = asyncio.run(
        free_register.run_free_register_once_email(
            _cfg(),
            sms_selection=None,
            worker_id=7,
            output_root=output_root,
            require_sms=False,
        )
    )

    assert ok is True
    assert phase_calls == [("phase1", "free-success@example.test", None)]
    assert store.completed == ["free-success@example.test"]
    assert store.returned == []
    assert FakeSession.instances and FakeSession.instances[0].exited is True
    assert FakeSession.instances[0].kwargs["incognito"] is True

    account_text = (output_root / "account.txt").read_text(encoding="utf-8")
    assert "free-success@example.test----LcMockPassword9!----query-code" in account_text
    token_files = list((output_root / "cpa鍙锋睜").glob("codex-*-free.json"))
    assert len(token_files) == 0


def test_run_free_register_once_email_failure_removes_pool_account(monkeypatch: Any, tmp_path: Path) -> None:
    store = FakeStore()
    _patch_common(monkeypatch, tmp_path, store, "free-failure@example.test")

    async def fake_phase1(flow, account, mail_provider, sms_selection, profile, prefix):
        raise RuntimeError("phase1 failed")

    async def fake_save_debug_page(page, prefix: str, name: str) -> None:
        return None

    monkeypatch.setattr(free_register, "phase1_email_register", fake_phase1)
    monkeypatch.setattr(free_register, "save_free_debug_page", fake_save_debug_page)

    output_root = tmp_path / "out"
    ok = asyncio.run(
        free_register.run_free_register_once_email(
            _cfg(),
            sms_selection=None,
            worker_id=3,
            output_root=output_root,
            require_sms=False,
        )
    )

    assert ok is False
    assert store.completed == ["free-failure@example.test"]
    assert store.returned == []
    assert store.failed == [("free-failure@example.test", "RuntimeError: phase1 failed")]
    assert not (output_root / "account.txt").exists()
    assert FakeSession.instances and FakeSession.instances[0].exited is True


def test_normalize_register_mode_accepts_aliases() -> None:
    assert free_register.normalize_register_mode("phone") == "phone"
    assert free_register.normalize_register_mode("mobile") == "phone"
    assert free_register.normalize_register_mode("email") == "email"
    assert free_register.normalize_register_mode("email_only") == "email"
    assert free_register.normalize_register_mode("emailonly") == "email"


def test_normalize_register_mode_rejects_invalid_value() -> None:
    with pytest.raises(free_register.FreeRegisterError, match="invalid register_mode"):
        free_register.normalize_register_mode("invalid")


def test_classify_free_register_error() -> None:
    assert free_register.classify_free_register_error(RuntimeError("mail pool empty")) == "mail_pool_empty"
    assert free_register.classify_free_register_error(RuntimeError("email already registered")) == "email_already_registered"
    assert free_register.classify_free_register_error(RuntimeError("operation timeout")) == "timeout"
    assert free_register.classify_free_register_error(RuntimeError("OAuth callback missing code")) == "oauth_callback_missing_code"
    assert free_register.classify_free_register_error(RuntimeError("random failure")) == "unknown"

