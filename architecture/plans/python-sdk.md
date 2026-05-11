# NemoClaw Python SDK — Master Plan

## Goal

Replace NemoClaw's CLI-spawning orchestration with a pure-Python SDK stack that
can be containerized for Kubernetes without shipping any `openshell` or
`nemoclaw` CLI binaries in the service image.

## Problem

NemoClaw's TypeScript blueprint runner communicates with the OpenShell gateway
by spawning the `openshell` binary as a subprocess. This makes containerization
awkward: the binary must be present, versioned, and on `$PATH`. In a K8s
service pod you want a Python package that speaks gRPC directly to the
OpenShell gateway Deployment.

## Target Architecture

```
Your service pod
┌─────────────────────────────────────────┐
│  your-service (Python)                  │
│  + nemoclaw (Python SDK)  ←─ new        │
│  + openshell (Python SDK) ←─ extended   │
└──────────────────┬──────────────────────┘
                   │ gRPC (mTLS or insecure)
┌──────────────────▼──────────────────────┐
│  OpenShell gateway (K8s Deployment)     │
│  openshell-server binary                │
└─────────────────────────────────────────┘
```

No CLI binaries in the service container. The gateway runs as its own
Deployment, addressed via `SandboxClient` endpoint config or cluster metadata.

## Repos and Package Locations

| Deliverable | Location | Package name |
|---|---|---|
| Extended OpenShell Python SDK | `OpenShell/python/openshell/` | `openshell` |
| New NemoClaw Python SDK | `NemoClaw/python/nemoclaw/` | `nemoclaw` |

---

## Phase 1 — Extend OpenShell Python SDK

The proto already defines all required RPC methods. The SDK just needs Python
wrappers for the ~13 methods not yet exposed.

### 1a. ProviderClient  (`openshell/provider.py`)

New client class sharing the same `grpc.Channel` as `SandboxClient`.

| RPC | Purpose |
|---|---|
| `CreateProvider` | Add messaging/credential provider |
| `GetProvider` | Fetch provider by name |
| `ListProviders` | Enumerate providers |
| `UpdateProvider` | Update provider env vars / credentials |
| `DeleteProvider` | Remove provider |
| `AttachSandboxProvider` | Bind provider to a sandbox |
| `DetachSandboxProvider` | Unbind provider from a sandbox |
| `GetSandboxProviderEnvironment` | Fetch injected env for a sandbox |
| `ListProviderProfiles` | Enumerate provider profiles |
| `GetProviderProfile` | Fetch a specific profile |

### 1b. PolicyClient  (`openshell/policy.py`)

| RPC | Purpose |
|---|---|
| `GetSandboxConfig` | Fetch current sandbox policy YAML |
| `UpdateConfig` | Apply `PolicyMergeOperation` (add/remove rules) |
| `GetSandboxPolicyStatus` | Live enforcement status |
| `ListSandboxPolicies` | Enumerate applied policies |

### 1c. SandboxClient extensions  (`openshell/sandbox.py`)

| RPC | Purpose |
|---|---|
| `CreateSshSession` | Obtain SSH credentials for sandbox |
| `RevokeSshSession` | Tear down SSH session |
| `GetSandboxLogs` | Fetch historical logs |
| `WatchSandbox` | Stream live sandbox events |

### 1d. `__init__.py` re-exports

Expose all new clients from the top-level `openshell` namespace.

---

## Phase 2 — NemoClaw Python SDK Core

Package scaffolding + shared infrastructure used by all feature modules.

### 2a. Package setup  (`NemoClaw/python/`)

- `pyproject.toml` — `nemoclaw` package, depends on `openshell`, `pyyaml`,
  `pydantic`, `paramiko`
- `nemoclaw/__init__.py` — public surface

### 2b. BlueprintLoader  (`nemoclaw/blueprint.py`)

Pydantic models for `blueprint.yaml`: inference profile, sandbox spec, router
config, policy references. Validates on load; raises typed errors.

### 2c. SandboxStateRegistry  (`nemoclaw/registry.py`)

Read/write `~/.nemoclaw/state.json` (sandboxes, custom policies, messaging
channels, version metadata). Typed dataclasses. Thread-safe file locking.

### 2d. PolicyEngine  (`nemoclaw/policy_engine.py`)

Port of TypeScript `policies.ts` / `tiers.ts`:
- Enumerate presets from `nemoclaw-blueprint/policies/presets/*.yaml`
- Resolve tier access levels from `tiers.yaml`
- YAML merge: add / remove network rules against a base policy
- Validate custom preset structure (`preset.name` + `network_policies`)

---

## Phase 3 — Feature Modules

Build in this order (each unblocks the next).

### 3.1  Status + Diagnostics  (`nemoclaw/status.py`)

**Depends on:** Phase 1c (WatchSandbox, GetSandboxLogs), Phase 2c (registry)

Aggregates three sources:
- Registry metadata (base config, model, provider)
- Live gateway state via `GetSandbox` + `WatchSandbox`
- Inference health via HTTP probe (provider-type-aware: NIM, Ollama, TGI, etc.)

