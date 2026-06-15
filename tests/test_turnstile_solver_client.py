from __future__ import annotations

import pytest

from modules.turnstile_solver_client import TurnstileSolverError, is_local_solver_configured


def test_is_local_solver_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.utils.load_env",
        lambda _: {"TURNSTILE_SOLVER_URL": "http://127.0.0.1:8889"},
    )
    assert is_local_solver_configured() is True


@pytest.mark.asyncio
async def test_solve_turnstile_requires_config(monkeypatch) -> None:
    from modules.turnstile_solver_client import solve_turnstile_via_local_solver

    monkeypatch.setattr("modules.utils.load_env", lambda _: {})
    with pytest.raises(TurnstileSolverError, match="未配置"):
        await solve_turnstile_via_local_solver(page_url="https://x.com", sitekey="0xabc")
