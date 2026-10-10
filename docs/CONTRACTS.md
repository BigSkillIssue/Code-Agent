# Contracts — interfaces the agent must implement exactly

These signatures are binding: implement names, fields, types and defaults exactly as written. Bodies shown as `...` are yours to write. Each block names its file. If a contract cannot work, implement it anyway and raise an *Open issue* in `PROGRESS.md` (see AGENTS.md).

## Messages and events — `src/forge/providers/base.py`, `src/forge/events.py`

One internal message format for every provider; adapters translate to and from it.

```python
# providers/base.py
from typing import Literal, Any
from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]

class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    media_type: str                 # "image/png"
    data_b64: str

class ToolCall(BaseModel):
    id: str                         # provider's id, or "call_<n>" if none
    name: str
    arguments: dict[str, Any]

class ToolResult(BaseModel):
    call_id: str
    ok: bool
    text: str                       # what the model sees (already capped)
    spill_path: str | None = None   # full output file when capped
    code: str | None = None         # error code when ok=False (see docs/TOOLS.md)
    images: list["ImagePart"] = []   # images for vision models (read_file, MCP)

class Message(BaseModel):
    role: Role
    parts: list[TextPart | ImagePart] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)    # assistant only
    tool_result: ToolResult | None = None                       # tool only
    reasoning: str | None = None    # thinking text, kept only if provider needs it back

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
```

```python
# events.py — everything the core reports goes through EventBus as one of these
class Event(BaseModel):
    session_id: str
    agent_id: str = "main"
    ts: float                       # time.time()

class ModelDelta(Event):    kind: Literal["model_delta"] = "model_delta";   text: str
class ModelDone(Event):     kind: Literal["model_done"] = "model_done";     message: Message; usage: Usage
class ToolStarted(Event):   kind: Literal["tool_started"] = "tool_started"; call: ToolCall
class ToolOutput(Event):    kind: Literal["tool_output"] = "tool_output";   call_id: str; text: str   # S51: live output, whole lines, throttled
class TodosUpdated(Event):  kind: Literal["todos_updated"] = "todos_updated"; todos: list[Todo]   # S54 (forge/todos.py)
class ToolFinished(Event):  kind: Literal["tool_finished"] = "tool_finished"; result: ToolResult
class QuestionAsked(Event): kind: Literal["question"] = "question";         questions: list["Question"]
class PlanUpdated(Event):   kind: Literal["plan_updated"] = "plan_updated"; plan: "Plan"
class StepDone(Event):      kind: Literal["step_done"] = "step_done";       step_id: str; ok: bool
class Compacted(Event):     kind: Literal["compacted"] = "compacted";       level: int; tokens_before: int; tokens_after: int
class SessionDone(Event):   kind: Literal["session_done"] = "session_done"; ok: bool; report: str
class ErrorEvent(Event):    kind: Literal["error"] = "error";               message: str

# S59: the independent Apple reviewer's verdict on the request, the plan or the finished app;
# S61: also on the app's App Store listing (stage "listing")
GuidelineArea = Literal["safety", "performance", "business", "design", "legal", "hig"]
GuidelineStatus = Literal["ok", "concern", "violation"]
class GuidelineFinding(BaseModel):
    area: GuidelineArea              # App Store Review Guidelines sections 1-5, or the HIG
    status: GuidelineStatus
    guideline: str = ""              # the rule's number or HIG page, e.g. "5.1.1"
    reason: str
    fix: str = ""
class GuidelineReview(Event):
    kind: Literal["guideline_review"] = "guideline_review"
    stage: Literal["prompt", "plan", "product", "listing"]
    verdict: GuidelineStatus         # the worst finding; "concern" when the review failed
    summary: str
    findings: list[GuidelineFinding] = []   # every area at least once (missing ones: concern)
    sources: list[str] = []          # the Apple pages the reviewer read
    error: str = ""                  # no review could be made; never counts as a pass
```

Rule: every event serializes to one JSON line (`model_dump_json()`); headless mode (S40) prints exactly these lines.