Returns a `SandboxStatus` dataclass. Streaming variant yields `StatusEvent`s.

### 3.2  Channel Lifecycle  (`nemoclaw/channels.py`)

**Depends on:** Phase 1a (ProviderClient), Phase 2c (registry)

- `add(channel, token)` — create provider, attach to sandbox, update registry
- `remove(channel)` — detach provider, delete, update registry
- `start(channel, token)` — update provider credentials (token rotation)
- `stop(channel)` — detach provider without deleting (preserves config)
- `list()` — return registry channels with live attachment status

### 3.3  Policy Tiers + Presets  (`nemoclaw/policies.py`)

**Depends on:** Phase 1b (PolicyClient), Phase 2d (PolicyEngine), Phase 2c (registry)

- `list_presets()` — enumerate available presets with tier info
- `add_preset(name)` — merge preset rules via `UpdateConfig`
- `remove_preset(name)` — remove preset rules via `UpdateConfig`
- Custom preset support: load from file, validate, persist in registry for
  later removal

### 3.4  Rebuild  (`nemoclaw/rebuild.py`)

**Depends on:** 3.2 (channels), 3.3 (policies), Phase 1 (all clients)

Orchestration state machine:
1. Preflight checks (image resolution, inference endpoint health)
2. Auto-snapshot current state (calls 3.5 internally)
3. Delete old sandbox
4. Create new sandbox from same image
5. Re-attach providers in order
6. Re-apply policies
7. Restore inference config
8. Poll until `SANDBOX_PHASE_READY`

### 3.5  Snapshot + Backup  (`nemoclaw/snapshots.py`)

**Depends on:** Phase 1 (all clients), Phase 2c (registry)

⚠️ **Investigate first**: `openshell sandbox snapshot` CLI commands have no
corresponding RPC in the current proto. Before implementing, determine whether:
- Snapshots are purely local filesystem archives (workspace tar + registry)
- OR there is gRPC backing not yet in the published proto

If local-only: implement as filesystem archive + registry versioning.
If gRPC-backed: add snapshot RPCs to the proto and extend the OpenShell SDK.

- `create(label)` — snapshot current sandbox state
- `list()` — return versioned snapshot table
- `restore(version)` — restore to target sandbox

### 3.6  Upgrade Orchestration  (`nemoclaw/upgrade.py`)

**Depends on:** 3.4 (rebuild)

- `check()` — compare live sandbox versions against expected; return stale list
- `upgrade(sandboxes, auto)` — batch rebuild stale sandboxes; continue on
  failure, report summary

### 3.7  Skill Lifecycle  (`nemoclaw/skills.py`)

**Depends on:** Phase 1c (CreateSshSession), Phase 2c (registry)

- `install(skill_dir, sandbox_id)` — parse SKILL.md frontmatter, validate
  paths, upload via SSH, trigger agent refresh
- SSH client: `paramiko` (or use `ExecSandbox` for refresh commands to avoid
  SSH library if sufficient)

---

## Phase 4 — K8s Packaging

- `NemoClaw/python/Dockerfile` — slim Python image, no CLI binaries
- `NemoClaw/deploy/python-sdk/` — Helm chart or K8s manifests for the service
- Gateway connection via `SandboxClient(endpoint, tls=TlsConfig(...))` or
  `SandboxClient.from_active_cluster()` with config mounted as a K8s Secret

---

## Build Sequence

```
Phase 1a  ProviderClient                (OpenShell SDK)
Phase 1b  PolicyClient                  (OpenShell SDK)
Phase 1c  SandboxClient extensions      (OpenShell SDK)
Phase 1d  __init__ re-exports           (OpenShell SDK)
Phase 2a  nemoclaw package setup        (NemoClaw SDK)
Phase 2b  BlueprintLoader               (NemoClaw SDK)
Phase 2c  SandboxStateRegistry          (NemoClaw SDK)
Phase 2d  PolicyEngine                  (NemoClaw SDK)
Phase 3.1 StatusAggregator             (NemoClaw SDK)
Phase 3.2 ChannelManager               (NemoClaw SDK)
Phase 3.3 PolicyManager                (NemoClaw SDK)
Phase 3.4 RebuildOrchestrator          (NemoClaw SDK)
Phase 3.5 Snapshots (investigate first) (NemoClaw SDK)
Phase 3.6 UpgradeOrchestrator          (NemoClaw SDK)
Phase 3.7 SkillDeployer                (NemoClaw SDK)
Phase 4   K8s packaging                 (NemoClaw deploy)
```

## Open Questions

1. **Snapshots**: Do `openshell snapshot` CLI commands have gRPC backing?
   Check `openshell-server` source before implementing Phase 3.5.

2. **Blueprint directory**: Should `nemoclaw-blueprint/policies/presets/` be
   bundled inside the Python package (as package data) or remain a filesystem
   path configured at runtime? Runtime config is more flexible for K8s
   (ConfigMap mount).

3. **Registry location**: `~/.nemoclaw/state.json` is a local path convention.
   For K8s, this should be a PersistentVolumeClaim mount or externalised to a
   config store. Design the registry abstraction to support pluggable backends.
