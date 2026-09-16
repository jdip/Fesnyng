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
