import json
from pathlib import Path

import model_tools


def _dispatch_with_mock(monkeypatch, tool_name="browser_navigate", args=None, result='{"ok": true}'):
    from tools.registry import registry

    monkeypatch.setattr(registry, "dispatch", lambda name, call_args, **kw: result)
    monkeypatch.setattr(model_tools, "_READ_SEARCH_TOOLS", frozenset())
    return model_tools.handle_function_call(
        tool_name,
        args or {"url": "https://example.com/"},
        task_id="task-123",
        session_id="session-456",
        tool_call_id="call-789",
        skip_pre_tool_call_hook=True,
    )


def test_autobrowse_recorder_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_AUTOBROWSE_RECORD", raising=False)
    monkeypatch.delenv("HERMES_AUTOBROWSE_TASK", raising=False)
    monkeypatch.delenv("HERMES_AUTOBROWSE_WORKSPACE", raising=False)

    out = _dispatch_with_mock(monkeypatch)

    assert json.loads(out) == {"ok": True}
    assert not (tmp_path / "hermes-home" / "autobrowse").exists()


def test_autobrowse_recorder_records_allowlisted_tool_and_redacts(monkeypatch, tmp_path):
    workspace = tmp_path / "ab"
    secret = "SECRET_TOKEN_SHOULD_NOT_SURVIVE"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_AUTOBROWSE_RECORD", "1")
    monkeypatch.setenv("HERMES_AUTOBROWSE_TASK", "hn-top")
    monkeypatch.setenv("HERMES_AUTOBROWSE_WORKSPACE", str(workspace))

    out = _dispatch_with_mock(
        monkeypatch,
        args={
            "url": "https://news.ycombinator.com/",
            "headers": {"Authorization": f"Bearer {secret}"},
            "token": secret,
        },
        result=f'{{"ok": true, "text": "result mentions {secret}"}}',
    )

    assert json.loads(out)["ok"] is True
    run_dir = workspace / "traces" / "hn-top" / "run-001"
    events_path = run_dir / "events.jsonl"
    commands_path = run_dir / "commands.log"
    session_path = run_dir / "session.json"
    assert events_path.exists()
    assert commands_path.exists()
    assert session_path.exists()

    events_text = events_path.read_text()
    commands_text = commands_path.read_text()
    assert secret not in events_text
    assert secret not in commands_text

    event = json.loads(events_text.splitlines()[0])
    assert event["tool"] == "browser_navigate"
    assert event["status"] == "ok"
    assert event["task_id"] == "task-123"
    assert event["session_id"] == "session-456"
    assert event["tool_call_id"] == "call-789"
    assert event["args"]["headers"]["Authorization"] == "[REDACTED]"
    assert event["args"]["token"] == "[REDACTED]"
    # Raw tool results are not recorded unless explicitly enabled.
    assert "result_preview" not in event


def test_autobrowse_recorder_can_be_enabled_from_config(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    workspace = tmp_path / "cfg-ab"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "autobrowse:\n"
        "  recording:\n"
        "    enabled: true\n"
        "    task: cfg-task\n"
        f"    workspace: {workspace}\n"
        "    tools: ['mcp_scrapling_*']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_AUTOBROWSE_RECORD", raising=False)
    monkeypatch.delenv("HERMES_AUTOBROWSE_TASK", raising=False)
    monkeypatch.delenv("HERMES_AUTOBROWSE_WORKSPACE", raising=False)

    out = _dispatch_with_mock(monkeypatch, tool_name="mcp_scrapling_get", args={"url": "https://example.com/"})

    assert json.loads(out) == {"ok": True}
    event_path = workspace / "traces" / "cfg-task" / "run-001" / "events.jsonl"
    assert event_path.exists()
    assert json.loads(event_path.read_text().splitlines()[0])["tool"] == "mcp_scrapling_get"


def test_autobrowse_recorder_ignores_unallowlisted_tools(monkeypatch, tmp_path):
    workspace = tmp_path / "ab"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_AUTOBROWSE_RECORD", "1")
    monkeypatch.setenv("HERMES_AUTOBROWSE_TASK", "hn-top")
    monkeypatch.setenv("HERMES_AUTOBROWSE_WORKSPACE", str(workspace))

    out = _dispatch_with_mock(monkeypatch, tool_name="write_file", args={"path": "x", "content": "y"})

    assert json.loads(out) == {"ok": True}
    assert not (workspace / "traces" / "hn-top").exists()