## Provider interface — `src/forge/providers/base.py`

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]      # JSON Schema (object)

class Capabilities(BaseModel):
    context_window: int = 32_000
    max_output: int = 4_096
    tools: bool = True              # native tool calling
    parallel_tools: bool = False
    vision: bool = False
    reasoning: bool = False
    prompt_cache: bool = False
    web_search: bool = False        # provider-native search tool
    cost_in: float = 0.0            # USD per 1M input tokens
    cost_out: float = 0.0

class ChatRequest(BaseModel):
    model: str                      # model id as the provider knows it
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = []
    max_output: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    json_schema: dict[str, Any] | None = None   # structured output when supported

class StreamItem(BaseModel):        # what stream() yields, in order
    delta: str = ""                 # text chunk
    tool_call: ToolCall | None = None  # S52: a call complete before the reply ends (also in done)
    done: Message | None = None     # final assistant message (last item only)
    usage: Usage | None = None      # with the last item

class Provider(Protocol):
    name: str                       # "openai", "anthropic", "openrouter", ...
    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]: ...
    async def count_tokens(self, req: ChatRequest) -> int: ...
    def capabilities(self, model: str) -> Capabilities: ...

class ProviderError(Exception):
    kind: Literal["auth", "rate_limit", "overloaded", "context_overflow", "bad_request", "network"]
    retry_after_s: float | None
```

Rules:

- `stream()` retries `rate_limit`, `overloaded` and `network` itself (backoff 1, 2, 4, 8, 16 s); it raises `ProviderError` only when retries are exhausted or the error is not retryable. The registry then tries the next model in the role's fallback chain.
- If `capabilities(model).tools` is `False`, the adapter itself applies the `TOOL_FALLBACK` prompt and parses tool calls out of the text, so callers never see the difference.
- `count_tokens` may estimate (characters / 4) when the provider has no counting endpoint.

```python
# providers/registry.py
def get_provider(name: str, cfg: "ForgeConfig") -> Provider: ...
def resolve_role(role: str, cfg: "ForgeConfig") -> list[tuple[Provider, str]]:
    """Return the (provider, model) fallback chain for a role, e.g. 'coder'."""
```

`FakeProvider` (`src/forge/providers/fake.py`) implements the same `Provider` protocol and is the only provider used in offline tests.

## Ports — `src/forge/ports.py`

The four seams. v1 implements them in `src/forge/local/`; a server later adds new implementations that pass the same conformance tests (S43).

```python
class Session(BaseModel):
    id: str                          # uuid4 hex
    project_root: str
    created_at: float
    status: Literal["active", "waiting", "done", "failed", "cancelled"]
    spec: "TaskSpec | None" = None
    plan: "Plan | None" = None
    messages: list[Message] = []     # full transcript, never compacted
    summary: str = ""                # latest compaction summary

