"""Opt-in Autobrowse tool-call recorder.

This module is deliberately fail-open and disabled by default. When explicitly
enabled, it records allowlisted browser/web tool calls into the same
``autobrowse/traces/<task>/run-NNN`` workspace shape used by the
``hermes-autobrowse`` skillifier flow.

Activation options, in precedence order:

- ``HERMES_AUTOBROWSE_RECORD=1``
- ``autobrowse.recording.enabled: true`` in config.yaml

The recorder never writes raw tool results unless ``include_result_preview`` is
explicitly enabled. Arguments and previews are redacted before disk writes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from utils import env_var_enabled

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|set-cookie|token|secret|password|passwd|pwd|client[_-]?secret|refresh[_-]?token|access[_-]?token)"
)
SENSITIVE_TEXT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\n]+"),
    re.compile(r"(?i)(set-cookie\s*:\s*)[^\n]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd|client[_-]?secret|refresh[_-]?token|access[_-]?token)\s*[:=]\s*)[\"']?[^\"'\s,}]+"
    ),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{20,})\b"),
]

DEFAULT_TOOL_ALLOWLIST = [
    "browser_*",
    "web_search",
    "web_extract",
    "mcp_scrapling_*",
    "mcp_playwright_browser_*",
    "mcp_chrome_devtools_*",
]


@dataclass(frozen=True)
class RecorderSettings:
    workspace: Path
    task: str
    run: str
    tools: tuple[str, ...]
    include_result_preview: bool
    max_result_preview_chars: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kebab(value: str, fallback: str = "autobrowse-task") -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:64].strip("-") or fallback


def _redact_text(text: str) -> str:
    redacted = text.replace("\r", "")
    for pattern in SENSITIVE_TEXT_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            key_s = str(key)
            out[key_s] = "[REDACTED]" if SENSITIVE_KEY_RE.search(key_s) else _redact_value(inner)
        return out
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _preview(value: Any, max_chars: int) -> str:
    value = _redact_value(value)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    text = _redact_text(text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _load_config_recording() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        autobrowse = cfg.get("autobrowse") if isinstance(cfg, dict) else None
        if not isinstance(autobrowse, dict):
            return {}
        recording = autobrowse.get("recording")
        return recording if isinstance(recording, dict) else {}
    except Exception:
        return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default


def _as_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_tool_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, Iterable):
        raw = [str(part).strip() for part in value]
    else:
        raw = []
    tools = tuple(part for part in raw if part)
    return tools or tuple(DEFAULT_TOOL_ALLOWLIST)


def _settings(task_id: str = "", session_id: str = "") -> RecorderSettings | None:
    cfg = _load_config_recording()

    env_flag = os.getenv("HERMES_AUTOBROWSE_RECORD")
    enabled = env_var_enabled("HERMES_AUTOBROWSE_RECORD") if env_flag is not None else _as_bool(cfg.get("enabled"), False)
    if not enabled:
        return None

    task = os.getenv("HERMES_AUTOBROWSE_TASK") or str(cfg.get("task") or "")
    if not task:
        if _as_bool(cfg.get("derive_task_from_session"), False):
            task = task_id or session_id
        else:
            # Explicit task names prevent accidental global capture.
            return None

    workspace_raw = os.getenv("HERMES_AUTOBROWSE_WORKSPACE") or str(
        cfg.get("workspace") or (get_hermes_home() / "autobrowse")
    )
    run = os.getenv("HERMES_AUTOBROWSE_RUN") or str(cfg.get("run") or "run-001")
    include_preview = _as_bool(
        os.getenv("HERMES_AUTOBROWSE_INCLUDE_RESULT_PREVIEW")
        if os.getenv("HERMES_AUTOBROWSE_INCLUDE_RESULT_PREVIEW") is not None
        else cfg.get("include_result_preview"),
        False,
    )
    max_preview = _as_int(
        os.getenv("HERMES_AUTOBROWSE_MAX_RESULT_CHARS") or cfg.get("max_result_preview_chars"),
        2000,
        minimum=0,
        maximum=20000,
    )
    tools = _parse_tool_list(os.getenv("HERMES_AUTOBROWSE_TOOLS") or cfg.get("tools"))

    return RecorderSettings(
        workspace=Path(workspace_raw).expanduser().resolve(),
        task=_kebab(task),
        run=_kebab(run, fallback="run-001"),
        tools=tools,
        include_result_preview=include_preview,
        max_result_preview_chars=max_preview,
    )


def _tool_allowed(tool_name: str, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        if selector.endswith("*") and tool_name.startswith(selector[:-1]):
            return True
        if selector == tool_name:
            return True
    return False


def _ensure_workspace(settings: RecorderSettings, task_id: str, session_id: str) -> Path:
    task_dir = settings.workspace / "tasks" / settings.task
    run_dir = settings.workspace / "traces" / settings.task / settings.run
    reports_dir = settings.workspace / "reports"
    task_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    task_md = task_dir / "task.md"
    if not task_md.exists():
        task_md.write_text(
            f"# Task: {settings.task.replace('-', ' ').title()}\n\n"
            "## Objective\nAutobrowse recording started from Hermes tool-call hook. Curate this before publishing.\n\n"
            "## URL / Entry Point\nSee trace events.\n\n"
            "## Expected Output\nReturn structured JSON matching the curated workflow.\n\n"
            "## Acceptance Criteria\n- Workflow is reproducible without relying on accidental UI state.\n"
            "- No secrets, cookies, or client data are stored in traces.\n",
            encoding="utf-8",
        )

    strategy_md = task_dir / "strategy.md"
    if not strategy_md.exists():
        strategy_md.write_text(
            f"# Strategy: {settings.task}\n\n"
            "## Current Best Path\nReview `traces/<task>/<run>/events.jsonl`, then replace noisy tool calls with a deterministic fast path.\n\n"
            "## Known Selectors / APIs / Shortcuts\n- TBD\n\n"
            "## Timing Rules\n- TBD\n\n"
            "## Failure Modes\n- TBD\n\n"
            "## Iteration Notes\n",
            encoding="utf-8",
        )

    session_path = run_dir / "session.json"
    if not session_path.exists():
        session_path.write_text(
            json.dumps(
                {
                    "task": settings.task,
                    "run": settings.run,
                    "created_at": _now_iso(),
                    "status": "recording",
                    "task_id": task_id,
                    "session_id": session_id,
                    "source": "hermes-autobrowse-tool-hook",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    for name in ("events.jsonl", "commands.log"):
        path = run_dir / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    return run_dir


def _append_event(run_dir: Path, event: dict[str, Any]) -> None:
    clean = _redact_value(event)
    events_path = run_dir / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")

    line = (
        f"[{clean.get('timestamp')}] {clean.get('status')} {clean.get('tool')}"
        f" task_id={clean.get('task_id', '')} duration_ms={clean.get('duration_ms', '')}"
    )
    args = clean.get("args")
    if args:
        line += f" args={_preview(args, 1000)}"
    with (run_dir / "commands.log").open("a", encoding="utf-8") as handle:
        handle.write(_redact_text(line).strip() + "\n")


def maybe_record_tool_call(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int | None = None,
) -> None:
    """Record one allowlisted tool call if Autobrowse recording is enabled.

    Never raises: the caller is the agent's tool dispatcher, so recorder failures
    must not affect the user-visible tool result.
    """
    try:
        settings = _settings(task_id=task_id, session_id=session_id)
        if settings is None or not _tool_allowed(tool_name, settings.tools):
            return
        run_dir = _ensure_workspace(settings, task_id, session_id)
        status = "error" if isinstance(result, str) and '"error"' in result[:200].lower() else "ok"
        event: dict[str, Any] = {
            "timestamp": _now_iso(),
            "tool": tool_name,
            "status": status,
            "args": args or {},
            "task_id": task_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "duration_ms": duration_ms,
        }
        if settings.include_result_preview and settings.max_result_preview_chars > 0:
            event["result_preview"] = _preview(result, settings.max_result_preview_chars)
        _append_event(run_dir, event)
    except Exception:
        # Fail open. Tool dispatch must never fail because tracing failed.
        return
