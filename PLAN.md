# Starting Architecture Plan

This is a starting point. It is *not* gospel.

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

The initial product may use a very small role set, even a single effective member role, but the schema/API must be able to evolve toward:

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
- hosts;
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

Start with one OpenCode runtime/service per credential or isolation boundary rather than assuming every employee must have its own container.

We should test whether OpenCode's native agent/session model is sufficient to isolate durable employees cleanly.

Potential boundaries include:

- work organization vs personal organization;
- separate OpenAI accounts;
- separate hosts;
- highly privileged agents;
- experimental/untrusted agents.

Do not introduce Docker-per-agent unless it solves a concrete security or dependency problem.

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

Available to the user but not advertised for automatic model selection.

Examples:

- grilling;
- Wayfinder;
- to-spec;
- handoff.

The application should manage **which skills belong to which employee**, but OpenCode should decide how those skills are loaded and executed.

Existing Codex/agent-team skills should be reused where practical rather than rewritten.

---

## 5. Memory

Long-term memory remains an architectural area to design separately.

Initial principle:

```text
OpenCode session history
    = conversational/task context

Agent memory
    = durable employee-specific knowledge

Repository/project docs
    = canonical shared project knowledge
```

Do not overload conversation history as permanent memory.

The memory implementation should remain replaceable and should not force the project into a larger agent framework such as Hermes.

Possible later options:

- structured filesystem memory;
- QMD/semantic retrieval;
- lightweight vector memory;
- shared organization/project knowledge stores.

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

The organization should eventually support agents running across multiple machines.

Conceptually:

```text
                   Org UI
                     │
          ┌──────────┼──────────┐
          │          │          │
       Linux VM    Mac Mini    Server
          │          │          │
      OpenCode     OpenCode   OpenCode
```

The control plane should know:

- which host owns an agent/runtime;
- host availability;
- how to reach the OpenCode server;
- runtime status.

Initial implementation can target one host first.

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

Secrets should remain outside the normal application database wherever practical.

---

## 9. MVP

### Phase 1 — Establish tenancy and prove the interaction layer

Start with the correct tenancy model even in the prototype.

Build a small web prototype that:

1. supports more than one human user;
2. lets a user create more than one organization;
3. lets an organization be shared with another user through `OrganizationMembership`;
4. verifies that a user cannot access an organization they are not a member of;
5. connects assistant-ui to one OpenCode server for an organization-scoped agent;
6. starts/resumes a session;
7. renders messages;
8. renders shell/tool activity compactly;
9. renders file changes/diffs;
10. handles permission requests;
11. handles interactive questions;
12. verifies that tool output can stay collapsed by default.

This phase answers two foundational questions:

> Is the user/organization/membership boundary correct enough to grow into finer-grained authorization later?

> Can OpenCode + assistant-ui reproduce the interaction experience we like from Codex Desktop?

### Phase 2 — Persistent employee

Add one durable employee with:

- name;
- title;
- persona;
- assigned skills;
- provider/model;
- persistent OpenCode session access.

Validate Matt Pocock-style explicit and auto-invoked skills.

### Phase 3 — Organization experience

Expand the tenancy model into the full organization experience:

- organization switcher;
- member management;
- departments;
- `reports_to`;
- org-chart UI;
- agent roster/status;
- click-through from an employee to its conversation.

Keep authorization centralized so later role-based and per-agent grants can be introduced without rewriting each feature.

### Phase 4 — Multiple employees/accounts

Add:

- several persistent employees;
- separate credential profiles;
- different models/providers;
- employee-specific skills;
- durable agent memory.

### Phase 5 — Multiple hosts

Add host registration and remote OpenCode instances.

---

## 10. Open Questions

These should be answered through prototypes rather than prematurely designed around.

1. Does GPT-5.6 running through OpenCode perform comparably enough to native Codex for our workflows?
2. How should durable employee identity map onto OpenCode's native agent/session model?
3. Do we need one OpenCode server per employee, per credential profile, per host, or something in between?
4. How should employee memory be stored and retrieved?
5. How much organization context should automatically enter an employee's prompt?
6. Should managers have direct runtime-level delegation abilities, or should delegation itself be implemented as a skill/tool?
7. What is the cleanest multi-host transport and authentication model?
8. How much of assistant-ui's OpenCode adapter will need customization to reach the desired Codex-Desktop-like presentation?
9. How should voice interaction eventually attach to an existing employee/session?
10. What minimal organization role model should ship first: owner/member, admin/member, or another small set?
11. Should per-agent grants be allow-list based, deny-list based, or inherited from organization roles when introduced?
12. Which resources besides agents will eventually need finer-grained grants (hosts, credential profiles, memory, settings)?

---

## 11. Guiding Constraint

Before adding a new subsystem, ask:

> Is this something the organization layer must own, or can the agent runtime / skill / external project system already own it?

Prefer the thinner architecture whenever possible.
