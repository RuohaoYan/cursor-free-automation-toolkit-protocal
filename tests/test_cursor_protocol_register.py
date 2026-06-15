from __future__ import annotations

import pytest

from modules.cursor_protocol_register import (
    CURSOR_WORKOS_CLIENT_ID,
    CursorAuthContext,
    extract_action_ids,
    format_probe_summary,
    _parse_auth_session_id,
    _signup_url_with_context,
)


def test_parse_auth_session_id_from_query() -> None:
    url = (
        "https://authenticator.cursor.sh/?client_id=x"
        "&authorization_session_id=01KT4B8P26WR57P3RS6SFEWVAX"
    )
    assert _parse_auth_session_id(url) == "01KT4B8P26WR57P3RS6SFEWVAX"


def test_extract_action_ids_finds_hashes() -> None:
    html = 'headers: {"Next-Action": "a1b2c3d4e5f6789012345678901234567890abcd"}'
    ids = extract_action_ids(html)
    assert "a1b2c3d4e5f6789012345678901234567890abcd" in ids


def test_signup_url_with_context_includes_session() -> None:
    ctx = CursorAuthContext(
        client_id=CURSOR_WORKOS_CLIENT_ID,
        redirect_uri="https://cursor.com/api/auth/callback",
        state="abc",
        authorization_session_id="01TEST",
        final_url="",
        bootstrap_url="",
    )
    url = _signup_url_with_context(ctx)
    assert "authorization_session_id=01TEST" in url
    assert "sign-up" in url


def test_format_probe_summary_cf() -> None:
    from modules.cursor_protocol_register import CursorProtocolProbe

    probe = CursorProtocolProbe(
        direct_signup_status=403,
        direct_signup_cf=True,
        auth_context=CursorAuthContext(
            client_id=CURSOR_WORKOS_CLIENT_ID,
            redirect_uri="https://cursor.com/api/auth/callback",
            state="",
            authorization_session_id="",
            final_url="https://authenticator.cursor.sh/",
            bootstrap_url="https://authenticator.cursor.sh/bootstrap",
            cookies={"__cf_bm": "x"},
            cf_blocked=True,
            http_status=403,
        ),
        authenticated_fetch_ok=False,
        authenticated_status=0,
    )
    text = format_probe_summary(probe)
    assert "403" in text
    assert "Cloudflare" in text
    assert "cf_blocked=True" in text
