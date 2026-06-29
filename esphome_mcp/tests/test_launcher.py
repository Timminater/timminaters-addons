import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import launcher


def test_generates_and_reuses_persistent_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    token_path = tmp_path / "mcp_auth_token"

    generated, generated_source = launcher._resolve_auth_token({}, token_path)
    stored, stored_source = launcher._resolve_auth_token({}, token_path)

    assert len(generated) >= 48
    assert generated_source == "generated"
    assert stored == generated
    assert stored_source == "stored"
    assert token_path.read_text(encoding="utf-8").strip() == generated


def test_configured_token_overrides_and_updates_persisted_token(tmp_path: Path):
    token_path = tmp_path / "mcp_auth_token"
    token_path.write_text("old-token-that-is-long-enough", encoding="utf-8")

    token, source = launcher._resolve_auth_token(
        {"mcp_auth_token": "new-token-that-is-long-enough"},
        token_path,
    )

    assert token == "new-token-that-is-long-enough"
    assert source == "configured"
    assert token_path.read_text(encoding="utf-8").strip() == token


def test_rejects_short_configured_token(tmp_path: Path):
    with pytest.raises(SystemExit, match="at least 16 characters"):
        launcher._resolve_auth_token({"mcp_auth_token": "too-short"}, tmp_path / "token")


def test_requires_app_data_volume_for_automatic_generation(tmp_path: Path):
    missing_parent = tmp_path / "missing" / "mcp_auth_token"

    with pytest.raises(SystemExit, match="/data volume"):
        launcher._resolve_auth_token({}, missing_parent)


def test_main_logs_same_token_at_every_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    options_path = tmp_path / "options.json"
    options_path.write_text(
        '{"esphome_dashboard_url": "http://esphome:6052"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "OPTIONS_PATH", options_path)
    monkeypatch.setattr(launcher, "TOKEN_PATH", tmp_path / "mcp_auth_token")
    monkeypatch.setattr(launcher.os, "execvp", lambda *_args: None)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

    launcher.main()
    first_log = capsys.readouterr().out
    first_token = first_log.split("bearer token (generated): ", 1)[1].splitlines()[0]

    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    launcher.main()
    second_log = capsys.readouterr().out

    assert f"bearer token (stored): {first_token}" in second_log
