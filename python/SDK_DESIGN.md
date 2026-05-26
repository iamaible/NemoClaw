# NemoClaw Python SDK — Design Reference

> **Audience:** engineers integrating the SDK into a control plane or automation layer.
> This document covers module responsibilities, SDK layer boundaries, key design
> decisions, and the data flow for each major operation.

---

## 1. Purpose

The NemoClaw Python SDK (`nemoclaw/`) is the programmatic, K8s-deployable layer
for sandbox orchestration. It sits **above** the OpenShell Python SDK
(`openshell/`) and provides:

- Higher-level workflow orchestrators (onboard, rebuild, upgrade)
- A local state registry so callers don't round-trip into running sandboxes for metadata
- Policy preset management (named presets instead of raw YAML)
- Skill deployment via SSH
- Aggregated sandbox status

It is **not** the interactive NemoClaw CLI (`nemoclaw onboard` wizard). That is
a TypeScript product targeting end-users. This SDK targets control planes,
automation scripts, and the NemoClaw K8s controller.

---

## 2. Layer Boundary

```
┌─────────────────────────────────────────┐
│          Your Control Plane             │
│  (FastAPI / gRPC service / script)      │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│        NemoClaw Python SDK              │  ← nemoclaw/
│                                         │
│  OnboardOrchestrator  RebuildOrchestrator
│  UpgradeOrchestrator  SkillDeployer     │
│  StatusAggregator     PolicyManager     │
│  ChannelManager       PolicyEngine      │
│  SandboxStateRegistry BlueprintLoader   │
│  SnapshotManager                        │
└──────────────┬──────────────────────────┘
               │  uses
┌──────────────▼──────────────────────────┐
│        OpenShell Python SDK             │  ← openshell/
│                                         │
│  SandboxClient    ProviderClient        │
│  PolicyClient     InferenceRouteClient  │
└──────────────┬──────────────────────────┘
               │  gRPC
┌──────────────▼──────────────────────────┐
│        OpenShell Gateway                │
│  (K8s pod / local Docker)               │
└─────────────────────────────────────────┘
```

**Rule:** NemoClaw modules may call OpenShell SDK clients but never call the
OpenShell CLI binary (`subprocess`). The CLI is only for human/script use
outside the SDK.

---

## 3. Module Inventory

### 3.1 Orchestrators

#### `onboard_orchestrator.py` — `OnboardOrchestrator`

First-time sandbox creation. The programmatic equivalent of `nemoclaw onboard`.

**When to use:** `POST /agents/launch` in a control plane; any automation that
creates a sandbox that did not previously exist.

**Workflow:**
```
preflight (not in registry?) → create → wait_ready
  → attach_providers → apply_presets → register
```

**Key parameter:** `registry_entry: SandboxEntry | None`
Pass a fully-populated entry (with `metadata={"folder_id": ..., "model_id": ...}`)
to store caller-specific data alongside the NemoClaw fields. When `None`, a
minimal entry is written.

**Does NOT:** delete, rebuild, or touch an existing sandbox.

---

#### `rebuild_orchestrator.py` — `RebuildOrchestrator`

Rebuild an existing registered sandbox in-place (delete → recreate, preserving
providers and policy presets).

**When to use:** image upgrade, config change on a running sandbox, `/agents/update`
equivalent.

**Precondition:** sandbox must already exist in the local registry (onboard first).

**Workflow:**
```
preflight (in registry?) → snapshot_providers → [snapshot presets from registry]
  → delete → wait_deleted → create → wait_ready
  → attach_providers → apply_presets
```

Registry entry is **not** re-written by rebuild — the entry already exists. Callers
call `registry.update_sandbox()` separately if they need to change stored fields.

---

#### `upgrade_orchestrator.py` — `UpgradeOrchestrator`

Batch image-version check and optional rebuild across multiple sandboxes.

**When to use:** rolling upgrades, CI pipelines that bump the sandbox image tag.

