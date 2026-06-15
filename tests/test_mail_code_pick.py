from __future__ import annotations

from datetime import datetime, timezone

from modules.mail_provider import _pick_newest_code


def test_pick_newest_code_accepts_excluded_when_received_after_since_floor() -> None:
    anchor = datetime(2026, 6, 1, 18, 9, 16, tzinfo=timezone.utc)
    received = datetime(2026, 6, 1, 18, 9, 20, tzinfo=timezone.utc)
    candidates = [(received, "211017")]
    exclude = {"211017"}

    picked = _pick_newest_code(candidates, exclude, since_floor=anchor)

    assert picked == "211017"


def test_pick_newest_code_skips_excluded_when_too_old() -> None:
    anchor = datetime(2026, 6, 1, 18, 9, 16, tzinfo=timezone.utc)
    received = datetime(2026, 6, 1, 17, 0, 0, tzinfo=timezone.utc)
    candidates = [(received, "211017")]
    exclude = {"211017"}

    picked = _pick_newest_code(candidates, exclude, since_floor=anchor)

    assert picked is None
