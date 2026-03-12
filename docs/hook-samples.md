# Hook Configuration Samples

This document explains how to use `example_hooks.json` with the local endpoint:

- `POST http://127.0.0.1:8000/hook-events`

## Event Purpose

- `UserPromptSubmit`: sent when a user submits a new prompt.
- `PostToolUse`: sent after a tool succeeds.
- `PostToolUseFailure`: sent after a tool fails.

Current endpoint behavior in this branch: it accepts JSON, reads event type from `x-event-type`, and echoes both values in the response.

## Payload Samples

The app reads JSON body from the hook command and event type from `x-event-type` header.

### UserPromptSubmit

```json
{
  "event": "UserPromptSubmit",
  "session_id": "sess_demo_001",
  "repo": "sun-praise/software-factory",
  "branch": "feat/m2-hook-docs",
  "cwd": "/home/user/your-project",
  "prompt": "Add hook sample documentation",
  "timestamp": "2026-03-12T09:00:00Z"
}
```

### PostToolUse

```json
{
  "event": "PostToolUse",
  "session_id": "sess_demo_001",
  "tool_name": "Edit",
  "tool_call_id": "tool_123",
  "status": "success",
  "duration_ms": 142,
  "timestamp": "2026-03-12T09:00:01Z"
}
```

### PostToolUseFailure

```json
{
  "event": "PostToolUseFailure",
  "session_id": "sess_demo_001",
  "tool_name": "Bash",
  "tool_call_id": "tool_124",
  "status": "failure",
  "error": "command exited with code 1",
  "timestamp": "2026-03-12T09:00:02Z"
}
```

## Local Debug Commands

Start service:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Send sample events with curl:

```bash
curl -i -X POST http://127.0.0.1:8000/hook-events \
  -H 'content-type: application/json' \
  -H 'x-event-type: UserPromptSubmit' \
  -d '{"event":"UserPromptSubmit","session_id":"sess_demo_001","prompt":"Add hook sample documentation"}'

curl -i -X POST http://127.0.0.1:8000/hook-events \
  -H 'content-type: application/json' \
  -H 'x-event-type: PostToolUse' \
  -d '{"event":"PostToolUse","session_id":"sess_demo_001","tool_name":"Edit","status":"success"}'

curl -i -X POST http://127.0.0.1:8000/hook-events \
  -H 'content-type: application/json' \
  -H 'x-event-type: PostToolUseFailure' \
  -d '{"event":"PostToolUseFailure","session_id":"sess_demo_001","tool_name":"Bash","status":"failure"}'
```

Expected response shape:

```json
{
  "ok": true,
  "message": "Hook event received",
  "event_type": "PostToolUse",
  "received": {
    "event": "PostToolUse",
    "session_id": "sess_demo_001",
    "tool_name": "Edit",
    "status": "success"
  }
}
```

If `x-event-type` is missing, `event_type` becomes `"unknown"`.

## Troubleshooting

- `404 Not Found`: verify the endpoint is `/hook-events` and app is running on the same host and port.
- `event_type` is `unknown`: add `x-event-type` header in the hook command.
- Empty `received` payload: ensure `content-type: application/json` is set and payload is valid JSON.
- Hook command works manually but not in runtime: check your hook config path and confirm runner can execute `curl`.
- Unexpected shell quoting errors: use single quotes around JSON in curl examples or move payload to a file.

## Configuration Loading

### File Location

Copy `example_hooks.json` to your project root or OpenEdge config directory:

```bash
cp example_hooks.json ~/.config/opencode/hooks.json
```

Or keep it in your project:

```bash
# Project-level hook config
cp example_hooks.json .opencode/hooks.json
```

### Activating Hooks

1. **Global config**: Place `hooks.json` in `~/.config/opencode/`
2. **Project config**: Place `hooks.json` in `<project>/.opencode/`
3. OpenEdge will automatically load hooks on startup

### Customizing Events

Edit `hooks.json` to add/remove event types:

- `UserPromptSubmit` - Triggered when user submits a prompt
- `PostToolUse` - Triggered after successful tool execution
- `PostToolUseFailure` - Triggered after failed tool execution

Modify the `matcher` field to filter specific tools (regex supported).