**Methods:**
- `check(desired_image_tag, sandbox_names=None)` — pure read, returns which sandboxes are behind
- `upgrade(desired_image_tag, *, force=False, dry_run=False, sandbox_names=None)` — calls `RebuildOrchestrator` per sandbox

---

### 3.2 Policy

#### `policy_engine.py` — `PolicyEngine`

Resolves named policy presets from YAML files into OpenShell policy objects.
Pure in-memory; no gRPC calls.

**When to use:** anywhere you need to convert a preset name (`"aible"`,
`"brave-search"`) into a merge operation for `PolicyClient`.

**Key types:** `PresetInfo`, `TierInfo`, `TierPresetRef`

---

#### `policy_manager.py` — `PolicyManager`

Combines `PolicyEngine` (preset parsing) with `openshell.policy.PolicyClient`
(gRPC merge operations) so callers don't build proto objects.

**Methods:**
- `add_preset(sandbox_name, preset_name)` → `PolicyAddResult`
- `remove_preset(sandbox_name, preset_name)` → `PolicyRemoveResult`
- `apply_tier(sandbox_name, tier_name)` → `PolicyApplyTierResult`
- `list_presets(sandbox_name)` → `list[PresetInfo]`

**Registry side-effect:** `add_preset` and `remove_preset` update `entry.policies`
in the local registry.

---

### 3.3 State

#### `registry.py` — `SandboxStateRegistry`

File-based local state store at `~/.nemoclaw/sandboxes.json` (overridable via
`NEMOCLAW_REGISTRY_FILE` env var). Uses a lock directory (`sandboxes.json.lock`)
for safe concurrent writes.

**Key type:** `SandboxEntry`

```python
@dataclass
class SandboxEntry:
    name: str
    image_tag: str | None       # sandbox container image
    model: str | None           # inference model
    provider: str | None        # inference provider name
    gpu_enabled: bool
    policies: list[str]         # applied preset names
    custom_policies: list[CustomPolicyEntry]
    policy_tier: str | None
    agent: str | None           # agent type (e.g. "openclaw")
    agent_version: str | None
    messaging_channels: list[str]
    disabled_channels: list[str]
    dashboard_port: int | None
    metadata: dict[str, Any]    # caller-defined extension data
    # ... (GPU detail fields omitted for brevity)
```

The `metadata` field is intentionally untyped — it is the extension point for
application-specific data (e.g. `{"folder_id": 42, "model_id": 7}` from a
control plane) without polluting the NemoClaw schema.

**Key functions:**
```python
register_sandbox(entry)              # write new entry (error if exists)
update_sandbox(name, **fields)       # partial update
get_sandbox(name) -> SandboxEntry|None
list_sandboxes() -> list[SandboxEntry]
remove_sandbox(name) -> bool
get_default() -> str | None
set_default(name)
add_custom_policy(name, entry)
remove_custom_policy(sandbox_name, preset_name)
```

**Design note:** The registry is the source of truth for **metadata**. Live
sandbox phase comes from `SandboxClient.get()`. Never read metadata from inside
the sandbox (e.g. reading a file via `exec_stream`) when the registry has it.

---

### 3.4 Status

#### `status.py` — `StatusAggregator`

Aggregates registry state + live gRPC probe into a single `SandboxStatus` struct.
Replaces manual polling of `sandbox_get` + `probe_gateway_ui` patterns.

**Constructor:** takes an optional `SandboxClient`. If `None`, gateway probe is skipped.

**Methods:**
- `get_status(sandbox_name, *, skip_inference_probe=False)` → `SandboxStatus`
- `get_status_all(*)` → `list[SandboxStatus]`

**`SandboxStatus` shape:**
```python
@dataclass
class SandboxStatus:
    name: str
    registry_found: bool
    model: str
    provider: str
    policies: list[str]
    gpu_enabled: bool
    image_tag: str | None
    agent: str | None
    gateway: LiveGatewayStatus | None   # None if client not provided
    inference: InferenceHealthStatus | None
```

---

### 3.5 Skills

#### `skill_deployer.py` — `SkillDeployer`