class Store(Protocol):
    async def create_session(self, project_root: str) -> Session: ...
    async def save_session(self, s: Session) -> None: ...
    async def load_session(self, session_id: str) -> Session: ...
    async def list_sessions(self, project_root: str, limit: int = 20) -> list[Session]: ...
    async def search(self, project_root: str, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """(session_id, excerpt) pairs for `recall`."""

class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...
    def subscribe(self, session_id: str) -> AsyncIterator[Event]: ...

class Command(BaseModel):
    argv: list[str] | None = None    # exec form, or
    script: str | None = None        # text sent to a persistent shell
    shell: Literal["bash", "powershell", "none"] = "none"
    cwd: str
    timeout_s: float = 120
    env: dict[str, str] = {}

class CommandResult(BaseModel):
    exit_code: int | None            # None = still running (background)
    stdout: str
    stderr: str
    timed_out: bool = False
    sandbox_denied: bool = False     # failed because the sandbox blocked it
    job_id: str | None = None

class SandboxPolicy(BaseModel):
    mode: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"
    writable_roots: list[str] = []
    network: bool = False

class Executor(Protocol):
    async def run(self, cmd: Command, policy: SandboxPolicy, background: bool = False,
                  on_output: Callable[[str], None] | None = None) -> CommandResult: ...  # S51: output while it runs
    async def job_output(self, job_id: str, since_line: int = 0) -> CommandResult: ...
    async def job_stop(self, job_id: str) -> CommandResult: ...

class Answer(BaseModel):
    question_index: int
    values: list[str]                # chosen options or typed text

class Approval(BaseModel):
    allow: bool
    remember: bool = False           # add an allow rule for this session
    feedback: str = ""               # "no, do X instead" goes back to the model

class Renderer(Protocol):
    async def show(self, event: Event) -> None: ...
    async def ask(self, questions: list["Question"]) -> list[Answer]: ...
    async def approve(self, call: ToolCall, reason: str) -> Approval: ...
```

**Browser (added in S50).** The browser agent drives pages through this port; `local/playwright_browser.py` implements it with Playwright. `SessionState.browser_factory` holds the factory and `SessionState.browsers` holds the open browser of each agent. Expected failures raise `BrowserError`, which the tools turn into `ToolResult(ok=False)`.

```python
class BrowserError(Exception):         # message + hint
    hint: str

class PageView(BaseModel):
    url: str
    title: str
    image: ImagePart | None = None     # screenshot of the visible part

class Browser(Protocol):               # targets: visible text, "css=<selector>" or "x,y"
    async def open(self, url: str) -> PageView: ...
    async def click(self, target: str) -> PageView: ...
    async def type(self, target: str, text: str, submit: bool) -> PageView: ...
    async def scroll(self, pixels: int) -> PageView: ...
    async def back(self) -> PageView: ...
    async def view(self) -> PageView: ...
    async def read(self) -> str: ...
    async def close(self) -> None: ...

class BrowserFactory(Protocol):        # a fresh context (no profile, no downloads) per agent
    async def new_browser(self) -> Browser: ...
    async def close(self) -> None: ...
```

**Apple builds (added in S58a).** Apple projects (Swift, SwiftUI) are built, tested and shown on simulated devices through this port. `local/xcode_builder.py` implements it on a Mac with Xcode (running XcodeGen first when the project has a `project.yml`); a server may implement it with a remote Mac. `SessionState.apple` holds it; without one the `apple` tools are not offered. Expected failures (no Xcode, no such scheme or device, a timeout) raise `AppleBuildError`; a build that fails is a result with `ok=False` and the compiler's messages. Settings live in `[apple]` (`timeout_s`, `max_screenshots`, `devices`: the preferred simulator per platform).

```python
ApplePlatform = Literal["ios", "ipados", "macos", "watchos"]
AppleAction = Literal["build", "test", "archive"]

class AppleBuildError(Exception):      # message + hint
    hint: str

class AppleIssue(BaseModel):
    severity: Literal["error", "warning"]
    message: str
    file: str = ""
    line: int = 0

class AppleBuildResult(BaseModel):
    ok: bool
    platform: ApplePlatform
    action: AppleAction
    scheme: str
    issues: list[AppleIssue] = []
    tests_run: int = 0
    tests_failed: int = 0
    log_tail: str = ""                 # the end of the build log
    artifact: str = ""                 # the archive (action "archive"), on the builder

class AppleScreen(BaseModel):
    platform: ApplePlatform
    device: str                        # the simulated device ("Mac" for macOS)
    dark: bool
    image: ImagePart

class AppleBuilder(Protocol):          # works on the session's project
    async def build(self, platform: ApplePlatform, action: AppleAction,
                    scheme: str | None = None) -> AppleBuildResult: ...
    async def screenshot(self, platform: ApplePlatform, device: str | None = None,
                         dark: bool = False) -> AppleScreen: ...
    async def close(self) -> None: ...
```

## Plan models — `src/forge/plan.py`

```python
class Question(BaseModel):
    text: str
    kind: Literal["choice", "multi", "text", "confirm"]
    options: list[str] = []          # 2–6 for choice / multi
    default: str | None = None       # used in headless mode
    why: str                         # one line: what changes with the answer

class TaskSpec(BaseModel):
    goal: str
    context: str
    requirements: list[str]
    constraints: list[str] = []
    acceptance_criteria: list[str]   # at least 1
    assumptions: list[str] = []
    open_questions: list[Question] = []
    size: Literal["trivial", "small", "medium", "large"]

StepStatus = Literal["todo", "doing", "done", "failed", "skipped"]

class Step(BaseModel):
    id: str                          # "s1", "s2", ...
    title: str
    detail: str
    files: list[str] = []
    depends_on: list[str] = []
    check: str                       # shell command, or "review: <criterion>"
    role: str = "coder"
    status: StepStatus = "todo"
    notes: str = ""
    attempts: int = 0

class Plan(BaseModel):
    spec: TaskSpec
    steps: list[Step]
    version: int = 1

    def next_ready_step(self) -> Step | None:
        """First 'todo' step whose dependencies are all 'done' or 'skipped'."""
    def validate_graph(self) -> list[str]:
        """Errors: duplicate ids, unknown dependencies, cycles, missing checks. Empty = valid."""
    def ready_steps(self) -> list[Step]:
        """All steps that could run now in parallel (used by teams)."""
```

**Check semantics.** A `check` starting with `review:` is judged by the reviewer role against the step's diff and must return `{"pass": bool, "reason": str}`. Any other `check` is a shell command run with the session's sandbox; exit code 0 = pass.

```python
# pipeline.py — public entry points
async def run_task(prompt: str, ctx: "Ctx") -> "Report": ...      # whole pipeline
async def refine(prompt: str, ctx: "Ctx") -> TaskSpec: ...
async def clarify(spec: TaskSpec, ctx: "Ctx") -> TaskSpec: ...
async def make_plan(spec: TaskSpec, ctx: "Ctx") -> Plan: ...
async def execute(plan: Plan, ctx: "Ctx") -> Plan: ...
async def final_review(plan: Plan, ctx: "Ctx") -> "Report": ...

class Report(BaseModel):
    ok: bool
    summary: str
    files_changed: list[str]
    assumptions: list[str]
    manual_checks: list[str]
    usage: Usage
    ready_for_apple: bool = False    # S60: only when the user approved the app (--apple)
    apple_summary: str = ""          # S60: why the app is (not) ready for Apple
```

## Agent loop — `src/forge/agent.py`

```python
class AgentResult(BaseModel):
    text: str                        # final assistant text
    messages: list[Message]          # this agent's transcript
    usage: Usage
    stopped: Literal["done", "max_turns", "budget", "cancelled", "error"]

async def run_agent(ctx: Ctx, task: str, *, role: str = "coder",
                    history: list[Message] | None = None,
                    max_turns: int = 40) -> AgentResult:
    """Loop: build request (prompts.render(role)), stream via the role's fallback chain,
    run tool calls with call_tool (parallel only if the model asked for several and
    all are read_only), append results, compact if needed, repeat until the model
    answers without tool calls or a limit is hit."""
```

**Early start (S52).** While the reply streams, a call in a `tool_call` item starts at once when four things hold:
- it is read-only, auto-permitted and allowed by the rules (`can_start_early`);
- it is not in a sequential group (`browser`);
- it is not `research`;
- every call before it in the same reply was also started.

The finished reply decides what is used. A started call counts only if the reply contains the same call with the same id, name and arguments; otherwise it is cancelled, for example after a fallback model answered. "Parallel" means read-only and not in a sequential group (`can_run_concurrently`).

## Tool framework — `src/forge/tools.py` (top of file), `src/forge/ctx.py`

```python
# ctx.py — everything a tool or agent may touch, passed explicitly
@dataclass
class Ctx:
    session: Session
    cfg: "ForgeConfig"
    root: Path                       # project root, resolved
    cwd: Path                        # current dir, always inside root
    store: Store
    bus: EventBus
    executor: Executor
    renderer: Renderer
    ledger: "ReadLedger"             # path -> sha256 of last read content
    permissions: "Permissions"
    hooks: "Hooks"
    agent_id: str = "main"
    role: str = "coder"
    headless: bool = False
```

```python
# tools.py — framework part (above all tool definitions)
Permission = Literal["auto", "ask"]

@dataclass(frozen=True)
class ToolDef:
    name: str
    group: str                       # "shell", "files", "search", "web", "plan", "agents", "memory", "mcp"
    fn: Callable[..., Awaitable[str | ToolResult]]
    spec: ToolSpec                   # generated from signature + docstring
    permission: Permission
    read_only: bool                  # True = allowed in plan mode and for reviewer roles
    specifier_arg: str | None        # arg used by rules: "command", "path", "url", "role"

REGISTRY: dict[str, ToolDef] = {}

def tool(*, group: str, permission: Permission = "auto", read_only: bool = False,
         specifier_arg: str | None = None) -> Callable: ...
    """Register an async function `fn(ctx: Ctx, **args)` as a tool. First param must be ctx."""

def for_role(role: str, cfg: "ForgeConfig") -> list[ToolDef]: ...
async def call_tool(ctx: Ctx, call: ToolCall) -> ToolResult: ...
    """validate -> permission -> pre_tool hooks -> run -> cap output -> post_tool hooks -> audit"""

OUTPUT_CAP_OK = 30_000               # chars inline on success
OUTPUT_PREVIEW = 2_000               # chars shown when spilled
OUTPUT_CAP_FAIL = 10_000             # head+tail chars on failure
```

Rules: a tool returns `str` (wrapped as `ok=True`) or a `ToolResult`; it returns `ToolResult(ok=False, text="error: ...")` for expected failures and never raises for them. The docstring's first line is the tool description the model sees; parameter descriptions come from `Annotated[str, "description"]`.

```python
# permissions.py
class Decision(BaseModel):
    action: Literal["run", "ask", "deny"]
    reason: str

class Permissions:
    def check(self, tool: ToolDef, args: dict[str, Any], ctx: Ctx) -> Decision: ...
    """deny rules -> ask rules -> allow rules -> read-only list -> sandbox mode x approval policy"""

# hooks.py
HookEvent = Literal["session_start", "prompt_submit", "pre_tool", "post_tool",
                    "step_done", "pre_compact", "stop", "subagent_stop"]
class HookOutcome(BaseModel):
    block: bool = False
    message: str = ""                # shown to the model when blocking
```

## Config schema — `src/forge/config.py`

`ForgeConfig` is one Pydantic model; this TOML is its full v1 shape. `forge.example.toml` in the repo must equal this file, and a test loads it.

```toml
profile = "default"                    # selected profile, overridable with -p

[providers.openai]
kind = "openai_compat"                 # openai_compat | anthropic | google | litellm
wire = "chat"                          # chat | responses (openai_compat only)
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
headers = {}

[providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[providers.ollama]
kind = "openai_compat"
base_url = "http://localhost:11434/v1"

[models."ollama/qwen3:32b"]            # optional capability overrides for unknown models
context_window = 32000
tools = true

[roles]                                # role -> fallback chain of "provider/model"
refiner    = ["openai/gpt-5-mini"]
planner    = ["anthropic/claude-sonnet"]
coder      = ["anthropic/claude-sonnet", "openai/gpt-5"]
reviewer   = ["openai/gpt-5-mini"]
compressor = ["openai/gpt-5-mini"]
explore    = ["openai/gpt-5-mini"]

[sandbox]
mode = "workspace-write"               # read-only | workspace-write | full-access
network = false
writable_roots = []

[approval]
policy = "on-request"                  # on-request | always | never

[permissions]
allow = ["bash(git status*)", "bash(pytest *)"]
ask   = ["bash(git push *)"]
deny  = ["bash(rm -rf *)", "read_file(./.env*)"]

[limits]
max_turns_per_step = 40
max_step_attempts = 3
max_clarify_rounds = 3
max_parallel_agents = 4
max_cost_usd = 5.0
compact_at = 0.70                      # level 2 summary
reset_at = 0.90                        # level 3 reset
mcp_defer_threshold = 40               # above this many MCP tools, load schemas via tool_search
max_web_searches = 200                 # per session

[web]
search_backend = "native"              # native | brave | tavily | searxng
search_api_key_env = ""

[mcp_servers.github]                   # S36
command = ["npx", "-y", "@modelcontextprotocol/server-github"]
env_keys = ["GITHUB_TOKEN"]

[hooks]                                # S37
post_tool = [{ match = "edit_file|write_file", command = "ruff format {path}" }]

[profiles.ci]
approval = { policy = "never" }
limits = { max_cost_usd = 1.0 }
```

Loading order, later wins: built-in defaults → `~/.forge/forge.toml` → `<project>/.forge/config.toml` → `FORGE_*` env vars (`FORGE_SANDBOX__MODE=read-only`) → CLI flags. A project config may **not** set `providers.*`, `mcp_servers.*` or `hooks.*` unless the project is trusted (`forge trust`); untrusted values are ignored with a warning.

## App manifest — `src/forge/app_manifest.py`

Every product Forge builds (Phase 8) describes itself in `forge.app.toml` at its root. Hosting (Forge Web) reads only this file; it never runs the product's `Dockerfile` or `docker-compose.yml`. Secrets are listed by name; their values live in Forge Web's vault and never in the product.

```python
Runtime = Literal["python3.12", "node22", "static"]
ResourceClass = Literal["small", "medium", "large"]
Client = Literal["web", "apple", "android", "windows"]

class Service(BaseModel):            # extra keys are refused in every model of this file
    name: str                        # lowercase slug, unique in the app
    runtime: Runtime
    root: str = "."                  # the service's folder, relative to the product
    command: list[str] = []          # how to start it; required unless runtime is "static"
    build: list[str] = []            # how to build it (runs in a throwaway container)
    output: str = "dist"             # static only: the folder in root its build writes; served
                                     # as a single-page app (unknown paths get index.html)
    port: int                        # 1024-65535, unique in the app
    health: str = "/healthz"         # path that answers 200 when the service is up
    route: str | None = None         # public path prefix ("/", "/api"); None = internal only

class Database(BaseModel):
    engine: Literal["postgres16"] = "postgres16"

class Storage(BaseModel):
    max_gb: int = 1                  # 1-100

class Mail(BaseModel):
    daily_limit: int = 200           # 1-10000; mail goes out through Forge Web's relay only

class Payments(BaseModel):
    kind: Literal["none", "relay"] = "none"   # "relay": Forge Web's payments relay (W35)
    digital_goods: bool = False      # sold in native apps: the stores' own purchases (Apple 3.1.1)

class AppManifest(BaseModel):
    version: Literal[1] = 1
    name: str                        # lowercase slug: the app's host name
    resource_class: ResourceClass = "small"
    services: list[Service]          # at least one
    database: Database | None = None
    storage: Storage | None = None
    mail: Mail | None = None
    env: dict[str, str] = {}         # plain settings; names like SECRET, TOKEN, PASSWORD, *_KEY refused
    secrets: list[str] = []          # names only, never values
    clients: list[Client] = ["web"]
    payments: Payments = Payments()

class ManifestProblem(BaseModel):
    field: str                       # dotted path, e.g. "services.1.port"; "" for the whole file
    message: str
    line: int | None = None          # for TOML syntax errors

MANIFEST = "forge.app.toml"
def parse_manifest(text: str) -> AppManifest | list[ManifestProblem]: ...
def load_manifest(path: Path) -> AppManifest | list[ManifestProblem]: ...   # a file or the product folder
```

Rules: problems are values, never exceptions; every problem is reported, not only the first. Names in `env` and `secrets` are upper-case (`^[A-Z][A-Z0-9_]*$`), may not repeat between the two, and may not be `PORT` or `DATABASE_URL` (hosting sets them). Routes are unique and start with `/`, as do health paths.
