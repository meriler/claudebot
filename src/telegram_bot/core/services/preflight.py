"""Startup preflight checks for the bot.

Two modes:
- ``run_startup_preflight()`` — called from ``__main__.py`` before the dispatcher.
  Performs writable filesystem checks. On failure, the bot exits with a clear
  error message (no polling started, no half-broken state).
- ``run_health_checks()`` — read-only variant for the ``/health`` slash command.
  Same checks but without side effects (no temp files), safe for repeated calls.

The Claude Code CLI is currently required in both modes: ``_needs_claude``
always returns True, so its checks run on every deployment. Relaxing this for
pure Codex-only deployments is a TODO.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.topic_config import TopicConfig

logger = logging.getLogger(__name__)

MIN_CLAUDE_VERSION = (2, 1, 130)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class PreflightError(RuntimeError):
    """Raised when a startup preflight check fails."""


def _parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _needs_claude(topic_config: TopicConfig | None) -> bool:
    """Whether the Claude Code CLI checks must run. Currently always True.

    Conservative default for multi-instance deployments. Relaxing this for
    pure Codex-only deployments is a TODO; until then Claude CLI is required.
    """
    return True


def _check_vault(settings: Settings, *, writable: bool) -> CheckResult:
    if not settings.vault:
        # VAULT is optional: only file-saving skills need it. An explicitly
        # set but broken path still fails below — the user clearly wants it.
        return CheckResult(
            "vault",
            True,
            "not set (optional; set VAULT=/path for file-saving skills)",
        )
    p = Path(settings.vault)
    if not p.is_dir():
        return CheckResult(
            "vault",
            False,
            f"VAULT path does not exist or is not a directory: {p}",
        )
    if not writable:
        return CheckResult("vault", True, f"{p} (read-only check)")
    try:
        with tempfile.NamedTemporaryFile(dir=p, prefix=".preflight-", delete=True):
            pass
    except OSError as e:
        return CheckResult("vault", False, f"VAULT not writable: {p} ({e})")
    return CheckResult("vault", True, str(p))


def _check_tmp(*, writable: bool) -> CheckResult:
    tmp = Path(tempfile.gettempdir())
    if not tmp.is_dir():
        return CheckResult("tmp", False, f"tempdir does not exist: {tmp}")
    if not writable:
        return CheckResult("tmp", True, f"{tmp} (read-only check)")
    try:
        with tempfile.NamedTemporaryFile(prefix=".preflight-", delete=True):
            pass
    except OSError as e:
        return CheckResult("tmp", False, f"tempdir not writable: {tmp} ({e})")
    return CheckResult("tmp", True, str(tmp))


def _check_bot_token(settings: Settings) -> CheckResult:
    if not settings.telegram_bot_token:
        return CheckResult(
            "bot_token",
            False,
            "TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather and add to .env.",
        )
    return CheckResult("bot_token", True, "set")


def _check_claude(
    claude_runner: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
) -> CheckResult:
    """Check Claude Code CLI is installed, modern enough, and onboarded."""
    binary = shutil.which("claude")
    if binary is None:
        return CheckResult(
            "claude_cli",
            False,
            "claude not in PATH. Install Claude Code: https://claude.com/claude-code",
        )
    try:
        result = claude_runner([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        return CheckResult("claude_cli", False, f"`claude --version` failed: {e}")
    version = _parse_version(result.stdout)
    if version is None:
        return CheckResult(
            "claude_cli", False, f"could not parse `claude --version`: {result.stdout!r}"
        )
    if version < MIN_CLAUDE_VERSION:
        return CheckResult(
            "claude_cli",
            False,
            f"Claude Code version {'.'.join(map(str, version))} is too old. "
            f"Minimum: {'.'.join(map(str, MIN_CLAUDE_VERSION))}. Run `claude update`.",
        )
    return CheckResult("claude_cli", True, f"v{'.'.join(map(str, version))} at {binary}")


def _check_claude_onboarding() -> CheckResult:
    """Check that CC onboarding (text style, etc.) has been completed.

    CC 2.1.140+ shows interactive theme prompts at first run that block tmux
    sessions until the user clicks through them.
    """
    path = Path.home() / ".claude.json"
    if not path.exists():
        return CheckResult(
            "claude_onboarding",
            False,
            f"Claude Code config not found at {path}. "
            "Run `claude` interactively once to complete onboarding.",
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult("claude_onboarding", False, f"Could not read {path}: {e}")
    if data.get("hasCompletedOnboarding") is not True:
        return CheckResult(
            "claude_onboarding",
            False,
            "Claude Code onboarding not completed. "
            "Run `claude` interactively (theme selection screen) and exit with /exit.",
        )
    return CheckResult("claude_onboarding", True, "completed")


def _all_checks(
    settings: Settings, topic_config: TopicConfig | None, *, writable: bool
) -> list[CheckResult]:
    results = [
        _check_bot_token(settings),
        _check_vault(settings, writable=writable),
        _check_tmp(writable=writable),
    ]
    if _needs_claude(topic_config):
        results.append(_check_claude())
        results.append(_check_claude_onboarding())
    return results


def run_startup_preflight(settings: Settings, topic_config: TopicConfig | None) -> None:
    """Run all checks with writable side-effects. Raise PreflightError on first failure."""
    results = _all_checks(settings, topic_config, writable=True)
    for r in results:
        if r.ok:
            logger.info("preflight: ✅ %s: %s", r.name, r.detail)
        else:
            logger.error("preflight: ❌ %s: %s", r.name, r.detail)
    failures = [r for r in results if not r.ok]
    if failures:
        msg = "Preflight failed:\n" + "\n".join(f"  ❌ {r.name}: {r.detail}" for r in failures)
        raise PreflightError(msg)


def run_health_checks(settings: Settings, topic_config: TopicConfig | None) -> list[CheckResult]:
    """Read-only version for /health. No side effects."""
    return _all_checks(settings, topic_config, writable=False)
