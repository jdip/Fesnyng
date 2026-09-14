# Fesnyng
A multi-user, open-source control plane for persistent AI agent organizations.

> **Project goal:** Build a lightweight, self-hosted, multi-user control plane for persistent organizations of AI agents, with a clean Codex-Desktop-like interaction experience, while staying deliberately unopinionated about how projects track and execute work.

## Overview

The project will provide a centralized place where multiple human users can create, share, define, organize, reach, and manage persistent AI-agent organizations.

A human user may belong to multiple organizations, an organization may have multiple human members, and each organization contains its own durable agent employees.

The platform should understand things like:

- who an agent is;
- its title, department, and reporting relationship;
- where it runs;
- which model/provider/account it uses;
- which skills and tools it has access to;
- its persistent memory and identity;
- whether it is available, busy, or offline;
- how a human or another agent can reach it.

It should **not** try to become the task-management system for every project.

Engineering work can continue to live naturally in GitHub Issues, branches, pull requests, and CI. Other projects may use Jira, Markdown plans, direct conversations, or other systems. Agents should interact with those systems as tools rather than forcing everything into a new proprietary task hierarchy.

## Core Principles

### Own the organization, not the work

The platform owns the persistent agent roster and organizational relationships.

It does not require work to be represented as internal tasks before an agent can act.

```text
Organization
├── Architect
│   └── Wayfinder
├── Developer
└── Reviewer

External systems
├── issue/task trackers
├── repositories and code review
├── CI/CD
├── project documentation
└── direct human requests
```

### Multi-user and multi-organization from day one

Multi-user tenancy is a foundational architectural requirement.

The system must support:

- multiple human user accounts;
- each user creating and belonging to multiple organizations;
- multiple users sharing access to the same organization;
- strict organization-level isolation of agents, credentials, memory, hosts, and configuration;
- an authorization model that can later grow into organization roles and per-agent access grants without changing the core tenancy model.

The relationship between users and organizations must be many-to-many rather than treating an organization as a private child object owned by one user.

Conceptually:

```text
User A ─────┐
            ├── Organization 1
User B ─────┘       ├── Architect
                    ├── Developer
                    └── Reviewer

User A ───────── Organization 2
User C ───────── Organization 3
```

The MVP distinguishes owner/admin management from member use. The schema and authorization boundaries leave room for later distinctions such as:

- organization owner/admin;
- manager;
- ordinary member/user;
- read-only access;
- access to only selected agent employees;
- access to specific hosts, credentials, or administrative settings.

These later permissions should be additive policy, not require redesigning identity or tenancy.

### Persistent agents

Agents are durable identities rather than disposable task executions.

Each agent should be able to have its own:

- persona and instructions;
- model/provider configuration;
- credentials/account;
- skills;
- tools/MCP access;
- workspace access;
- long-term memory;
- conversation/session history.

### Native agent runtime

The project does not implement another custom agent loop. Each employee selects
either the pinned **OpenCode** harness or the pinned **Codex App Server**
harness when it is created; OpenCode remains the default. The chosen native
harness owns sessions, model calls, tools, skills, and execution for that
employee. Fesnyng owns the organization, host lifecycle, browser authorization,
and durable harness binding around that native work.

Every host thread keeps the harness that created it. Changing an existing
employee's harness is a deliberate, safe operation: Fesnyng first captures a
complete host-owned snapshot and permanently freezes the old threads, then
applies the target harness. The old thread remains readable through its original
renderer and cannot be resumed, edited, replied to, or otherwise mutated.

### Clean interaction UX

The web interface should feel closer to the Codex Desktop app than a raw CLI transcript:

- assistant responses remain visually dominant;
- tool activity is visible but compact;
- command output is collapsed by default;
- diffs are easy to inspect;
- approvals and questions are first-class UI;
- reasoning/tool noise should not overwhelm the conversation.

The presentation layer uses **assistant-ui** with harness-specific native adapters.

### Skills remain the behavior layer

Agent behavior should stay modular and reusable through skills.

OpenCode supports on-demand skills and explicit invocation, allowing us to preserve workflows such as:

- Wayfinder;
- grilling;
- domain modeling;
- implementation;
- code review;
- security review;
- GitHub workflows.

The organization layer should not need to understand the internal logic of those workflows.

## Initial Architecture

```text
React application + assistant-ui
               │
      Python control plane
        │             │
 Python host A ↔ Python host B
      │                 │
 Agent containers    Agent containers
 (OpenCode or Codex) (OpenCode or Codex)
```

The application above the selected native harness is intentionally narrow.

### The application owns

- human users and authentication;
- organizations;
- organization memberships and authorization boundaries;
- departments;
- agent identities;
- titles and reporting relationships;
- agent-to-host placement;
- model/provider/account assignment;
- skill assignments;
- memory configuration;
- availability/status;
- delegation policy;
- authentication and access to the management UI.

### The selected native harness owns

- agent execution;
- conversations and sessions;
- model calls;
- tool calls;
- shell execution;
- file edits;
- permissions;
- MCP;
- skills;
- subagents;
- compaction/context management.

### External systems own their own work

The platform should remain agnostic about where work is tracked and coordinated.

A project may use:

- GitHub Issues and pull requests;
- GitLab issues and merge requests;
- Jira;
- another task or ticket system;
- repository-local Markdown/spec files;
- direct interactive conversations;
- custom internal systems;
- no formal task tracker at all.

Agents should interact with those systems directly through their available tools, APIs, CLIs, MCP servers, or skills rather than requiring work to be mirrored into an internal task database.

## Example Organization

```text
Example Organization
└── CTO
    ├── Product Architect
    │   └── Wayfinder
    ├── Developer
    └── Reviewer
```

Each employee can be a persistent OpenCode- or Codex-backed agent with its own
identity, skills, account, and memory. A single organization may use both
harnesses, while each thread remains bound to the harness that originally
created it.

## Non-Goals

At least initially, this project is **not** intended to be:

- a replacement for existing issue/task trackers;
- another autonomous task orchestration framework;
- a new agent protocol;
- a new LLM chat framework;
- a custom coding-agent runtime;
- a repository management system.

## Public Repository

This project is intended to be developed as a public repository from the beginning.

Documentation, examples, fixtures, screenshots, sample organizations, sample agents, and default configuration should therefore use generic or fictional data rather than real company names, internal systems, credentials, infrastructure details, customer information, or other identifying operational context.

Public-safe examples should be the default rather than something cleaned up later before release.

## Status

The local application is implemented against the [approved MVP specification](https://github.com/jdip/Fesnyng/issues/10). [Development instructions](docs/development.md) describe the runnable components and checks, and the [local retained-MVP guide](docs/local-mvp.md) starts one control plane, two local hosts, Docker agents, and the browser workspace. [Issue #18](https://github.com/jdip/Fesnyng/issues/18) records retained end-to-end acceptance and delivery evidence. [PLAN.md](PLAN.md) remains the initial architecture source, amended by the confirmed primary map.

The first implementation target is a minimal proof of concept with:

1. multiple human users;
2. users able to create multiple organizations;
3. organizations shareable with multiple users through explicit membership;
4. strict organization-scoped access boundaries;
5. a small reporting hierarchy inside an organization;
6. persistent OpenCode- or Codex-backed agents;
7. assistant-ui as the conversation surface;
8. per-agent skills and identity;
9. direct interactive sessions without requiring a task object.

The full-system MVP uses one control plane and two independent local Python agent-host APIs with one Docker container per persistent agent. Owner/admin and member roles ship initially; finer per-agent grants are deferred. Hosts retain execution, credentials and applied peer communication while the control plane is unavailable.
