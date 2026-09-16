# Organization Docker access and shared resources

Docker is an optional administrator-configured employee capability, disabled by
default. Employees use ordinary Docker/Compose against an external engine.
Fesnyng does not run Docker-in-Docker, require another VM, or choose Compose
project names, ports, sharing rules or stack lifetimes.

## Dedicated engine configuration

Raw Docker access grants administrative control of the entire engine and its
host-mounted data. Allocate the engine to **one organization**, including every
other application using it. Labels and registration do not create isolation from
an engine administrator. The administrator owns this allocation.

On the organization work host, obtain the engine ID with
`docker info --format '{{.ID}}'`. Configure the agent-host process using the actual
organization and employee UUIDs:

```bash
export FESNYNG_AGENT_HOST_DOCKER_CAPABILITY='{
  "organization_id": "11111111-1111-4111-8111-111111111111",
  "engine_id": "administrator-verified-engine-id",
  "socket_path": "/var/run/docker.sock",
  "employee_ids": ["22222222-2222-4222-8222-222222222222"]
}'
```

The local Unix socket must be usable by the host API and mounted from that same
source path by the engine. The host's ordinary Docker context and this socket must
resolve to the exact configured engine ID. This configuration targets the Linux
organization work host; a desktop VM's forwarded and internal sockets can have
different paths. Do not assume a desktop socket can be mounted unchanged.

Enabling the capability refuses other retained or new organization bindings,
mismatched engines, and observed foreign-organization Fesnyng containers/volumes.
Legacy volumes without organization labels are accepted only when this host can
verify their exact retained home/workspace name, host/employee labels and durable
employee ownership. Other unattributed volumes remain uncertain and retained.
These checks catch mistakes; they cannot prove the absence of unrelated unlabeled
workloads or constrain a deliberate engine administrator.

The runtime declares Docker CLI 29.5.2, Compose 5.1.4 and Buildx 0.34.1 through a
digest-pinned official Docker client image stage in
[agent-runtime/Dockerfile](../agent-runtime/Dockerfile). Image updates own these
versions. Only clients are copied; the native harness entrypoint remains in use.

## Granting and revoking access

Only allowlisted employees receive the socket mount and `DOCKER_HOST`. Existing
containers need an **explicit rebuild** through their employee lifecycle to add
or remove it; restart cannot change mounts.

Stop the affected employee before changing its grant. Update the host setting,
restart the host API, and explicitly rebuild the employee using its retained
storage. Verify the capability and mounts before resuming. Removing configuration
alone does **not** revoke a socket already mounted in a running container.
Fesnyng blocks native admission on a detected grant/mount mismatch, but an
already-running process remains outside that gate. Stop and rebuild remain usable
to resolve mismatches. Preserve retained volumes and workspace data.

## Workspaces and Compose

New [host-managed workspaces](workspaces.md) are mounted at the same absolute path
inside the employee. A Compose file can use an ordinary relative bind such as
`./site:/usr/share/nginx/html:ro`; the engine sees the same files. Legacy directories
backed only by an employee volume do not acquire this property. Do not silently
migrate them or substitute a path containing different data.

Organization, department and employee skills decide naming, sharing and teardown.
Archive, workspace cleanup and employee restart do not tear down sibling stacks.
Inspect mounts before workspace removal: deleting a bind-mounted directory can
affect a service even while its container remains present.

## Resource inventory and explicit operations

The employee **Resources** view shows registered containers on its organization
host and the selected employee's capability. Discover a container and register
its full immutable ID with a name. Associate it with zero or more Projects and
host-local threads; several employees/threads may share it.

Inspection returns state, mounts, published ports, networks and selected Compose
metadata. It omits environment variables, credentials and daemon socket details.
Missing/unavailable records remain visible; a new container reusing a name is not
the registered container. Registration is metadata, not a security boundary.

Human operations require organization membership, a bound host and CSRF protection.
Employee MCP tools require their configured capability and an owned source thread.
Employees can attach/detach their own threads while preserving other associations.
Fesnyng-managed employee containers use their existing lifecycle controls.

Start, stop and removal are explicit operations against the registered engine and
container ID. Association changes invalidate older confirmation revisions.
Removal requires a stopped container and deletes its writable layer. It uses
neither force nor volume deletion; bind-mounted host files are retained.
**Unregister** removes metadata only. Volumes and networks appear in container
inspection and remain manageable through ordinary Docker/Compose; this facade
does not provide automatic stack teardown.

Published ports are discovery evidence, not a promise of network access. Service
registration, optional Tailscale routing and deployment guards belong to the next
approved slice. UFW incoming defaults alone do not protect Docker-forwarded traffic.
