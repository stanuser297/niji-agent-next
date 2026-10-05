def todo_read(ctx: dict) -> str:
    todos = (ctx.get("todos") or {}).get("items", [])
    if not todos:
        return "[no todos yet]"
    by_id = {item.get("id"): item for item in todos if isinstance(item, dict)}
    lines = []
    for i, item in enumerate(todos, 1):
        mark = {"pending": " ", "in_progress": ">", "completed": "x", "blocked": "!"}.get(
            item.get("status"), "?")
        line = f"{mark} {i}. {item.get('content', '')}"
        dependencies = item.get("depends_on", [])
        waiting = [dependency for dependency in dependencies
                   if by_id.get(dependency, {}).get("status") != "completed"]
        if dependencies:
            line += " (depends on: " + ", ".join(dependencies) + ")"
        if waiting:
            line += " (waiting for: " + ", ".join(waiting) + ")"
        criteria = item.get("acceptance_criteria")
        if isinstance(criteria, str) and criteria:
            line += "\n  Check: " + criteria
        evidence = item.get("evidence")
        if isinstance(evidence, str) and evidence:
            line += "\n  Reported evidence (agent-reported, not independently attested): " + evidence
        lines.append(line)
    return "\n".join(lines)


def todo_write(todos: list, activeForm: str = "", ctx: dict = None) -> str:
    from ..planning import normalize_plan, save_plan, validate_approved_plan_progress
    ctx = ctx or {}
    agent = ctx.get("agent")
    approved = getattr(agent, "approved_plan", None) if agent is not None else None
    if approved is not None:
        previous = getattr(agent, "todos", {}).get("items", [])
        plan = validate_approved_plan_progress(todos, approved, previous)
    else:
        plan = normalize_plan(todos)
    # Save before mutating in-memory state so an I/O error cannot report a
    # checklist update that was not durably recorded.
    if agent is not None:
        save_plan(agent.session_id, plan)
    state = ctx.get("todos")
    if isinstance(state, dict):
        state["items"] = plan
    if agent is not None:
        agent.todos = state if isinstance(state, dict) else {"items": plan}
        callback = getattr(agent, "plan_callback", None)
        if callback:
            try:
                callback(plan, activeForm)
            except Exception:
                pass
    return f"[ok] plan saved: {len(plan)} steps ({activeForm or 'n/a'})"


def task(prompt: str, role: str = "coder", ctx: dict = None) -> str:
    """Spawn a bounded, role-scoped subagent with a fresh project context."""
    from ..agent import Agent
    from . import PLAN_SUBAGENT_TOOLS, READ_ONLY_SUBAGENT_TOOLS, SUBAGENT_TOOLS
    ctx = ctx or {}
    parent = ctx.get("agent")
    depth = ctx.get("depth", 0)
    if parent is None:
        return "[error] no parent agent"
    if depth >= 2:
        return "[error] subagent depth limit reached (max 2)"
    roles = {
        "explore": (READ_ONLY_SUBAGENT_TOOLS,
                    "Explore only: inspect and report evidence; do not edit files or run commands."),
        "plan": (PLAN_SUBAGENT_TOOLS,
                 "Plan only: return steps, risks, and checks; do not edit files or run commands."),
        "coder": (SUBAGENT_TOOLS,
                  "Implement only the bounded subtask; inspect the diff and run focused checks."),
    }
    if role not in roles:
        return "[error] role must be explore, plan, or coder"
    allowed_tools, role_note = roles[role]
    sub = Agent(
        provider_cfg=parent.provider_cfg,
        approval=parent.approval,
        max_turns=min(parent.max_turns, 15),
        max_tool_calls=min(parent.max_tool_calls, 12),
        max_tool_calls_per_turn=min(parent.max_tool_calls_per_turn, 4),
        verbose=False,
        depth=depth + 1,
        mcp_clients=[],
        allowed_tools=allowed_tools,
        workspace=getattr(parent, "workspace", None),
    )
    # Parent /undo can also reverse a subagent's file changes; the same guarded
    # snapshot stack prevents a child from creating an invisible edit trail.
    sub.file_change_history = parent.file_change_history
    sub._file_change_lock = parent._file_change_lock
    sub.approval_callback = parent.approval_callback
    sub.activity_callback = lambda event: parent._record_activity(
        event.get("level", "INFO"), "Subagent: " + event.get("message", ""))
    result = sub.chat(f"[{role} subtask] {role_note}\n\n{prompt}")
    return f"[subagent role={role} report]\n" + str(result)[:12000]


def skill_read(name: str, ctx: dict = None) -> str:
    """Load an installed, name-addressed workflow; arbitrary paths are never accepted."""
    from ..instructions import read_skill
    agent = (ctx or {}).get("agent")
    return read_skill(name, getattr(agent, "skills", {}))


def memory_read() -> str:
    from ..config import MEMORY_FILE
    if not MEMORY_FILE.exists():
        return "[memory empty]"
    return MEMORY_FILE.read_text(errors="replace")[:20000]


def memory_write(note: str) -> str:
    from ..config import MEMORY_FILE
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(MEMORY_FILE, "a") as f:
        f.write(note.rstrip() + "\n")
    try:
        MEMORY_FILE.parent.chmod(0o700)
        MEMORY_FILE.chmod(0o600)
    except OSError:
        pass
    return "[ok] saved to long-term memory"
