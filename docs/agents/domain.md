# Domain documentation

[README.md](../../README.md) owns the enduring project purpose. [PLAN.md](../../PLAN.md) is the initial architecture source for the [primary product map](https://github.com/jdip/Fesnyng/issues/8); it incorporates the confirmed amendments. The [approved specification](https://github.com/jdip/Fesnyng/issues/10) owns MVP scope and acceptance. Read these before changing product terms, tenancy, agent relationships, runtime ownership or task-system scope.

One control plane manages multiple autonomous agent hosts. An agent is a persistent identity with configuration, tools, skills and memory; a thread is a native conversation belonging to that agent. A host can serve multiple organizations through isolated organization-scoped registrations. Credential profiles belong to organizations, with independent login/refresh ownership on each host.

Use established terms in code and explanations. A substantive terminology or architecture gap returns through `domain-modeling` to the associated map; routine implementation choices reuse approved coverage.
