"""Delegation mode: Planner -> Executor -> Critic, each an independent process.

The lead roles (planner, executor, critic) run on the lead model (opus, medium
effort) in separate `claude -p` processes with their own system prompts. The
executor never edits code: it only decides which subtasks to dispatch and how
to word them. Workers (coder on sonnet, researcher on haiku) are separate
processes started by this module. The critic re-checks the real repository
state and either approves or sends fix subtasks back through the executor.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..codex_runner import execution_prompt, read_task_context, stop_process_group
from ..config import Config
from ..project_index import ProjectInfo
from ..task_store import TaskRecord, TaskStore
from .orchestrator import list_of_strings, parse_json_object, read_task_diff

ROLE_PLANNER = "planner"
ROLE_EXECUTOR = "executor"
ROLE_CRITIC = "critic"
ROLE_CODER = "coder"
ROLE_RESEARCHER = "researcher"
WORKER_ROLES = (ROLE_CODER, ROLE_RESEARCHER)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

# A subtask may be dispatched this many times before the executor must give up.
MAX_ATTEMPTS = 2
# Consecutive executor turns without a valid dispatch before the run stops.
MAX_STALLS = 2

PROMPTS_DIR = Path(__file__).with_name("delegation_prompts")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
RESULT_CHARS = 3000


class PlanError(ValueError):
    pass


class DelegationAborted(RuntimeError):
    """The task was canceled while the pipeline was running."""


@dataclass
class Subtask:
    id: str
    role: str
    title: str
    brief: str
    depends_on: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    attempts: int = 0
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    summary: str = ""
    risk: str = "medium"
    risk_reasons: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    subtasks: list[Subtask] = field(default_factory=list)


@dataclass(frozen=True)
class CriticVerdict:
    verdict: str
    summary: str = ""
    verified: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    fix_subtasks: list[dict[str, Any]] = field(default_factory=list)
    effort_fit: str = ""


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    role: str
    model: str
    effort: str | None
    system_prompt: str
    prompt: str
    cwd: str
    permission_mode: str
    disallowed_tools: tuple[str, ...]
    timeout_seconds: int
    log_dir: Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    text: str = ""
    timed_out: bool = False
    canceled: bool = False


class ProcessRunner(Protocol):
    def __call__(self, spec: ProcessSpec) -> ProcessResult:
        ...


def load_prompt(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def parse_subtasks(
    raw: Any,
    max_count: int,
    known_ids: set[str] | None = None,
    id_prefix: str = "",
) -> list[Subtask]:
    """Validate raw subtask dicts: ids, roles, briefs, dependencies, cycles."""
    known = known_ids or set()
    if not isinstance(raw, list):
        raise PlanError("subtasks must be a list")
    if len(raw) > max_count:
        raise PlanError(f"too many subtasks: {len(raw)} > {max_count}")

    raw_ids: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PlanError("each subtask must be an object")
        raw_id = str(item.get("id") or "").strip()
        if not ID_RE.match(raw_id):
            raise PlanError(f"invalid subtask id: {raw_id!r}")
        if raw_id in raw_ids:
            raise PlanError(f"duplicate subtask id: {raw_id}")
        raw_ids.append(raw_id)
    mapped = {raw_id: f"{id_prefix}{raw_id}" for raw_id in raw_ids}
    for new_id in mapped.values():
        if new_id in known:
            raise PlanError(f"subtask id already exists: {new_id}")

    subtasks: list[Subtask] = []
    for item in raw:
        role = str(item.get("role") or "").strip().lower()
        if role not in WORKER_ROLES:
            raise PlanError(f"unknown role {role!r} in subtask {item.get('id')}")
        brief = str(item.get("brief") or "").strip()
        if not brief:
            raise PlanError(f"subtask {item.get('id')} has an empty brief")
        deps = []
        for dep in list_of_strings(item.get("depends_on")):
            dep = dep.strip()
            if dep in mapped:
                deps.append(mapped[dep])
            elif dep in known:
                deps.append(dep)
            else:
                raise PlanError(f"subtask {item.get('id')} depends on unknown id {dep!r}")
        raw_id = str(item["id"]).strip()
        subtasks.append(
            Subtask(
                id=mapped[raw_id],
                role=role,
                title=str(item.get("title") or "").strip() or raw_id,
                brief=brief,
                depends_on=deps,
                files=[normalize_path(path) for path in list_of_strings(item.get("files"))],
                acceptance=list_of_strings(item.get("acceptance")),
            )
        )

    ensure_acyclic(subtasks)
    return subtasks


def ensure_acyclic(subtasks: list[Subtask]) -> None:
    local = {subtask.id: subtask for subtask in subtasks}
    remaining = {sid: {dep for dep in st.depends_on if dep in local} for sid, st in local.items()}
    while remaining:
        ready = [sid for sid, deps in remaining.items() if not deps]
        if not ready:
            raise PlanError("dependency cycle between subtasks: " + ", ".join(sorted(remaining)))
        for sid in ready:
            del remaining[sid]
        for deps in remaining.values():
            deps.difference_update(ready)


def parse_plan(text: str, max_count: int) -> Plan:
    payload = parse_json_object(text)
    if not payload:
        raise PlanError("planner reply is not a JSON object")
    plan = Plan(
        summary=str(payload.get("summary") or "").strip(),
        risk=str(payload.get("risk") or "medium").strip().lower(),
        risk_reasons=list_of_strings(payload.get("risk_reasons")),
        questions=list_of_strings(payload.get("questions")),
    )
    if plan.questions:
        return plan
    plan.subtasks = parse_subtasks(payload.get("subtasks"), max_count)
    if not plan.subtasks:
        raise PlanError("plan has no subtasks")
    return plan


def parse_critic(text: str) -> CriticVerdict:
    payload = parse_json_object(text)
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"approve", "request_changes", "needs_human"}:
        raise PlanError("critic reply has no valid verdict")
    issues = [item for item in payload.get("issues") or [] if isinstance(item, dict)]
    fixes = [item for item in payload.get("fix_subtasks") or [] if isinstance(item, dict)]
    return CriticVerdict(
        verdict=verdict,
        summary=str(payload.get("summary") or "").strip(),
        verified=list_of_strings(payload.get("verified")),
        unverified=list_of_strings(payload.get("unverified")),
        issues=issues,
        fix_subtasks=fixes,
        effort_fit=str(payload.get("effort_fit") or "").strip().lower(),
    )


def normalize_path(path: str) -> str:
    return posixpath.normpath(path.strip()) if path.strip() else ""


def files_conflict(left: Subtask, right: Subtask) -> bool:
    """Coders that may touch the same path must not run in parallel.

    An empty file list means "unknown", so it conflicts with every other coder.
    """
    if left.role != ROLE_CODER or right.role != ROLE_CODER:
        return False
    if not left.files or not right.files:
        return True
    for a in left.files:
        for b in right.files:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def clip(value: str, limit: int = RESULT_CHARS) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "\n..."


def parse_worker_result(result: ProcessResult) -> tuple[str, dict[str, Any]]:
    """Map a worker process outcome to (subtask status, result payload)."""
    if result.canceled:
        raise DelegationAborted("canceled")
    if result.timed_out:
        return STATUS_FAILED, {"status": STATUS_FAILED, "summary": "Worker timed out."}
    if result.returncode != 0:
        return STATUS_FAILED, {
            "status": STATUS_FAILED,
            "summary": f"Worker exited with code {result.returncode}.",
            "output_tail": clip(result.text[-1500:], 1500),
        }
    payload = parse_json_object(result.text)
    status = str(payload.get("status") or "").strip().lower()
    if status not in {STATUS_DONE, STATUS_BLOCKED, STATUS_FAILED}:
        return STATUS_FAILED, {
            "status": STATUS_FAILED,
            "summary": "Worker returned no structured result.",
            "output_tail": clip(result.text[-1500:], 1500),
        }
    return status, payload


class SubprocessRunner:
    """Runs one `claude -p` process, killing it on timeout or task cancel."""

    def __init__(self, config: Config, should_stop: Callable[[], bool]) -> None:
        self.config = config
        self.should_stop = should_stop

    def binary(self) -> str:
        argv = shlex.split(self.config.claude_executor_command)
        return argv[0] if argv else "claude"

    def argv(self, spec: ProcessSpec) -> list[str]:
        argv = [
            self.binary(),
            "-p",
            "--output-format",
            "text",
            "--model",
            spec.model,
            "--permission-mode",
            spec.permission_mode,
            "--append-system-prompt",
            spec.system_prompt,
            "--no-session-persistence",
        ]
        if spec.effort:
            argv.extend(["--effort", spec.effort])
        if spec.disallowed_tools:
            argv.extend(["--disallowedTools", ",".join(spec.disallowed_tools)])
        return argv

    def __call__(self, spec: ProcessSpec) -> ProcessResult:
        if self.should_stop():
            return ProcessResult(returncode=-1, canceled=True)
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        out_path = spec.log_dir / f"{spec.name}.out"
        err_path = spec.log_dir / f"{spec.name}.err"
        (spec.log_dir / f"{spec.name}.prompt.md").write_text(spec.prompt, encoding="utf-8")
        env = os.environ.copy()
        env["CODEX_TELEGRAM_SUPPRESS_STOP_HOOK"] = "1"
        deadline = time.monotonic() + spec.timeout_seconds
        with out_path.open("wb") as out, err_path.open("wb") as err:
            try:
                process = subprocess.Popen(
                    self.argv(spec),
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    cwd=spec.cwd,
                    start_new_session=True,
                    env=env,
                )
            except OSError as exc:
                return ProcessResult(returncode=127, text=f"failed to start: {exc}")
            try:
                assert process.stdin is not None
                process.stdin.write(spec.prompt.encode("utf-8"))
                process.stdin.close()
            except OSError:
                pass
            while True:
                try:
                    returncode = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                canceled = self.should_stop()
                timed_out = time.monotonic() >= deadline
                if canceled or timed_out:
                    stop_process_group(process.pid)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        stop_process_group(process.pid, 9)
                        process.wait()
                    text = out_path.read_text(encoding="utf-8", errors="replace")
                    return ProcessResult(
                        returncode=-1,
                        text=text,
                        timed_out=timed_out and not canceled,
                        canceled=canceled,
                    )
        text = out_path.read_text(encoding="utf-8", errors="replace")
        return ProcessResult(returncode=returncode, text=text)


class DelegationService:
    def __init__(
        self,
        config: Config,
        store: TaskStore,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.process_runner = process_runner
        self._name_lock = threading.Lock()

    def run(
        self,
        task: TaskRecord,
        project: ProjectInfo,
        notify: Callable[[str], None] | None = None,
        append_event: Callable[..., None] | None = None,
    ) -> TaskRecord:
        return _Run(self, task, project, notify, append_event).execute()

    def runner_for(self, task: TaskRecord) -> ProcessRunner:
        if self.process_runner is not None:
            return self.process_runner
        return SubprocessRunner(self.config, lambda: self.is_canceled(task.id))

    def is_canceled(self, task_id: str) -> bool:
        current = self.store.load_task(task_id)
        return current is not None and current.phase == "canceled"

    def unavailable_reason(self) -> str:
        if self.process_runner is not None:
            return ""
        if not self.config.claude_enabled:
            return "Claude provider is disabled (CODEX_CLAUDE_ENABLED=0)."
        try:
            argv = shlex.split(self.config.claude_executor_command)
        except ValueError as exc:
            return f"Claude executor command is not parseable: {exc}"
        if not argv:
            return "Claude executor command is empty."
        if shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            return f"Claude binary not found: {argv[0]}"
        return ""


class _Run:
    """State of one delegated task."""

    def __init__(
        self,
        service: DelegationService,
        task: TaskRecord,
        project: ProjectInfo,
        notify: Callable[[str], None] | None,
        append_event: Callable[..., None] | None,
    ) -> None:
        self.service = service
        self.config = service.config
        self.store = service.store
        self.task = task
        self.project = project
        self._notify = notify
        self._append_event = append_event
        self.runner = service.runner_for(task)
        self.dir = self.store.task_dir(task.id) / "delegation"
        self.subtasks: dict[str, Subtask] = {}
        self.plan = Plan()
        self.executor_report = ""
        self.brief = ""
        self.counter = 0

    # -- helpers -----------------------------------------------------------

    def notify(self, text: str) -> None:
        if self._notify is not None:
            self._notify(text)

    def event(self, agent: str, message: str, data: dict[str, Any] | None = None) -> None:
        if self._append_event is not None:
            self._append_event(self.task.id, "agent_message", agent, message, data)

    def fail(self, error: str, phase: str = "failed") -> TaskRecord:
        self.task.phase = phase
        self.task.error = error
        self.store.save_task(self.task)
        self.save_state()
        return self.task

    def save_state(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "plan": {
                "summary": self.plan.summary,
                "risk": self.plan.risk,
                "risk_reasons": self.plan.risk_reasons,
            },
            "subtasks": [asdict(subtask) for subtask in self.subtasks.values()],
        }
        (self.dir / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def spec(self, role: str, name: str, prompt: str, model: str) -> ProcessSpec:
        cfg = self.config
        permission = "plan"
        disallowed: tuple[str, ...] = EDIT_TOOLS
        if role == ROLE_EXECUTOR:
            # Coordination only: no edits, no shell, no in-process sub-agents.
            permission = "bypassPermissions"
            disallowed = EDIT_TOOLS + ("Bash", "Agent", "Workflow")
        elif role == ROLE_CRITIC:
            # May run tests and read-only git commands, never edit.
            permission = "bypassPermissions"
        elif role == ROLE_CODER:
            permission, disallowed = "bypassPermissions", ()
        lead = role in {ROLE_PLANNER, ROLE_EXECUTOR, ROLE_CRITIC}
        return ProcessSpec(
            name=name,
            role=role,
            model=model,
            effort=cfg.delegate_lead_effort if lead else None,
            system_prompt=load_prompt(role),
            prompt=prompt,
            cwd=self.task.project_path,
            permission_mode=permission,
            disallowed_tools=disallowed,
            timeout_seconds=cfg.delegate_process_timeout_seconds,
            log_dir=self.dir / "logs",
        )

    def models_summary(self) -> dict[str, str]:
        cfg = self.config
        return {
            "planner": cfg.delegate_lead_model,
            "executor": cfg.delegate_lead_model,
            "critic": cfg.delegate_lead_model,
            "coder": cfg.delegate_coder_model,
            "researcher": cfg.delegate_researcher_model,
        }

    def next_name(self, role: str) -> str:
        with self.service._name_lock:
            self.counter += 1
            return f"{self.counter:02d}-{role}"

    def call_lead(self, role: str, prompt: str) -> ProcessResult:
        spec = self.spec(role, self.next_name(role), prompt, self.config.delegate_lead_model)
        result = self.runner(spec)
        if result.canceled:
            raise DelegationAborted("canceled")
        return result

    # -- pipeline ----------------------------------------------------------

    def execute(self) -> TaskRecord:
        reason = self.service.unavailable_reason()
        if reason:
            return self.fail(f"Delegation mode needs Claude: {reason}")
        self.task.phase = "running"
        self.task.returncode = None
        self.task.error = ""
        self.task.orchestrator_enabled = True
        self.task.model_routing = {
            **self.task.model_routing,
            "delegation": {"models": self.models_summary()},
        }
        final_path = self.store.task_dir(self.task.id) / "final.md"
        self.task.final_path = str(final_path)
        self.store.save_task(self.task)
        try:
            return self.pipeline(final_path)
        except DelegationAborted:
            self.save_state()
            return self.task

    def pipeline(self, final_path: Path) -> TaskRecord:
        context = read_task_context(
            self.config.index_dir,
            self.task,
            self.project,
            vault_dir=self.config.obsidian_vault_dir,
        )
        self.brief = execution_prompt(self.task, context)

        plan = self.make_plan()
        if isinstance(plan, str):
            return self.fail(plan)
        if plan.questions:
            self.task.plan_text = "Planner has blocking questions:\n" + "\n".join(
                f"- {item}" for item in plan.questions
            )
            self.notify(self.task.plan_text)
            return self.fail("Planner has blocking questions.", phase="needs_human")
        self.plan = plan
        self.subtasks = {subtask.id: subtask for subtask in plan.subtasks}
        self.save_state()
        self.event("DelegatePlanner", "Plan prepared.", {"summary": plan.summary, "risk": plan.risk})
        self.notify(render_plan(plan))

        error = self.run_executor_loop()
        if error is not None:
            return self.fail(error[1], phase=error[0])

        max_rounds = self.config.delegate_max_critic_rounds
        for round_number in range(1, max_rounds + 1):
            self.notify(f"Critic (round {round_number}/{max_rounds}): проверяю результат.")
            verdict = self.run_critic()
            if isinstance(verdict, str):
                return self.fail(verdict, phase="needs_human")
            self.task.orchestrator_review_rounds = round_number - 1
            self.store.save_task(self.task)
            self.event("DelegateCritic", f"Critic verdict: {verdict.verdict}", asdict(verdict))
            self.notify(f"Critic: {verdict.verdict}. {verdict.summary[:400]}".strip())
            if verdict.verdict == "approve":
                self.write_final(final_path, verdict)
                self.task.phase = "completed"
                self.task.returncode = 0
                self.store.save_task(self.task)
                self.save_state()
                return self.task
            if verdict.verdict == "needs_human" or round_number == max_rounds:
                self.write_final(final_path, verdict)
                text = verdict.summary or "Critic requested human review."
                if verdict.verdict == "request_changes":
                    text = f"Critic still sees issues after {max_rounds} rounds. {text}"
                return self.fail(text, phase="needs_human")
            try:
                fixes = parse_subtasks(
                    verdict.fix_subtasks,
                    self.config.delegate_max_subtasks,
                    known_ids=set(self.subtasks),
                    id_prefix=f"r{round_number}-",
                )
            except PlanError as exc:
                self.write_final(final_path, verdict)
                return self.fail(f"Critic fix subtasks are invalid: {exc}", phase="needs_human")
            if not fixes:
                self.write_final(final_path, verdict)
                return self.fail(
                    "Critic requested changes without fix subtasks.", phase="needs_human"
                )
            for fix in fixes:
                self.subtasks[fix.id] = fix
            self.save_state()
            self.notify(f"Critic: передаю доработку executor ({len(fixes)} подзадач).")
            error = self.run_executor_loop()
            if error is not None:
                return self.fail(error[1], phase=error[0])
        return self.task

    # -- planner -----------------------------------------------------------

    def make_plan(self) -> Plan | str:
        self.notify("Planner: составляю план.")
        prompt = (
            f"Limits: at most {self.config.delegate_max_subtasks} subtasks.\n\n"
            f"Task material:\n{self.brief}"
        )
        feedback = ""
        last_error = ""
        for _attempt in (1, 2):
            result = self.call_lead(ROLE_PLANNER, prompt + feedback)
            if result.timed_out or result.returncode != 0:
                return f"Planner process failed (exit {result.returncode}, timeout={result.timed_out})."
            try:
                return parse_plan(result.text, self.config.delegate_max_subtasks)
            except PlanError as exc:
                last_error = str(exc)
                feedback = (
                    f"\n\nYour previous reply was rejected: {exc}. "
                    "Reply again with one valid JSON object only."
                )
        return f"Planner returned an invalid plan twice: {last_error}"

    # -- executor loop -----------------------------------------------------

    def run_executor_loop(self) -> tuple[str, str] | None:
        """Drive the executor until every subtask is done. Returns (phase, error) on failure."""
        max_turns = self.config.delegate_max_subtasks * 3 + 3
        errors: list[str] = []
        stalls = 0
        for _turn in range(max_turns):
            result = self.call_lead(ROLE_EXECUTOR, self.executor_prompt(errors))
            if result.timed_out or result.returncode != 0:
                return "failed", (
                    f"Executor process failed (exit {result.returncode}, timeout={result.timed_out})."
                )
            payload = parse_json_object(result.text)
            status = str(payload.get("status") or "").strip().lower()
            if status not in {"continue", "done", "blocked"}:
                errors = ["Reply was not a JSON object with a valid status."]
                stalls += 1
                if stalls >= MAX_STALLS:
                    return "failed", "Executor returned invalid replies."
                continue
            report = str(payload.get("final_report") or "").strip()
            if status == "blocked":
                return "needs_human", report or "Executor reported the run is blocked."
            if status == "done":
                open_ids = [st.id for st in self.subtasks.values() if st.status != STATUS_DONE]
                if open_ids:
                    errors = [f"status done, but subtasks are not done: {', '.join(open_ids)}"]
                    stalls += 1
                    if stalls >= MAX_STALLS:
                        return "failed", "Executor declared done with unfinished subtasks."
                    continue
                self.executor_report = report
                self.event("DelegateExecutor", "Executor finished.", {"final_report": report})
                return None

            wave, errors = self.select_wave(payload.get("dispatch"))
            if not wave:
                stalls += 1
                errors = errors or ["Nothing was dispatched but subtasks are still open."]
                if stalls >= MAX_STALLS:
                    return "failed", "Executor made no progress: " + "; ".join(errors)
                continue
            stalls = 0
            self.run_wave(wave)
        return "failed", "Executor exceeded its turn limit."

    def executor_prompt(self, errors: list[str]) -> str:
        state = {
            "limits": {
                "max_parallel": self.config.delegate_max_parallel,
                "max_attempts_per_subtask": MAX_ATTEMPTS,
            },
            "plan_summary": self.plan.summary,
            "risk": self.plan.risk,
            "subtasks": [asdict(st) for st in self.subtasks.values()],
            "dispatch_errors_from_last_turn": errors,
        }
        return (
            f"Original task:\n{clip(self.task.prompt, 6000)}\n\n"
            f"Current state:\n{json.dumps(state, ensure_ascii=False, indent=2)}"
        )

    def select_wave(self, raw: Any) -> tuple[list[Subtask], list[str]]:
        errors: list[str] = []
        wave: list[Subtask] = []
        items = raw if isinstance(raw, list) else []
        for item in items:
            if not isinstance(item, dict):
                errors.append("dispatch entry is not an object")
                continue
            sid = str(item.get("id") or "").strip()
            subtask = self.subtasks.get(sid)
            if subtask is None:
                subtask, error = self.new_fix_subtask(item)
                if error:
                    errors.append(error)
                    continue
            else:
                brief = str(item.get("brief") or "").strip()
                if brief:
                    subtask.brief = brief
            problem = self.dispatch_problem(subtask)
            if problem:
                errors.append(f"{subtask.id}: {problem}")
                continue
            if len(wave) >= self.config.delegate_max_parallel:
                continue
            if any(files_conflict(subtask, other) for other in wave):
                continue
            wave.append(subtask)
        return wave, errors

    def new_fix_subtask(self, item: dict[str, Any]) -> tuple[Subtask | None, str]:
        sid = str(item.get("id") or "").strip()
        if len(self.subtasks) >= self.config.delegate_max_subtasks * 2:
            return None, f"{sid}: subtask limit reached"
        if "role" not in item:
            return None, f"unknown subtask id {sid!r}"
        try:
            created = parse_subtasks([{**item, "depends_on": []}], 1, known_ids=set(self.subtasks))
        except PlanError as exc:
            return None, f"{sid}: {exc}"
        subtask = created[0]
        self.subtasks[subtask.id] = subtask
        return subtask, ""

    def dispatch_problem(self, subtask: Subtask) -> str:
        if subtask.status not in {STATUS_PENDING, STATUS_FAILED}:
            return f"status is {subtask.status}, cannot dispatch"
        if subtask.attempts >= MAX_ATTEMPTS:
            return "attempt limit reached"
        waiting = [dep for dep in subtask.depends_on if self.subtasks[dep].status != STATUS_DONE]
        if waiting:
            return "dependencies not done: " + ", ".join(waiting)
        return ""

    def run_wave(self, wave: list[Subtask]) -> None:
        models = self.models_summary()
        self.notify(
            f"Executor: запускаю воркеров ({len(wave)}): " + ", ".join(st.id for st in wave) + "."
        )
        for subtask in wave:
            subtask.attempts += 1
            self.event(
                f"Worker:{subtask.id}",
                f"Dispatched {subtask.role} ({models[subtask.role]}): {subtask.title}",
                {"files": subtask.files},
            )
        self.save_state()
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = [(subtask, pool.submit(self.run_worker, subtask)) for subtask in wave]
            outcomes = [(subtask, future.result()) for subtask, future in futures]
        for subtask, result in outcomes:
            status, payload = parse_worker_result(result)
            subtask.status = status
            subtask.result = trim_result(payload)
            summary = str(payload.get("summary") or "")[:300]
            self.event(f"Worker:{subtask.id}", f"{status}: {summary}", subtask.result)
            self.notify(f"Worker {subtask.id} ({subtask.role}): {status}. {summary}".strip())
        self.save_state()

    def run_worker(self, subtask: Subtask) -> ProcessResult:
        model = self.models_summary()[subtask.role]
        name = f"{self.next_name(subtask.role)}-{subtask.id}"
        return self.runner(self.spec(subtask.role, name, self.worker_prompt(subtask), model))

    def worker_prompt(self, subtask: Subtask) -> str:
        deps = {dep: self.subtasks[dep].result for dep in subtask.depends_on}
        previous = (
            "\n\nPrevious attempt result (verify the repo state before redoing work):\n"
            f"{json.dumps(subtask.result, ensure_ascii=False)}"
            if subtask.result and subtask.status == STATUS_FAILED
            else ""
        )
        criteria = "\n".join(f"- {item}" for item in subtask.acceptance or ["none given"])
        return (
            f"Project: {self.task.project_name} ({self.task.project_path})\n"
            f"Task summary: {self.plan.summary}\n\n"
            f"Subtask {subtask.id} [{subtask.role}]: {subtask.title}\n\n"
            f"Brief:\n{subtask.brief}\n\n"
            f"Files you may modify: {json.dumps(subtask.files)}\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"Results of dependencies:\n{json.dumps(deps, ensure_ascii=False, indent=2)}"
            f"{previous}"
        )

    # -- critic ------------------------------------------------------------

    def run_critic(self) -> CriticVerdict | str:
        prompt = (
            f"Original task:\n{clip(self.task.prompt, 6000)}\n\n"
            f"Plan summary: {self.plan.summary}\nRisk: {self.plan.risk} "
            f"{json.dumps(self.plan.risk_reasons, ensure_ascii=False)}\n\n"
            "Subtasks and worker results (claims to verify):\n"
            f"{json.dumps([asdict(st) for st in self.subtasks.values()], ensure_ascii=False, indent=2)}\n\n"
            f"Executor report:\n{self.executor_report or '-'}\n\n"
            f"Repository diff (read it yourself too):\n{read_task_diff(self.task.project_path)}"
        )
        feedback = ""
        last_error = ""
        for _attempt in (1, 2):
            result = self.call_lead(ROLE_CRITIC, prompt + feedback)
            if result.timed_out or result.returncode != 0:
                return f"Critic process failed (exit {result.returncode}, timeout={result.timed_out})."
            try:
                return parse_critic(result.text)
            except PlanError as exc:
                last_error = str(exc)
                feedback = (
                    f"\n\nYour previous reply was rejected: {exc}. "
                    "Reply with one valid JSON object only."
                )
        return f"Critic returned an invalid verdict twice: {last_error}"

    def write_final(self, path: Path, verdict: CriticVerdict) -> None:
        lines = [self.executor_report or "Executor report is empty.", "", f"Critic: {verdict.verdict}"]
        if verdict.summary:
            lines.append(verdict.summary)
        for issue in verdict.issues:
            lines.append(
                f"- [{issue.get('severity', '?')}] {issue.get('file', '')}:{issue.get('line', '')} "
                f"{issue.get('problem', '')}"
            )
        if verdict.unverified:
            lines.append("Не проверено: " + "; ".join(verdict.unverified))
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def trim_result(payload: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(payload, ensure_ascii=False)) <= RESULT_CHARS * 2:
        return payload
    return {
        "status": payload.get("status"),
        "summary": clip(str(payload.get("summary") or ""), RESULT_CHARS),
        "files_changed": payload.get("files_changed", []),
        "open_issues": payload.get("open_issues", []),
        "truncated": True,
    }


def render_plan(plan: Plan) -> str:
    lines = [f"Planner: план готов (risk: {plan.risk}). {plan.summary}".strip()]
    for subtask in plan.subtasks:
        deps = f" <- {', '.join(subtask.depends_on)}" if subtask.depends_on else ""
        lines.append(f"- {subtask.id} [{subtask.role}] {subtask.title}{deps}")
    return "\n".join(lines)