SSH-based skill installation via `SandboxClient.create_ssh_session()` and
paramiko. Replaces `sandbox_write_text_file` + `sandbox exec python3 -c ...`
patterns in the old CLI wrapper.

**Constructor:** takes `SandboxClient` + injectable `SshRunnerFactory` (default:
`paramiko_runner_factory`).

**Methods:**
- `deploy(sandbox_name, skill_dir)` → `DeployResult` — installs all SKILL.md files from a local directory

**`parse_skill_frontmatter(content)`** — validates YAML `---` delimiters and
`name` field before deploy. Raises `SkillDeployerError` on invalid content.

**`_collect_files(skill_dir)`** — returns `(safe, dotfiles, unsafe)` — dotfile
paths are never uploaded.

---

### 3.6 Channels

#### `channels.py` — `ChannelManager`

Add, remove, start, and stop messaging channels (Slack, etc.) on a sandbox.
Reads channel state from the registry; executes changes via `SandboxClient.exec_stream`.

**Methods:**
- `add_channel(sandbox_name, channel_type, config)` → `ChannelResult`
- `remove_channel(sandbox_name, channel_type)` → `ChannelResult`
- `start_channel(sandbox_name, channel_type)` → `ChannelResult`
- `stop_channel(sandbox_name, channel_type)` → `ChannelResult`

---

### 3.7 Snapshots

#### `snapshot_manager.py` — `SnapshotManager`

Tar-based backup and restore of sandbox workspace via `exec_stream`.

**Methods:**
- `backup(sandbox_name, label=None)` → `SnapshotRef`
- `restore(sandbox_name, selector)` → `RestoreResult`
- `list_snapshots(sandbox_name)` → `list[SnapshotRef]`
- `find_snapshot(sandbox_name, selector)` → `SnapshotRef | None`
- `delete_snapshot(sandbox_name, selector)` → `bool`

**Selectors:** `"v<N>"` (version number), `"latest"`, label string, or ISO timestamp prefix.

---

### 3.8 Blueprint

#### `blueprint.py` — `BlueprintLoader`

Pydantic models for `blueprint.yaml`. Parses inference profiles, sandbox spec,
router config, and policy config. Read-only; no gRPC calls.

**Root model:** `Blueprint`

```python
class Blueprint(BaseModel):
    version: str
    components: Components   # sandbox, inference, router, policy
    ...
```

---

## 4. OpenShell SDK — What NemoClaw Uses

NemoClaw does **not** re-implement these. Use them directly when the NemoClaw
layer doesn't provide a method.

| Need | OpenShell module | Key method |
|---|---|---|
| Sandbox CRUD | `openshell.sandbox.SandboxClient` | `create`, `get`, `list`, `delete`, `wait_ready` |
| File exec / read in sandbox | `SandboxClient` | `exec_stream(sandbox_id, command, stdin=...)` |
| SSH session for SkillDeployer | `SandboxClient` | `create_ssh_session`, `revoke_ssh_session` |
| Provider create / attach / detach | `openshell.provider.ProviderClient` | `create`, `attach`, `detach`, `list_sandbox_providers` |
| Cluster inference routing | `openshell.sandbox.InferenceRouteClient` | `set`, `get` |
| Policy draft chunk approve/reject | `openshell.policy.PolicyClient` | `list_draft_policy_chunks`, `approve_draft_policy_chunk`, `reject_draft_policy_chunk` |
| TLS config | `openshell.sandbox.TlsConfig` | constructor |

**Constructing clients from env:**
```python
from openshell.sandbox import SandboxClient, TlsConfig
from openshell.provider import ProviderClient
from openshell.policy import PolicyClient

tls = TlsConfig(
    ca_path=Path(os.environ["OPENSHELL_TLS_CA"]),
    cert_path=Path(os.environ["OPENSHELL_TLS_CERT"]),
    key_path=Path(os.environ["OPENSHELL_TLS_KEY"]),
)
sandbox_client = SandboxClient(os.environ["OPENSHELL_ENDPOINT"], tls=tls)
provider_client = ProviderClient.from_sandbox_client(sandbox_client)
policy_client = PolicyClient(sandbox_client._channel)
```

