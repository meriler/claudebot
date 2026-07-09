"""S2 (audit 2026-07-02): the bot MCP server must not send files from sensitive
locations, and must reject paths outside the allowed roots.

Loads the MCP server module the same way it runs in production (src on path).
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "mcp-servers" / "bot"))
sys.path.insert(0, str(_ROOT / "src"))

import server  # noqa: E402


def test_legit_temp_file_is_allowed(tmp_path: Path) -> None:
    # tmp_path lives under the system temp dir → an allowed root.
    f = tmp_path / "image.png"
    f.write_bytes(b"x")
    resolved, err = server._resolve_file_path(str(f))
    assert err is None
    assert resolved == f


def test_sensitive_path_blocked(tmp_path: Path, monkeypatch) -> None:
    # Simulate a credential store by marking tmp_path's parent sensitive.
    secret = tmp_path / "secrets.env"
    secret.write_text("TOKEN=abc")
    monkeypatch.setattr(server, "_sensitive_roots", lambda: [tmp_path])
    resolved, err = server._resolve_file_path(str(secret))
    assert resolved is None
    assert err is not None and "denied" in err


def test_symlink_into_sensitive_is_blocked(tmp_path: Path, monkeypatch) -> None:
    # A symlink inside an allowed root pointing at a sensitive target must be
    # caught by resolving the link before the deny check.
    secret_dir = tmp_path / "secretstore"
    secret_dir.mkdir()
    target = secret_dir / "token"
    target.write_text("s3cr3t")
    link = tmp_path / "innocent.txt"
    link.symlink_to(target)
    monkeypatch.setattr(server, "_sensitive_roots", lambda: [secret_dir])
    resolved, err = server._resolve_file_path(str(link))
    assert resolved is None
    assert err is not None


def test_path_outside_allowed_roots_blocked(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")
    # Shrink the allowed roots to somewhere that does NOT contain tmp_path.
    monkeypatch.setattr(server, "_allowed_roots", lambda: [Path("/nonexistent-root")])
    monkeypatch.setattr(server, "_sensitive_roots", list)
    resolved, err = server._resolve_file_path(str(f))
    assert resolved is None
    assert err is not None and "outside the allowed" in err


def test_missing_file_reports_not_found(tmp_path: Path) -> None:
    resolved, err = server._resolve_file_path(str(tmp_path / "nope.png"))
    assert resolved is None
    assert "not found" in err


def test_empty_path_rejected() -> None:
    resolved, err = server._resolve_file_path("")
    assert resolved is None
    assert err is not None


def test_allowlist_env_extension(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_FILE_ALLOWLIST", str(tmp_path))
    roots = server._allowed_roots()
    assert tmp_path.resolve() in roots


@pytest.mark.parametrize("subdir", [".config", ".claude", ".ssh"])
def test_home_credential_dirs_are_sensitive(subdir: str) -> None:
    roots = server._sensitive_roots()
    assert (Path.home() / subdir).resolve() in [r.resolve() for r in roots]
