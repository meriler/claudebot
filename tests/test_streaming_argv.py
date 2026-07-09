"""Tests for SessionManager.build_streaming_argv and one-shot regression."""

from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager


def _mgr(tmp_path: Path) -> SessionManager:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        session_mapping_path=str(tmp_path / "session_mapping.json"),
    )
    return SessionManager(settings)


def test_streaming_argv_fresh_has_input_format_no_resume(tmp_path: Path) -> None:
    argv = _mgr(tmp_path).build_streaming_argv(None, mode="free")
    assert "--input-format" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--output-format" in argv  # inherited from the shared base
    assert "--resume" not in argv
    assert argv[-1] == "-p"  # required by --input-format; prompt comes via stdin
    # No prompt argument: nothing after -p.
    assert argv.count("-p") == 1


def test_streaming_argv_resume_includes_session(tmp_path: Path) -> None:
    argv = _mgr(tmp_path).build_streaming_argv("sid-123", mode="free")
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sid-123"
    assert argv[-1] == "-p"


def test_one_shot_prompt_goes_to_stdin_not_argv(tmp_path: Path) -> None:
    # S7: the prompt must NOT be in argv (world-readable via ps/procfs) — it
    # goes to stdin. argv ends with -p and no prompt argument.
    argv, stdin_text = _mgr(tmp_path)._build_command("hello", None, mode="free")
    assert "--input-format" not in argv
    assert argv[-1] == "-p"
    assert "hello" not in " ".join(argv)  # prompt not leaked into argv
    assert stdin_text.endswith("hello")  # mode prompt + tg context + prompt via stdin


def test_streaming_argv_pins_model_before_prompt_marker(tmp_path: Path) -> None:
    argv = _mgr(tmp_path).build_streaming_argv("sid-123", mode="free", model="claude-opus-4-8")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    # --model must precede the trailing -p stdin marker.
    assert argv.index("--model") < argv.index("-p")
    assert argv[-1] == "-p"


def test_streaming_argv_no_model_when_unset(tmp_path: Path) -> None:
    argv = _mgr(tmp_path).build_streaming_argv("sid-123", mode="free")
    assert "--model" not in argv


def test_one_shot_resume_pins_model(tmp_path: Path) -> None:
    argv, stdin_text = _mgr(tmp_path)._build_command(
        "hello", "sid-123", mode="free", model="claude-sonnet-5"
    )
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[-1] == "-p"  # prompt via stdin, argv ends at -p
    assert stdin_text == "hello"  # resume sends the raw prompt (no mode prefix)
    assert argv.index("--model") < argv.index("--resume")


def test_one_shot_fresh_pins_model(tmp_path: Path) -> None:
    argv, stdin_text = _mgr(tmp_path)._build_command(
        "hi", None, mode="free", model="claude-opus-4-8"
    )
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[-1] == "-p"
    assert stdin_text.endswith("hi")


def test_one_shot_no_model_when_unset(tmp_path: Path) -> None:
    argv, _stdin = _mgr(tmp_path)._build_command("hi", None, mode="free")
    assert "--model" not in argv


def test_base_shared_between_oneshot_and_streaming(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    base = mgr._base_command_argv(mode="free")
    streaming = mgr.build_streaming_argv(None, mode="free")
    # Every base flag is present in the streaming argv (shared assembly).
    assert base == streaming[: len(base)]
