"""Tests for the notification system (Pushover + Discord)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notif_cfg(
    mkbrr_wizard: ModuleType,
    *,
    enabled: bool = True,
    policy: str = "summary",
    pushover_enabled: bool = False,
    pushover_app_token: str = "tok",
    pushover_user_key: str = "key",
    pushover_priority: int = 0,
    pushover_failure_priority: int = 1,
    pushover_device: str = "",
    discord_enabled: bool = False,
    discord_webhook_url: str = "https://discord.com/api/webhooks/test",
    discord_username: str = "mkbrr-wizard",
    discord_avatar_url: str = "",
    discord_color_success: int = 0x2ECC71,
    discord_color_failure: int = 0xE74C3C,
    discord_color_partial: int = 0xF39C12,
    timeout_seconds: int = 10,
) -> Any:
    return mkbrr_wizard.NotificationsCfg(
        enabled=enabled,
        policy=policy,
        pushover=mkbrr_wizard.PushoverCfg(
            enabled=pushover_enabled,
            app_token=pushover_app_token,
            user_key=pushover_user_key,
            priority=pushover_priority,
            failure_priority=pushover_failure_priority,
            device=pushover_device,
        ),
        discord=mkbrr_wizard.DiscordCfg(
            enabled=discord_enabled,
            webhook_url=discord_webhook_url,
            username=discord_username,
            avatar_url=discord_avatar_url,
            color_success=discord_color_success,
            color_failure=discord_color_failure,
            color_partial=discord_color_partial,
        ),
        timeout_seconds=timeout_seconds,
    )


def _make_event(mkbrr_wizard: ModuleType, **kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "event_type": "create",
        "success": True,
        "title": "Torrent Created",
        "details": {"path": "/data/test", "preset": "BTN", "exit_code": 0, "elapsed": 12.5},
    }
    defaults.update(kwargs)
    return mkbrr_wizard.NotifyEvent(**defaults)


# ---------------------------------------------------------------------------
# Config parsing tests
# ---------------------------------------------------------------------------


class TestNotificationsConfigParsing:
    """Test that notifications config is parsed from YAML correctly."""

    def test_missing_notifications_section_uses_defaults(self, mkbrr_wizard: ModuleType) -> None:
        """When notifications is absent, defaults to disabled."""
        yaml_content = "runtime: native\ndocker_support: false\nchown: false\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.notifications.enabled is False
            assert cfg.notifications.policy == "summary"
            assert cfg.notifications.pushover.enabled is False
            assert cfg.notifications.discord.enabled is False
            assert cfg.notifications.timeout_seconds == 10
        finally:
            os.unlink(temp_path)

    def test_full_notifications_config(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
notifications:
  enabled: true
  policy: failures_only
  timeout_seconds: 5
  pushover:
    enabled: true
    app_token: my_app_token
    user_key: my_user_key
    priority: -1
    failure_priority: 2
    device: myphone
  discord:
    enabled: true
    webhook_url: https://discord.com/api/webhooks/123/abc
    username: mybot
    avatar_url: https://example.com/avatar.png
    color_success: 3066993
    color_failure: 15158332
    color_partial: 15965202
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            n = cfg.notifications
            assert n.enabled is True
            assert n.policy == "failures_only"
            assert n.timeout_seconds == 5

            assert n.pushover.enabled is True
            assert n.pushover.app_token == "my_app_token"
            assert n.pushover.user_key == "my_user_key"
            assert n.pushover.priority == -1
            assert n.pushover.failure_priority == 2
            assert n.pushover.device == "myphone"

            assert n.discord.enabled is True
            assert n.discord.webhook_url == "https://discord.com/api/webhooks/123/abc"
            assert n.discord.username == "mybot"
            assert n.discord.avatar_url == "https://example.com/avatar.png"
        finally:
            os.unlink(temp_path)

    def test_env_var_expansion_in_tokens(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
notifications:
  enabled: true
  pushover:
    enabled: true
    app_token: ${TEST_PO_TOKEN}
    user_key: ${TEST_PO_USER}
  discord:
    enabled: true
    webhook_url: ${TEST_DC_WEBHOOK}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            with patch.dict(
                os.environ,
                {
                    "TEST_PO_TOKEN": "expanded_token",
                    "TEST_PO_USER": "expanded_user",
                    "TEST_DC_WEBHOOK": "https://expanded.url/webhook",
                },
            ):
                cfg = mkbrr_wizard.load_config(Path(temp_path))
                assert cfg.notifications.pushover.app_token == "expanded_token"
                assert cfg.notifications.pushover.user_key == "expanded_user"
                assert cfg.notifications.discord.webhook_url == "https://expanded.url/webhook"
        finally:
            os.unlink(temp_path)

    def test_invalid_policy_raises(self, mkbrr_wizard: ModuleType) -> None:
        yaml_content = """\