---

## 5. Data Flow

### 5.1 First-time sandbox launch (`POST /agents/launch`)

```
Control plane
  │
  ├─ [Aible-specific] resolve LLM endpoint → ProviderClient.create()
  ├─ [Aible-specific] InferenceRouteClient.set()
  │
  └─ OnboardOrchestrator.onboard(
         sandbox_name,
         spec=SandboxSpec(image=..., env={AIBLE_API_KEY:..., ...}),
         providers=["llm-provider", "web-search-provider"],
         presets=["aible"],
         registry_entry=SandboxEntry(
             name=sandbox_name,
             image_tag=settings.sandbox_image,
             policies=["aible"],
             metadata={"folder_id": 42, "model_id": 7,
                       "gateway_token": "...", "openclaw_port": 8080},
         ),
     )
     │
     ├─ SandboxClient.create(spec)
     ├─ SandboxClient.wait_ready(sandbox_name)
     ├─ ProviderClient.attach(sandbox_name, "llm-provider")
     ├─ PolicyManager.add_preset(sandbox_name, "aible")
     └─ registry.register_sandbox(entry)
  │
  ├─ [K8s only] SandboxClient.get(sandbox_name) → sandbox_ref.id
  ├─ [K8s only] SandboxClient.exec_stream(sandbox_id, ["/usr/local/bin/aible-start"])
  │            ↳ daemon thread; execs nemoclaw-start which starts openclaw gateway
  ├─ [K8s only] poll: SandboxClient.exec(sandbox_id, ["ss","-tlnp","|","grep",":8080"])
  │            ↳ wait until openclaw is listening before routing traffic to it
  ├─ [K8s only] create K8s Service + HTTPRoute (Envoy Gateway) for sandbox
  │            ↳ ws://gateway/sandbox/{name}/ws → pod:8080
  │
  └─ StatusAggregator.get_status(sandbox_name)  →  return to caller
```

