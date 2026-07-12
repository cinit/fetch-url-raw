"""CLI / HTTP mode argument handling tests."""

from __future__ import annotations

import pytest

from fetch_url_raw import server


def test_build_parser_defaults():
    parser = server.build_parser()
    args = parser.parse_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.path is None
    assert args.stateless_http is False
    assert args.allow_remote is False
    assert args.allow_private_network is False


def test_build_parser_http_flags():
    parser = server.build_parser()
    args = parser.parse_args(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--path",
            "/mcp",
            "--stateless-http",
            "--allow-remote",
            "--allow-private-network",
            "--log-level",
            "DEBUG",
        ]
    )
    assert args.transport == "streamable-http"
    assert args.host == "0.0.0.0"
    assert args.port == 9001
    assert args.path == "/mcp"
    assert args.stateless_http is True
    assert args.allow_remote is True
    assert args.allow_private_network is True
    assert args.log_level == "DEBUG"


def test_validate_bind_args_rejects_bad_port():
    with pytest.raises(SystemExit):
        server._validate_bind_args("127.0.0.1", 0)
    with pytest.raises(SystemExit):
        server._validate_bind_args("127.0.0.1", 70000)
    with pytest.raises(SystemExit):
        server._validate_bind_args("", 8000)


def test_configure_http_settings_localhost():
    server.configure_http_settings(
        host="127.0.0.1",
        port=8123,
        transport="streamable-http",
        path="/custom-mcp",
        stateless_http=True,
        allow_remote=False,
        log_level="WARNING",
    )
    assert server.mcp.settings.host == "127.0.0.1"
    assert server.mcp.settings.port == 8123
    assert server.mcp.settings.streamable_http_path == "/custom-mcp"
    assert server.mcp.settings.stateless_http is True
    assert server.mcp.settings.log_level == "WARNING"
    assert server.mcp.settings.transport_security.enable_dns_rebinding_protection is True


def test_configure_http_settings_sse_path():
    server.configure_http_settings(
        host="127.0.0.1",
        port=8124,
        transport="sse",
        path="/events",
        stateless_http=False,
        allow_remote=False,
        log_level="INFO",
    )
    assert server.mcp.settings.sse_path == "/events"
    assert server.mcp.settings.port == 8124


def test_configure_http_settings_path_must_start_with_slash():
    with pytest.raises(SystemExit):
        server.configure_http_settings(
            host="127.0.0.1",
            port=8000,
            transport="streamable-http",
            path="mcp",
            stateless_http=False,
            allow_remote=False,
            log_level="INFO",
        )


def test_configure_http_settings_remote_disables_rebinding_protection():
    server.configure_http_settings(
        host="0.0.0.0",
        port=8000,
        transport="streamable-http",
        path=None,
        stateless_http=False,
        allow_remote=True,
        log_level="INFO",
    )
    assert server.mcp.settings.host == "0.0.0.0"
    assert server.mcp.settings.transport_security.enable_dns_rebinding_protection is False


def test_main_configures_private_network_flag(monkeypatch):
    from fetch_url_raw import config as runtime_config
    from fetch_url_raw import server

    runtime_config.configure(allow_private_network=False)
    called = {}

    def fake_run(transport="stdio"):
        called["transport"] = transport
        called["allow"] = runtime_config.get_allow_private_network()

    monkeypatch.setattr(server.mcp, "run", fake_run)
    server.main(["--allow-private-network"])
    assert called["transport"] == "stdio"
    assert called["allow"] is True
    runtime_config.configure(allow_private_network=False)
