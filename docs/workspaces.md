# Thread workspaces

A Project can name a repository and an explicit default checkout branch. Starting
a thread in that Project prepares a separate Git worktree and working branch for
the employee. The starting branch can be overridden for that new thread. Fesnyng
fetches the selected branch; a missing branch or failed authentication stops
preparation instead of falling back to the remote's default branch.

Each persistent employee can work across several repositories and threads. Moving
a thread between Projects or changing a Project's defaults does not change an
existing thread's directory, repository or branch. A thread without a repository
uses an ordinary directory.

## Host storage

Set `FESNYNG_AGENT_HOST_WORKSPACE_ROOT` to an absolute directory on the work host.
If omitted, the agent host uses `workspaces` under its resolved state directory.
The host scopes storage by organization and employee. It mounts each employee's
directory into that employee container at the **same absolute path**. The Docker
daemon must see that host path; a remote daemon with unrelated storage is not a
supported substitute. For local Docker virtual machines, choose a directory the
virtual machine shares with the host.

Keep this directory durable and backed up together with the agent-host database
and retained runtime volumes. Do not point several independent host instances at
the same storage. Do not remove repository metadata separately from its linked
worktrees. Different threads use independent worktrees, while repository storage
is reused only within the employee's organization scope.

The old `/workspace` named volume remains mounted. Existing native mappings keep
their original directories and history; this feature does not move or delete
them. A container created before shared-path storage was configured needs the
existing safe container replacement operation to acquire its new mount. Retain
the old volumes and complete the host's admission and checkpoint checks. Merely
restarting an old container cannot add a Docker mount. An unavailable mount is a
preparation error, not permission to create a repository thread in empty storage.

## Administrator Git authentication

Preparation runs Git inside the employee container, using its persistent
`/home/agent` home and standard Git authentication. It does not use model-provider
credentials. The administrator provisions a credential helper or SSH setup for
that employee, with only the repository permissions that employee needs.

Supported patterns include:

- HTTPS with a noninteractive Git credential helper configured in the employee's
  Git configuration. The helper and its credential source must be available
  inside that container.
- SSH with an employee-specific private key, trusted `known_hosts` and SSH
  configuration under `/home/agent/.ssh`. The runtime image includes OpenSSH.
  Provision trust in advance; preparation cannot answer a host-key or passphrase
  prompt. Use a noninteractive key/agent setup appropriate to the installation.

Keep credential files restricted and outside repository workspaces. Do not put
tokens or passwords into a Project URL, issue, browser field or log. Projects
accept credential-free HTTP(S) and SSH repository locators; SSH usernames are
allowed. Credentials retain their actual Git-server scope: Fesnyng does not turn
an engine or repository administrator credential into narrower access.

Before enabling a Project, the administrator should verify `git ls-remote` for
the exact repository and `refs/heads/<checkout>` from the same employee container
with terminal prompting disabled. Confirm the branch exists and the configured
identity can fetch it. Record only a sanitized success/failure result. Then create
a thread through Fesnyng to verify the complete preparation and native handoff.

## Creation and recovery

Fesnyng reserves a directory and fresh working branch before creating the native
thread. A creation identifier ties retries to that intended thread. A verified
native receipt is reused; a failed Project assignment leaves the native thread
discoverable and allows grouping to be retried separately.

Retries retain the repository and checkout selected when preparation was first
reserved. Later Project edits apply to new threads. Retrying the original request
does not adopt a changed Project default, and changing the request requires a new
creation identifier.

If the native creation result is uncertain, inspect/recover that request using
the same identifier. Fesnyng must establish the native identity before returning
a successful receipt; it must not silently create a second thread. Preserve the
reserved workspace and host state while investigating. Starting a different new
thread is a separate intent, not recovery of the previous request.

Codex initialization records a factual, application-owned workspace context item
in native history, then verifies that the same thread can resume with the expected
permissions. This makes the thread durable before its first user turn without
running the model. Recovery checks for that durable receipt before attempting
initialization. Unverifiable outcomes retain their original reservation and
remain uncertain.

Workspace removal and replacement have their own authorized lifecycle. Project
archive, thread archive, and container restart do not clean up files or decide
when an organization's work should be discarded.

## Inspect and clean up deliberately

Open a thread's Workspace panel, or the Workspaces view for its Project or
employee, to inspect its location, repository, branch and cleanup eligibility.
Unavailable evidence is shown explicitly. Legacy directories without a verified
managed-workspace binding remain usable, but cannot be removed through these
operations. Inspection does not migrate them or delete their retained volumes.

A failed safety inspection disables cleanup without changing the thread's
execution state. An uncertain removal or replacement is different: the host
retains its verified history and blocks execution until the workspace outcome
can be verified. Do not delete retained state to clear that restriction.

Organization members can use the workspace controls for employees they can
access. An employee's authenticated MCP tools expose the same host-owned
operations for its own mapped threads: `workspace_list`, `workspace_inspect`,
`workspace_remove`, `workspace_discard` and `workspace_replace`. The tools do not
accept another employee identity or an arbitrary filesystem path.

Ordinary removal requires current evidence that execution is inactive, history
is safely retained, ownership is verified, and no files or unpushed commits
would be lost. An ordinary directory must be empty. A clean Git worktree alone
is insufficient: unpushed commits or an unverifiable upstream prevent ordinary
removal. The host checks the current filesystem and admission state when it
executes the request, so an earlier safe inspection cannot authorize later
changes.

Discard is a separate explicit operation for the loss shown in the current
inspection. It still refuses active work, stale evidence and unverified
ownership or history. Neither operation removes the shared repository, another
workspace, service data or Docker stacks. Organization, department and employee
workflows decide when cleanup is appropriate; Fesnyng does not schedule it.

After removal, the conversation keeps its native identity, Project association
and readable captured history. Continuing requires explicit replacement at the
same directory. Replacement preserves the retained working branch and thread;
it does not silently start a new conversation or apply a changed Project default.
Permanently frozen threads stay frozen and cannot be made writable by replacing
a workspace. Archive and container restart do not trigger cleanup.