runtime: native
docker_support: false
chown: false
notifications:
  enabled: true
  policy: invalid_value
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            with pytest.raises(ValueError, match=r"notifications\.policy"):
                mkbrr_wizard.load_config(Path(temp_path))
        finally:
            os.unlink(temp_path)

    def test_hex_color_strings_parsed(self, mkbrr_wizard: ModuleType) -> None:
        """YAML may parse 0x2ECC71 as a string — verify we handle it."""
        yaml_content = """\
runtime: native
docker_support: false
chown: false
notifications:
  enabled: true
  discord:
    enabled: true
    webhook_url: https://test.com
    color_success: "0x00FF00"
    color_failure: "0xFF0000"
    color_partial: "0xFFFF00"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        try:
            cfg = mkbrr_wizard.load_config(Path(temp_path))
            assert cfg.notifications.discord.color_success == 0x00FF00
            assert cfg.notifications.discord.color_failure == 0xFF0000
            assert cfg.notifications.discord.color_partial == 0xFFFF00
        finally:
            os.unlink(temp_path)


# ---------------------------------------------------------------------------
# Format helper tests
# ---------------------------------------------------------------------------


class TestFormatPushoverHtml:
    """Test Pushover HTML formatting."""

    def test_create_success(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(mkbrr_wizard)
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Torrent Created" in html
        assert "/data/test" in html
        assert "BTN" in html
        assert "12.5s" in html
        assert "green" in html

    def test_create_failure(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(
            mkbrr_wizard,
            success=False,
            title="Create Failed",
            details={"path": "/data/test", "exit_code": 1, "elapsed": 5.0},
        )
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Create Failed" in html
        assert "red" in html
        assert "exit 1" in html

    def test_batch_partial(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(
            mkbrr_wizard,
            event_type="batch",
            success=False,
            title="Batch Partial",
            details={
                "succeeded": 3,
                "failed": 2,
                "elapsed": 600.0,
                "result_rows": [
                    (1, "/a", "/a.torrent", 0),
                    (2, "/b", "/b.torrent", 1),
                    (3, "/c", "/c.torrent", 0),
                    (4, "/d", "/d.torrent", 0),
                    (5, "/e", "/e.torrent", 2),
                ],
            },
        )
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Batch Partial" in html
        assert "3" in html  # succeeded
        assert "2" in html  # failed
        assert "10m 0s" in html  # duration
        assert "Job 2" in html
        assert "Job 5" in html

    def test_batch_all_success(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(
            mkbrr_wizard,
            event_type="batch",
            success=True,
            title="Batch Complete",
            details={"succeeded": 5, "failed": 0, "elapsed": 120.0, "result_rows": []},
        )
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Batch Complete" in html
        assert "green" in html

    def test_inspect_event(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(
            mkbrr_wizard,
            event_type="inspect",
            success=True,
            title="Inspect Complete",
            details={"path": "/data/file.torrent", "exit_code": 0, "elapsed": 1.5},
        )
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Inspect Complete" in html

    def test_check_failure(self, mkbrr_wizard: ModuleType) -> None:
        event = _make_event(
            mkbrr_wizard,
            event_type="check",
            success=False,
            title="Check Failed",
            details={"path": "/data/file.torrent", "exit_code": 1, "elapsed": 300.0},
        )
        html = mkbrr_wizard._format_pushover_html(event)
        assert "Check Failed" in html
        assert "red" in html


class TestFormatDiscordEmbed:
    """Test Discord embed formatting."""

    def test_create_success_embed(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(mkbrr_wizard, discord_enabled=True)
        event = _make_event(mkbrr_wizard)
        embed = mkbrr_wizard._format_discord_embed(event, cfg.discord)
        assert embed["title"] == "✅ Torrent Created"
        assert embed["color"] == cfg.discord.color_success
        assert any(f["name"] == "Path" for f in embed["fields"])
        assert any(f["name"] == "Preset" for f in embed["fields"])
        assert "timestamp" in embed

    def test_create_failure_embed(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(mkbrr_wizard, discord_enabled=True)
        event = _make_event(
            mkbrr_wizard,
            success=False,
            title="Create Failed",
            details={"path": "/fail", "exit_code": 1, "elapsed": 2.0},
        )
        embed = mkbrr_wizard._format_discord_embed(event, cfg.discord)
        assert "Failed" in embed["title"]
        assert embed["color"] == cfg.discord.color_failure

    def test_batch_partial_embed(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(mkbrr_wizard, discord_enabled=True)
        event = _make_event(
            mkbrr_wizard,
            event_type="batch",
            success=False,
            title="Batch Partial",
            details={
                "succeeded": 2,
                "failed": 1,
                "elapsed": 60.0,
                "result_rows": [
                    (1, "/a", "/a.torrent", 0),
                    (2, "/b", "/b.torrent", 1),
                    (3, "/c", "/c.torrent", 0),
                ],
            },
        )
        embed = mkbrr_wizard._format_discord_embed(event, cfg.discord)
        assert embed["color"] == cfg.discord.color_partial
        assert any(f["name"] == "Failed Jobs" for f in embed["fields"])


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_seconds(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard._format_duration(5.3) == "5.3s"

    def test_minutes(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard._format_duration(125.0) == "2m 5s"

    def test_hours(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard._format_duration(3661.0) == "1h 1m 1s"


# ---------------------------------------------------------------------------
# NotificationManager policy tests
# ---------------------------------------------------------------------------


class TestNotificationManagerPolicy:
    """Test that the manager respects policy settings."""

    def test_disabled_does_not_start_thread(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(mkbrr_wizard, enabled=False)
        mgr = mkbrr_wizard.NotificationManager(cfg)
        assert mgr._active is False
        assert mgr._thread is None
        mgr.shutdown()

    def test_off_policy_does_not_start_thread(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(mkbrr_wizard, enabled=True, policy="off")
        mgr = mkbrr_wizard.NotificationManager(cfg)
        assert mgr._active is False
        mgr.shutdown()

    def test_no_providers_does_not_start_thread(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard, enabled=True, pushover_enabled=False, discord_enabled=False
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        assert mgr._active is False
        mgr.shutdown()

    def test_failures_only_skips_success(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            policy="failures_only",
            pushover_enabled=True,
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        try:
            # Mock _dispatch to check it's NOT called for success
            mgr._dispatch = AsyncMock()  # type: ignore[method-assign]
            event = _make_event(mkbrr_wizard, success=True)
            mgr.notify(event)
            # Give a tiny window for the coroutine to be scheduled
            time.sleep(0.1)
            # _dispatch should not have been called because success + failures_only
            mgr._dispatch.assert_not_called()
        finally:
            mgr.shutdown()

    def test_failures_only_sends_failures(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            policy="failures_only",
            pushover_enabled=True,
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        try:
            mgr._dispatch = AsyncMock()  # type: ignore[method-assign]
            event = _make_event(mkbrr_wizard, success=False, title="Create Failed")
            mgr.notify(event)
            time.sleep(0.3)
            mgr._dispatch.assert_called_once()
        finally:
            mgr.shutdown()

    def test_summary_sends_both(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            policy="summary",
            pushover_enabled=True,
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        try:
            mgr._dispatch = AsyncMock()  # type: ignore[method-assign]
            event_ok = _make_event(mkbrr_wizard, success=True)
            event_fail = _make_event(mkbrr_wizard, success=False, title="Failed")
            mgr.notify(event_ok)
            mgr.notify(event_fail)
            time.sleep(0.3)
            assert mgr._dispatch.call_count == 2
        finally:
            mgr.shutdown()

    def test_shutdown_drains_pending_notifications(self, mkbrr_wizard: ModuleType) -> None:
        """Ensure shutdown() waits for in-flight notifications instead of dropping them.

        Regression test: previously shutdown() called loop.stop() immediately,
        which silently dropped the last notification (e.g. success summary after
        the final operation).
        """
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            policy="summary",
            pushover_enabled=True,
            pushover_app_token="tok",
            pushover_user_key="usr",
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)

        call_log: list[str] = []

        async def slow_dispatch(event: Any) -> None:
            await asyncio.sleep(0.3)  # simulate HTTP latency
            call_log.append(event.title)

        mgr._dispatch = slow_dispatch  # type: ignore[method-assign]

        # Fire a notification and immediately shut down — the old code would
        # drop it because loop.stop() ran before the coroutine finished.
        event = _make_event(mkbrr_wizard, success=True, title="Final Summary")
        mgr.notify(event)
        mgr.shutdown(timeout=5.0)

        assert (
            "Final Summary" in call_log
        ), "shutdown() must drain pending notifications before stopping the loop"

    def test_notify_ignores_runtimeerror_when_loop_closing(
        self, mkbrr_wizard: ModuleType, monkeypatch: Any
    ) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            policy="summary",
            pushover_enabled=True,
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        try:
            event = _make_event(mkbrr_wizard, success=True)
            assert mgr._active is True

            call_count = {"count": 0}

            def _raise_runtimeerror(*_: Any, **__: Any) -> None:
                call_count["count"] += 1
                raise RuntimeError("Event loop is closed")

            monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _raise_runtimeerror)
            mgr.notify(event)
            assert call_count["count"] == 1
        finally:
            mgr.shutdown()


# ---------------------------------------------------------------------------
# NotificationManager HTTP dispatch tests (mocked)
# ---------------------------------------------------------------------------


class TestNotificationManagerDispatch:
    """Test actual HTTP dispatch with mocked httpx."""

    @pytest.mark.asyncio
    async def test_pushover_payload(self, mkbrr_wizard: ModuleType) -> None:
        """Verify Pushover API receives correct form data."""
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            pushover_enabled=True,
            pushover_app_token="test_token",
            pushover_user_key="test_user",
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        event = _make_event(mkbrr_wizard)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await mgr._send_pushover(event)
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            assert call_kwargs[0][0] == "https://api.pushover.net/1/messages.json"
            data = call_kwargs[1]["data"]
            assert data["token"] == "test_token"
            assert data["user"] == "test_user"
            assert data["html"] == "1"

        mgr.shutdown()

    @pytest.mark.asyncio
    async def test_discord_payload(self, mkbrr_wizard: ModuleType) -> None:
        """Verify Discord webhook receives correct JSON."""
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            discord_enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/test/token",
        )
        mgr = mkbrr_wizard.NotificationManager(cfg)
        event = _make_event(mkbrr_wizard)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            await mgr._send_discord(event)
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            assert call_kwargs[0][0] == "https://discord.com/api/webhooks/test/token"
            json_payload = call_kwargs[1]["json"]
            assert "embeds" in json_payload
            assert len(json_payload["embeds"]) == 1
            assert json_payload["username"] == "mkbrr-wizard"

        mgr.shutdown()


# ---------------------------------------------------------------------------
# _expand_env tests
# ---------------------------------------------------------------------------


class TestExpandEnv:
    def test_expands_env_var(self, mkbrr_wizard: ModuleType) -> None:
        with patch.dict(os.environ, {"MY_TEST_VAR": "hello"}):
            assert mkbrr_wizard._expand_env("${MY_TEST_VAR}") == "hello"

    def test_no_path_normalization(self, mkbrr_wizard: ModuleType) -> None:
        """URLs should not get Path-normalized."""
        url = "https://api.pushover.net/1/messages.json"
        assert mkbrr_wizard._expand_env(url) == url

    def test_empty_string(self, mkbrr_wizard: ModuleType) -> None:
        assert mkbrr_wizard._expand_env("") == ""

    def test_preserves_unset_var(self, mkbrr_wizard: ModuleType) -> None:
        # If an env var is not set, expandvars leaves ${VAR} as-is
        result = mkbrr_wizard._expand_env("${DEFINITELY_NOT_SET_XYZ}")
        assert result == "${DEFINITELY_NOT_SET_XYZ}"


# ---------------------------------------------------------------------------
# Graceful degradation when httpx missing
# ---------------------------------------------------------------------------


class TestHttpxMissing:
    def test_manager_inactive_without_httpx(self, mkbrr_wizard: ModuleType) -> None:
        cfg = _make_notif_cfg(
            mkbrr_wizard,
            enabled=True,
            pushover_enabled=True,
        )
        module = cast(Any, mkbrr_wizard)
        original = module._has_httpx
        try:
            module._has_httpx = False
            mgr = mkbrr_wizard.NotificationManager(cfg)
            assert mgr._active is False
            mgr.shutdown()
        finally:
            module._has_httpx = original
