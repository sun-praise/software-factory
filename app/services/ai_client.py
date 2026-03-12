from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.config import get_settings


MAX_CONTEXT_FILES = 8
MAX_FILE_CHARS = 12000
MAX_PROMPT_CHARS = 50000


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str | None = None
    action: str = "write"


@dataclass(frozen=True)
class FixPlan:
    summary: str
    changes: tuple[FileChange, ...] = ()


class AIClientError(RuntimeError):
    pass


class AIConfigError(AIClientError):
    pass


class AIRequestError(AIClientError):
    pass


class AIResponseError(AIClientError):
    pass


def generate_fix(
    *,
    prompt: str,
    workspace_dir: str,
    normalized_review: Mapping[str, Any],
) -> FixPlan:
    settings = get_settings()
    provider = settings.ai_provider.strip().lower()
    request_prompt = _build_request_prompt(prompt, workspace_dir, normalized_review)

    if provider == "anthropic":
        text = _call_anthropic(request_prompt)
    elif provider == "openai":
        text = _call_openai(request_prompt)
    else:
        raise AIConfigError(
            f"unsupported AI_PROVIDER '{settings.ai_provider or 'unset'}'"
        )

    return _parse_fix_plan(text)


def _build_request_prompt(
    prompt: str,
    workspace_dir: str,
    normalized_review: Mapping[str, Any],
) -> str:
    workspace = Path(workspace_dir).expanduser().resolve()
    review_paths = _collect_review_paths(normalized_review)
    sections = [
        "Return strict JSON only.",
        "Schema:",
        '{"summary": "short explanation", "changes": [{"path": "relative/path.py", "action": "write", "content": "full file content"}]}',
        "Rules:",
        "- Use action 'write' to replace a file with the full new contents.",
        "- Use action 'delete' only when the file must be removed.",
        "- Paths must be relative to the repository root.",
        "- Keep changes minimal and limited to the listed review issues.",
        "- If no safe fix is possible, return an empty changes list and explain why in summary.",
        "",
        "Task:",
        prompt.strip(),
    ]

    if review_paths:
        sections.extend(["", "Relevant file snapshots:"])
        for rel_path in review_paths:
            file_text = _read_context_file(workspace, rel_path)
            if file_text is None:
                sections.append(f"--- {rel_path} (missing) ---")
                continue
            sections.extend([f"--- {rel_path} ---", file_text])

    request_prompt = "\n".join(sections).strip()
    if len(request_prompt) > MAX_PROMPT_CHARS:
        return request_prompt[:MAX_PROMPT_CHARS]
    return request_prompt


def _collect_review_paths(normalized_review: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("must_fix", "should_fix"):
        items = normalized_review.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "").strip()
            if path and path not in paths:
                paths.append(path)
            if len(paths) >= MAX_CONTEXT_FILES:
                return paths
    return paths


def _read_context_file(workspace: Path, rel_path: str) -> str | None:
    candidate = (workspace / rel_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    text = candidate.read_text(encoding="utf-8")
    if len(text) > MAX_FILE_CHARS:
        return text[:MAX_FILE_CHARS]
    return text


def _call_anthropic(request_prompt: str) -> str:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AIConfigError("ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic")

    payload = {
        "model": settings.anthropic_model,
        "max_tokens": settings.ai_max_output_tokens,
        "temperature": settings.ai_temperature,
        "messages": [{"role": "user", "content": request_prompt}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    data = _post_json(
        url=f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
        headers=headers,
        payload=payload,
    )
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise AIResponseError("anthropic response missing content blocks")
    text_parts: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
    text = "\n".join(part for part in text_parts if part.strip()).strip()
    if not text:
        raise AIResponseError("anthropic response did not include text output")
    return text


def _call_openai(request_prompt: str) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise AIConfigError("OPENAI_API_KEY is required when AI_PROVIDER=openai")

    payload = {
        "model": settings.openai_model,
        "temperature": settings.ai_temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You produce minimal code fixes and return strict JSON only.",
            },
            {"role": "user", "content": request_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "content-type": "application/json",
    }
    data = _post_json(
        url=f"{settings.openai_base_url.rstrip('/')}/chat/completions",
        headers=headers,
        payload=payload,
    )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIResponseError("openai response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise AIResponseError("openai response missing message")
    text = str(message.get("content") or "").strip()
    if not text:
        raise AIResponseError("openai response did not include content")
    return text


def _post_json(
    url: str, headers: Mapping[str, str], payload: Mapping[str, Any]
) -> dict[str, Any]:
    timeout = httpx.Timeout(get_settings().ai_timeout_seconds)
    try:
        response = httpx.post(
            url, headers=dict(headers), json=dict(payload), timeout=timeout
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text.strip()
        detail = body or str(exc)
        raise AIRequestError(detail) from exc
    except httpx.HTTPError as exc:
        raise AIRequestError(str(exc)) from exc

    data = response.json()
    if not isinstance(data, dict):
        raise AIResponseError("AI response must be a JSON object")
    return data


def _parse_fix_plan(raw_text: str) -> FixPlan:
    payload = _extract_json_object(raw_text)
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise AIResponseError("AI response missing summary")

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise AIResponseError("AI response missing changes list")

    changes: list[FileChange] = []
    for item in raw_changes:
        if not isinstance(item, Mapping):
            raise AIResponseError("AI change entry must be an object")
        path = str(item.get("path") or "").strip()
        action = str(item.get("action") or "write").strip().lower()
        content_value = item.get("content")
        if not path:
            raise AIResponseError("AI change entry missing path")
        if action not in {"write", "delete"}:
            raise AIResponseError(f"unsupported change action '{action}'")
        if action == "write" and not isinstance(content_value, str):
            raise AIResponseError("write change must include string content")
        changes.append(
            FileChange(
                path=path,
                action=action,
                content=content_value if isinstance(content_value, str) else None,
            )
        )
    return FixPlan(summary=summary, changes=tuple(changes))


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                text = candidate
                break
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"failed to parse AI JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AIResponseError("AI response root must be an object")
    return payload
