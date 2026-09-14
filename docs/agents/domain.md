# Domain documentation

[README.md](../../README.md) owns the enduring project purpose. [PLAN.md](../../PLAN.md) is the initial architecture source for the [primary product map](https://github.com/jdip/Fesnyng/issues/8); it incorporates the confirmed amendments. The [approved specification](https://github.com/jdip/Fesnyng/issues/10) owns MVP scope and acceptance. Read these before changing product terms, tenancy, agent relationships, runtime ownership or task-system scope.

One control plane manages multiple autonomous agent hosts. An agent is a persistent identity with configuration, tools, skills and memory; a thread is a native conversation belonging to that agent. A host can serve multiple organizations through isolated organization-scoped registrations. Credential profiles belong to organizations, with independent login/refresh ownership on each host.

Each agent selects OpenCode or Codex App Server as its execution harness; agents in one organization may select different harnesses. A thread keeps the harness and native identity that created it. The [resolved harness map](https://github.com/jdip/Fesnyng/issues/82) and [approved specification](https://github.com/jdip/Fesnyng/issues/83) extend the initial architecture.

Switching harnesses permanently freezes existing threads after their history has been verified and retained by the owning host. A frozen thread remains readable with its original presentation even when its former harness is unavailable, provided its host is available. Switching back creates new threads and never thaws old ones. Freezing is distinct from archiving, which only changes navigation. Neither operation moves agent identity, memory or workspace ownership to the control plane.

A department is an organization-scoped grouping with an optional parent department and optional agent head. An agent may belong to one department or remain unassigned. Department nesting and agent reporting are separate relationships: reporting may cross department boundaries and does not alter permissions or collaboration routing.

Use established terms in code and explanations. A substantive terminology or architecture gap returns through `domain-modeling` to the associated map; routine implementation choices reuse approved coverage.
