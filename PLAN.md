# Starting Architecture Plan

This is the initial architecture source for the [primary product map](https://github.com/jdip/Fesnyng/issues/8). The map's confirmed dialogue and [approved MVP specification](https://github.com/jdip/Fesnyng/issues/10) amend the original starting plan; the sections below incorporate those decisions. [The initial snapshot](https://github.com/jdip/Fesnyng/blob/b7aeb8ade1116ed924269632d943b4d7390a2cd9/PLAN.md) preserves the original proposal. README.md owns the enduring project purpose.

The MVP uses a separate React/TypeScript/npm/Vite frontend and two Python/FastAPI backend components, managed with uv, Ruff and ty. One control plane manages multiple independently running agent hosts. SQLite owns durable Fesnyng state from the start; OpenCode owns execution and native session records.

## 0. Public Repository Constraint

This project is intended to be public from the start.

Architecture, documentation, tests, fixtures, screenshots, sample data, and default configuration should be designed with that assumption rather than sanitized later.

Use generic or fictional examples. Avoid embedding real organization names, internal infrastructure details, credentials, customer information, private URLs, or identifying operational data in committed materials.

Public-safe defaults and examples are part of the project architecture, not just documentation hygiene.

---

## Objective

Build the smallest useful version of a **multi-user, multi-organization** persistent agent organization manager using:

- **OpenCode** as the agent runtime;
- **assistant-ui** as the conversation/tool presentation layer;
- a thin custom application for users, organizations, membership/authorization, agent identity, placement, memory, and access.

The first milestone should prove that this architecture can reproduce the parts of Codex Desktop we value without building another chat or agent framework.

---

## 1. Architectural Decisions

### 1.1 Multi-user tenancy is foundational

The first database/schema and API design must assume:

- many human users;
- many organizations;
- each user may belong to many organizations;
- each organization may have many users.

Do **not** model organizations as directly owned child records of a single user.

Use a many-to-many organization membership boundary from the start:

```text
User
  │
  └──< OrganizationMembership >── Organization
```

At minimum, a membership should be independently addressable and suitable for later authorization attributes.

Possible starting shape:

```text
User
----
id
identity/auth fields

Organization
------------
id
name
created_by_user_id
created_at

OrganizationMembership
----------------------
id
organization_id
user_id
role
status
created_at
```

`created_by_user_id` is provenance, not the authorization boundary. Access is determined through `OrganizationMembership`.

The MVP distinguishes owner/admin management from ordinary member access. Real authentication, owner bootstrap and additional users are required. The schema/API can later evolve toward:

```text
owner/admin
manager
member/user
viewer
```

without replacing the membership model.

Future authorization should also be able to support agent-level grants such as:

```text
OrganizationMember
    ↓
AgentAccessGrant
    ↓
specific Agent
```

and potentially scoped access to hosts, credential profiles, memory, or administrative settings.

These finer-grained policies are **not MVP requirements**, but every organization-owned resource must be tenant-scoped from the beginning so adding them later is an authorization change rather than a data-model rewrite.

### 1.2 OpenCode is the initial agent runtime

Use OpenCode rather than building directly around Codex App Server.

Reasons:

- mature, fully open-source runtime;
- persistent sessions;
- headless server/API;
- structured event stream;
- shell and file tools;
- MCP support;
- permissions and approvals;
- primary agents and subagents;
- broad model/provider support;
- ChatGPT/OpenAI authentication support;
- native `SKILL.md` support;
- compatible skill discovery patterns.

We are intentionally optimizing for a good agent runtime rather than preserving the native Codex harness.

### 1.3 assistant-ui is the presentation layer

Use **assistant-ui** to provide the conversational interface.

Prefer the first-party OpenCode integration:

```text
assistant-ui
    ↓
@assistant-ui/react-opencode
    ↓
OpenCode
```

The UI should present structured OpenCode events deterministically. No second LLM should be used to summarize tool output.

Desired default presentation:

- assistant messages expanded;
- reasoning compact/collapsed;
- shell commands summarized;
- stdout/stderr collapsed until requested;
- file changes shown as compact diff cards;
- approvals rendered explicitly;
- agent questions rendered as first-class interaction;
- nested/subagent work visible but not dominant.

### 1.4 Do not build an internal task system

The platform should not require an internal task object in order to interact with or dispatch work to an agent.

Projects remain free to use whatever work-management and collaboration systems fit them, including issue trackers, repositories, documents, direct chat, scheduled work, or custom tooling.

The agent interacts with those systems directly through CLI tools, APIs, MCP, or skills.

### 1.5 Organization is the primary abstraction

The core domain model is initially:

```text
Human User
   │
   └── OrganizationMembership
             │
             ↓
        Organization
        └── Department
            └── Agent
                ├── title
                ├── reports_to
                ├── runtime
                ├── host
                ├── model/provider
                ├── skills
                └── memory
```

Users and organizations are many-to-many. Reporting relationships between agent employees should be first-class rather than inferred from naming conventions.

---

## 2. Agent Model

A starting `Agent` record should remain intentionally small, but every agent is explicitly tenant-scoped.

Possible fields:

```text
id
organization_id
department_id

name
display_name
title
reports_to_agent_id

host_id
runtime_type
runtime_instance_id

provider
model
credential_profile_id

persona/instructions
skill_policy
memory_profile_id

status
last_seen_at
```

Avoid storing runtime-specific conversation state in our primary data model unless necessary. OpenCode should remain authoritative for its own sessions.

### Authorization boundary

Every organization-owned entity should carry or inherit an `organization_id` boundary, including at minimum:

- departments;
- agents;
- organization-scoped host registrations and placement grants;
- credential profiles;
- memory profiles;
- skill assignments/configuration;
- runtime registrations;
- organization-level settings.

API requests must authorize access through the requesting user's active `OrganizationMembership`.

Avoid relying on UI filtering as the security boundary.

Future per-agent access can be layered on top of membership without changing ownership:

```text
User
  ↓
OrganizationMembership
  ↓
AgentAccessGrant
  ↓
Agent
```

---

## 3. Runtime Isolation

Use **one Docker container per persistent agent**, with one home host. An agent is its identity, instructions, skills, tools, memory and reporting relationships; it is not a thread. Multiple native threads can execute concurrently inside that agent's boundary. Assign workspaces deliberately and use separate Git worktrees for concurrent repository work.

Persist the runtime home, workspaces and installed tooling across restart and container replacement. Default to broad access inside host-controlled container privileges, mounts and resource limits, including ordinary internet access and tool installation. Agents receive no Docker socket. Ops owns infrastructure, reachability and private-network controls.

Organization defaults, mandatory limits and authorized per-thread overrides configure native permissions. Avoid routine approval stops. Version desired/applied configuration: delivered restrictions apply before subsequent actions, while instructions/model changes take effect at safe turn boundaries. A future native macOS execution profile can give agents device access after the Docker MVP.

---

## 4. Skills

Skills are the main behavioral extension mechanism.

Support both categories OpenCode provides:

### Agent-selectable skills

Advertised to the model through their descriptions and loaded only when relevant.

Examples:

- code review;
- testing;
- GitHub;
- security review;
- documentation.

### Explicit-only skills

Use native OpenCode commands for workflows available to the user but absent from automatic skill discovery. Keep native skill loading and command execution with OpenCode.

Examples:

- grilling;
- Wayfinder;
- to-spec;
- handoff.

The application should manage **which skills belong to which employee**, but OpenCode should decide how those skills are loaded and executed.

Existing Codex/agent-team skills should be reused where practical rather than rewritten.

---

## 5. Memory

Hosts persist explicit, inspectable and editable agent memory in SQLite, exposed to authorized users and agents through host APIs and native tools. Memory is distinct from OpenCode transcripts. Repository/project documents remain canonical shared knowledge.

Memory edits require organization authorization and retain their attribution. Use the maintained Python MCP SDK for host-owned memory and collaboration tools when needed; do not build a skill dispatcher or another agent framework. Shared writable organization knowledge and semantic/vector retrieval are deferred.

---

## 6. External Work and Project Systems

The platform should remain intentionally agnostic about where projects track and organize work.

Possible systems include:

- GitHub Issues / pull requests;
- GitLab issues / merge requests;
- Jira;
- other project-management or ticketing systems;
- Markdown specs and repository-local planning files;
- direct conversations;
- custom internal systems;
- no formal tracker.

The agent organization layer should not assume any one of these is authoritative.

A representative engineering workflow might look like:

```text
external work item or request
    ↓
agent reads project context
    ↓
implementation / analysis / discussion
    ↓
project-native artifacts
    ↓
review / validation / completion
```

Depending on the project, those artifacts might be branches and pull requests, documents, tickets, deployments, messages, or something else entirely.

Our application only needs enough project/resource awareness to make relevant systems discoverable and accessible to employees.

Do not mirror external task systems into an internal issue tracker unless a future use case clearly requires it.

---

## 7. Multi-Host Design

One control plane manages **multiple autonomous backend agent hosts from the MVP**:

```text
React frontend → Python control plane
                         │ desired configuration / placement
                  ┌──────┴──────┐
             Python host A ↔ Python host B
                │   │             │   │
             agent containers   agent containers
```

The control plane owns users, organizations, memberships, desired policy/configuration, placement and discovery. Hosts own applied configuration, execution, credentials, memory, workspaces and supervision. Physical hosts may serve multiple organizations through isolated organization-scoped registrations and agents.

The local proof runs two independent host API instances on macOS with its existing Docker environment, each with its own identity, SQLite state and credential ownership. A second physical machine and Linux validation are deferred. Host administration and network reachability belong to ops, including private networks or Tailscale.

Agents and configured peer communication continue without the browser/control plane. Hosts deliver same-organization contributions directly with cached applied peer authorization, durable outbox/inbox receipts, retry and duplicate suppression. There is no implicit control-plane relay. Agents can discover/read existing threads and intervene at safe boundaries or start recipient threads. Organization-chart distance biases collaborator selection without granting authority.

Hosts preserve human/agent provenance and ordered delivery envelopes around native calls. Distinguish queued messages, safe-boundary steering and explicit stop. On restart reconcile native history, dispatch identity and effect evidence; automatically resume known-safe work and investigate uncertain effects before replay. Escalate only unresolved decisions.

---

## 8. Multiple Accounts

Credential profiles should be first-class and organization-scoped by default.

Example:

```text
Credential Profile: Work OpenAI
Credential Profile: Personal OpenAI
Credential Profile: Anthropic Work
```

Agents reference credential profiles rather than embedding credentials directly.

The runtime isolation strategy must prevent one employee from accidentally using another organization's account.

Each host owns its own login and refresh for each organization-scoped profile. Assigned local agents share that host authorization; multiple accounts for the same provider remain distinct profiles. Different hosts may use the same account through independent logins, never synchronized rotating credentials.

The host protects its credential database and serializes refresh. The approved narrow native OpenCode JS/TS authentication plugin obtains current access credentials from Python; refresh tokens remain on the host. Reject mismatched account/profile assignments and expose subscription-supported model choices.

---

## 9. MVP

The [approved implementation specification and seven-ticket backlog](https://github.com/jdip/Fesnyng/issues/10) owns executable acceptance. The retained full-system proof includes:

1. Two users and two organizations, shared membership, role enforcement and denied cross-organization access across HTTP, events, native runtime operations, artifacts, memory and peer delivery.
2. Persistent agents performing real repository work, editable memory, concurrent threads, native reusable skills and explicit-only commands.
3. Maintained assistant-ui Thread and companion components for messages, composer, Markdown, scrolling, actions, branches and generic tools; supported slots for actual Fesnyng-specific gaps.
4. Two local host APIs, each sharing independently refreshed profile credentials among its assigned Docker agents.
5. CEO-to-senior-to-existing-junior-thread collaboration, discovery, visible provenance and meaningful results returning to the initiator.
6. Browser/control-plane outages, direct peer queue/retry, host restart and container replacement with retained work, tooling, history and safe recovery.
7. Organization switching, searchable agents/threads, actionable activity and an additional reporting-chart view. Notify for useful results, failures or unresolved input rather than every internal event.

The native adapter proof found missing pre-existing requests on initial attachment and stale requests after another client answered during disconnection. Apply the approved narrow `patch-package` fix to react-opencode's existing interaction synchronization unless upstream has released an equivalent fix. Reconcile authoritative pending IDs by request kind/session, preserve errors and record patch provenance/removal criteria; do not replace its conversation runtime.

## 10. Deferred decisions

The resolved map records the completed architecture dialogue and live integration evidence. Keep future native macOS agents with device access, Windows, automatic agent cloning/migration, cross-host credential sync, shared writable organization knowledge, finer grants/superior-agent approvals, public signup/external identity providers and infrastructure fleet UI out of this MVP. Reopen the map for a substantive new design gap; routine implementation choices remain with the implementer.

---

## 11. Guiding Constraint

Before adding a new subsystem, ask:

> Is this something the organization layer must own, or can the agent runtime / skill / external project system already own it?

Prefer the thinner architecture whenever possible.
