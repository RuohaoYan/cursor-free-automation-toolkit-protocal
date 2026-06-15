from modules.proxy_pool import ProxyPool
from modules.residential_proxy import (
    ResidentialProxyConfig,
    build_residential_proxy_password,
    build_residential_proxy_url,
    load_residential_proxy_config,
)


def test_build_password_with_region_and_attempt_session():
    config = ResidentialProxyConfig(
        enabled=True,
        server="gate.kookeey.info",
        port=1000,
        proxy_type="http",
        username="7522228-b23b5580",
        password="0259b3d8",
        region="GB_England",
        session_duration="1m",
        session_mode="attempt",
    )
    password = build_residential_proxy_password(config, worker_id=2, pick_id=7)
    assert password == "0259b3d8-GB_England-7-1m"


def test_build_url_encodes_credentials():
    config = ResidentialProxyConfig(
        enabled=True,
        server="gate.kookeey.info",
        port=1000,
        proxy_type="http",
        username="7522228-b23b5580",
        password="0259b3d8",
        region="GB_England",
        session_duration="1m",
        session_mode="fixed",
        fixed_session="11985184",
    )
    url = build_residential_proxy_url(config)
    assert url.startswith("http://7522228-b23b5580:")
    assert url.endswith("@gate.kookeey.info:1000")
    assert "0259b3d8-GB_England-11985184-1m" in url


def test_password_template_with_session_placeholder():
    config = ResidentialProxyConfig(
        enabled=True,
        server="gate.kookeey.info",
        port=1000,
        proxy_type="http",
        username="user",
        password="0259b3d8-GB_England-{session}-1m",
        session_mode="fixed",
        fixed_session="11985184",
    )
    assert build_residential_proxy_password(config) == "0259b3d8-GB_England-11985184-1m"


def test_build_url_with_upstream_uses_local_chain(monkeypatch):
    config = ResidentialProxyConfig(
        enabled=True,
        server="gate.kookeey.info",
        port=1000,
        proxy_type="http",
        username="7522228-b23b5580",
        password="0259b3d8",
        region="GB_England",
        session_duration="1m",
        session_mode="attempt",
        upstream="http://127.0.0.1:7897",
    )

    def fake_chain(upstream, gateway, *, cache_key):
        assert upstream == "http://127.0.0.1:7897"
        assert gateway.host == "gate.kookeey.info"
        assert gateway.password == "0259b3d8-GB_England-3-1m"
        return "http://127.0.0.1:18080"

    monkeypatch.setattr(
        "modules.proxy_chain_forwarder.get_chained_local_proxy_url",
        fake_chain,
    )
    url = build_residential_proxy_url(config, worker_id=1, pick_id=3)
    assert url == "http://127.0.0.1:18080"


def test_resolve_upstream_from_proxy_file(tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("http://127.0.0.1:7897\n", encoding="utf-8")
    from modules.residential_proxy import _resolve_upstream

    env = {
        "USE_PROXY": "true",
        "PROXY_FILE": str(proxy_file),
    }
    assert _resolve_upstream(env) == "http://127.0.0.1:7897"


def test_load_residential_proxy_config_from_env():
    env = {
        "RESIDENTIAL_PROXY_ENABLED": "true",
        "RESIDENTIAL_PROXY_SERVER": "gate.kookeey.info",
        "RESIDENTIAL_PROXY_PORT": "1000",
        "RESIDENTIAL_PROXY_TYPE": "http",
        "RESIDENTIAL_PROXY_USERNAME": "7522228-b23b5580",
        "RESIDENTIAL_PROXY_PASSWORD": "0259b3d8",
        "RESIDENTIAL_PROXY_REGION": "GB_England",
        "RESIDENTIAL_PROXY_SESSION_DURATION": "1m",
        "RESIDENTIAL_PROXY_UPSTREAM": "http://127.0.0.1:7897",
    }
    config = load_residential_proxy_config(env)
    assert config is not None
    assert config.enabled is True
    assert config.region == "GB_England"
    assert config.upstream == "http://127.0.0.1:7897"


def test_proxy_pool_uses_residential_when_enabled(tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("# empty\n", encoding="utf-8")
    residential = ResidentialProxyConfig(
        enabled=True,
        server="gate.kookeey.info",
        port=1000,
        proxy_type="http",
        username="7522228-b23b5580",
        password="0259b3d8",
        region="GB_England",
        session_duration="1m",
        session_mode="attempt",
    )
    pool = ProxyPool(proxy_file, residential=residential)
    assert pool.count() == 1
    picked = pool.pick(3)
    assert picked is not None
    assert "gate.kookeey.info:1000" in picked
    assert "0259b3d8-GB_England-3-1m" in picked