**Why the explicit exec step?** In Kubernetes, OpenShell sandbox pods use
`command: ['/opt/openshell/bin/openshell-sandbox']` in their pod spec, which
overrides the image ENTRYPOINT. The NemoClaw image entrypoint (`aible-start` →
`nemoclaw-start`) is never called automatically. The control plane must exec it
explicitly via `SandboxClient.exec_stream()` after the sandbox reaches Ready
phase. See [§ 5.6](#56-k8s-entrypoint-override--openclaw-startup) for the full pattern.

**Gateway token:** the token is generated by the control plane, stored in
`registry_entry.metadata["gateway_token"]`, and injected into the sandbox via
`GATEWAY_TOKEN` in `SandboxSpec.environment`. `aible-start` writes it into
`openclaw.json` before calling `nemoclaw-start`, so the openclaw gateway uses
the same token the control plane knows about.

---

### 5.2 Sandbox rebuild / update (`POST /agents/update`)

```
Control plane
  │
  └─ RebuildOrchestrator.rebuild(
         sandbox_name,
         spec=SandboxSpec(image=new_image, env={...}),
     )
     │
     ├─ registry.get_sandbox(sandbox_name)  [preflight]
     ├─ ProviderClient.list_sandbox_providers(sandbox_name)
     ├─ SandboxClient.delete(sandbox_name)
     ├─ SandboxClient.wait_deleted(sandbox_name)
     ├─ SandboxClient.create(spec)
     ├─ SandboxClient.wait_ready(sandbox_name)
     ├─ ProviderClient.attach(sandbox_name, ...) × N
     └─ PolicyManager.add_preset(sandbox_name, ...) × N
  │
  └─ StatusAggregator.get_status(sandbox_name)  →  return to caller
```

---

### 5.3 Skill install (`PUT /sandboxes/{name}/skills/{skill}`)

```
Control plane
  │
  └─ SkillDeployer.deploy(sandbox_name, local_skill_dir)
     │
     ├─ SandboxClient.create_ssh_session(sandbox_name)
     ├─ paramiko_runner_factory(session_ref)
     ├─ [upload] cat > /sandbox/.openclaw/workspace/skills/...
     ├─ [clear sessions] printf '{}' > sessions.json
     ├─ [verify] test -f <target> && echo EXISTS
     └─ SandboxClient.revoke_ssh_session(session_ref)
```

---

### 5.4 Sandbox health check (`GET /sandboxes/{name}`)

```
Control plane
  │
  └─ StatusAggregator.get_status(sandbox_name)
     │
     ├─ registry.get_sandbox(sandbox_name)   [metadata, policies, model]
     ├─ SandboxClient.get(sandbox_name)      [live phase]
     └─ HTTP probe → gateway UI              [is_live, latency_ms]
```

---

### 5.5 Policy draft chunk review

```
Control plane  →  PolicyClient.list_draft_policy_chunks(sandbox_name)
             →  PolicyClient.approve_draft_policy_chunk(sandbox_id, chunk_id)
             →  PolicyClient.reject_draft_policy_chunk(sandbox_id, chunk_id)
```
(Direct OpenShell SDK — no NemoClaw layer needed here.)

---

### 5.6 K8s ENTRYPOINT override — OpenClaw startup

OpenShell K8s sandbox pods always launch with:
```yaml
command: ["/opt/openshell/bin/openshell-sandbox"]
```
This overrides any image `ENTRYPOINT`. On a NemoClaw image the intended
entrypoint is `aible-start` → `nemoclaw-start`, but it is never invoked
automatically in K8s. The control plane must exec it explicitly:

```python
# 1. Resolve UUID (exec_stream takes sandbox_id, not name)
sandbox_ref = sandbox_client.get(effective_name)   # → SandboxRef(id=uuid, name=...)

# 2. Start aible-start in a background daemon thread.
#    The thread drains the stream so backpressure never stalls the gateway.
def _start_openclaw_bg(sandbox_id: str) -> None:
    def _run():
        try:
            for chunk in sandbox_client.exec_stream(
                sandbox_id, ["/usr/local/bin/aible-start"],
                timeout_seconds=86400,       # matches sandbox lifetime
            ):
                if hasattr(chunk, "data"):
                    logger.debug("openclaw: %s", chunk.data.decode(errors="replace"))
        except Exception as exc:
            logger.debug("openclaw stream closed: %s", exc)
    threading.Thread(target=_run, daemon=True).start()

# 3. Wait until openclaw binds its port (ss -tlnp) before routing traffic.
async def _wait_openclaw_port(sandbox_id: str, port: int, timeout=120.0) -> bool:
    check = ["bash", "-c", f"ss -tlnp 2>/dev/null | grep -q ':{port} '"]
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await loop.run_in_executor(
            None, lambda: sandbox_client.exec(sandbox_id, check, timeout_seconds=5)
        )
        if result.exit_code == 0:
            return True
        await asyncio.sleep(2)
    return False

# 4. Create the K8s Service + HTTPRoute only after openclaw is ready.
_start_openclaw_bg(sandbox_ref.id)
await _wait_openclaw_port(sandbox_ref.id, openclaw_port)
create_sandbox_route(effective_name, ...)
```

**Why `exec_stream` rather than `exec`?** `exec` blocks until the process exits.
`aible-start` stays alive for the lifetime of the sandbox (it execs
`nemoclaw-start`, which execs the openclaw gateway). Wrapping `exec_stream` in a
daemon thread lets the HTTP request return normally while keeping the exec stream
open.

**Why poll the port?** The K8s Service + HTTPRoute must be created only after the
upstream is reachable, otherwise Envoy returns 503 for the entire initial window
while openclaw is still starting.

**`exec_stream` vs `exec` for short commands:** for one-shot checks (like `ss
-tlnp`) use `exec()` — it collects stdout/stderr and returns an `ExecResult`.
Reserve `exec_stream()` for long-running processes where you want to consume
output incrementally.

---

## 6. Key Design Decisions

### Frozen dataclasses for all result types
`OnboardResult`, `RebuildResult`, `DeployResult`, etc. are `@dataclass(frozen=True)`.
Results are immutable values — callers can't accidentally mutate them after the fact.

### Injectable clients via constructor
All orchestrators take optional `sandbox_client`, `provider_client`, `policy_manager`
in `__init__`. When a client is `None`, the corresponding steps are silently skipped
rather than raising. This makes unit testing straightforward — pass `None` to skip
gRPC calls, pass a fake to control behavior.

### Registry as source of truth for metadata
Metadata (model, provider, presets, `metadata` dict) lives in the local registry.
Live phase comes from gRPC. The two are merged in `StatusAggregator`. Never read
metadata by exec-ing commands inside a running sandbox.

### `metadata: dict[str, Any]` on `SandboxEntry`
An intentionally untyped extension field. NemoClaw doesn't know about
`folder_id`, `model_id`, or any application-specific concept. The caller puts
whatever it needs there. This keeps the NemoClaw schema stable while allowing
integrators to store arbitrary data.

### `OnboardOrchestrator` vs `RebuildOrchestrator` — not combined
The two orchestrators are intentionally separate rather than one class with a
`first_time: bool` flag. Their preconditions are opposite (onboard: must NOT be
in registry; rebuild: MUST be in registry) and only onboard writes the registry
entry. Combining them would produce an orchestrator whose behaviour is unclear
without reading the flag.

### SkillDeployer uses SSH, not `exec_stream`
`exec_stream` can transfer content but doesn't reliably create parent directories
for arbitrary paths. The SSH + `cat >` pattern via `paramiko` (or an injectable
`SshRunnerFactory`) is more predictable and testable with `_FakeRunner`.

---

## 7. Testing Conventions

- Tests live adjacent to source: `foo.py` → `foo_test.py`
- No real gRPC calls in unit tests — all clients are replaced with fakes via `monkeypatch` or constructor injection
- `openshell.sandbox.ExecChunk` / `ExecResult` are patched with `_FakeExecChunk` / `_FakeExecResult` in snapshot and exec tests
- Run tests: `uv run pytest` from `NemoClaw/python/`

---

## 8. Environment Variables

| Variable | Default | Used by |
|---|---|---|
| `NEMOCLAW_REGISTRY_FILE` | `~/.nemoclaw/sandboxes.json` | `registry` |
| `NEMOCLAW_SNAPSHOTS_DIR` | `~/.nemoclaw/snapshots` | `SnapshotManager` |
| `OPENSHELL_ENDPOINT` | — | caller builds `SandboxClient` |
| `OPENSHELL_TLS_CA` | — | caller builds `TlsConfig` |
| `OPENSHELL_TLS_CERT` | — | caller builds `TlsConfig` |
| `OPENSHELL_TLS_KEY` | — | caller builds `TlsConfig` |

---

## 9. What the SDK Does Not Own

| Concern | Owner |
|---|---|
| Aible auth / Bearer token validation | Control plane |
| LLM endpoint resolution (Aible model registry) | Control plane |
| LLM mode routing (NIM / OpenAI / Anthropic) | Control plane |
| `ProviderClient.create()` for inference providers | Control plane (caller) |
| `InferenceRouteClient.set()` | Control plane (caller) |
| Slack / messaging config resolution | Control plane |
| OpenClaw WebSocket gateway URL construction | Control plane |
| Port forwarding (local dev only) | Control plane |
| **OpenClaw startup via exec (K8s ENTRYPOINT override)** | **Control plane** — see § 5.6 |
| **K8s Service + HTTPRoute (Envoy Gateway) per sandbox** | **Control plane** (`k8s_routing.py`) |
| **Gateway token generation and injection into `openclaw.json`** | **Control plane** (via `GATEWAY_TOKEN` env + `aible-start.sh`) |
| Interactive wizard (`nemoclaw onboard`) | NemoClaw TypeScript CLI |
