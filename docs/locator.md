# Router CLI locator

Consumers must not import `../deep-model-router` or a personal
`~/.claude/skills/model-router` symlink.

Order:

1. `DEEP_MODEL_ROUTER_CLI` if it is an executable `route_task.py`
2. `$DEEP_MODEL_ROUTER_ROOT/skills/model-router/scripts/route_task.py`
3. Claude cache `~/.claude/plugins/cache/**/deep-model-router/<ver>/.../route_task.py`
4. Codex cache `~/.codex/plugins/**/deep-model-router/**/.../route_task.py`

Missing → treat as router unavailable (consumer §11.3).
Python reference: `skills/model-router/scripts/locate_router.py`.
Node consumers copy the same order; they do not import the Python file.
