# Organization workspace

This React/Vite application connects to the separate Python control plane. Run
`npm ci` and `npm run dev` in this directory. `FESNYNG_CONTROL_PLANE_URL` selects
the control-plane origin for Vite's `/api` proxy. Configure the same browser
origin in the control plane's allowed-origin setting; see
[backend setup](../backend/README.md). Production requires an HTTPS reverse proxy
for the static build and Python API, with the same authenticated origin.

Sign in with an installation account, choose an organization, and open an agent.
The maintained assistant-ui Thread and ThreadList own conversation rendering,
Markdown, composer, actions, branches, scrolling, reasoning, and generic tools.
The native OpenCode adapter owns session attachment and event reconciliation.
Fesnyng supplies organization navigation, reporting relationships, settings,
explicit memory, attributable delivery history, and the authorized connection.
The reporting chart is the organization overview. Its compact icon and the
manager-only organization settings icon sit above the signed-in user in the
sidebar.

New Docker agents recommend `gpt-6-astra`; an existing agent keeps its selected
model until an authorized configuration change. An agent is persistent identity,
rules, tools, skills, and memory that can own multiple threads.

Threads are ordered by their latest incoming human or other-agent message;
the thread agent's own replies and tool activity do not bump them. Threads with
no incoming messages use creation time. The list refreshes while the app is
visible and when returning to it, preserving the selected conversation and draft.
Use a thread's menu to pin or unpin it. Pinned threads appear above the remaining
threads, retaining incoming-message recency within each group. Pins belong only
to the signed-in user and persist across devices and sign-in sessions.
Only the selected agent expands its maintained thread list in the page sidebar.
All pins and the selected thread remain visible alongside the first six recent
unpinned threads. Show more adds another page; Thread list settings saves a
personal page size across devices. One organization-wide title search finds
threads beyond the visible page and groups them by agent. Navigation preserves
unsent text, selected workflows and retry identity within the current workspace;
drafts are not stored on the server.

Owners and admins manage agents, organization permissions, membership, and named
credential profiles. Host registration and networking remain installation/ops
work. A profile is independently authorized on each host; the UI receives device
login instructions and status, never host management or refresh credentials.
Members can use agents, inspect/edit memory, and set allowed thread overrides.
Mandatory organization limits retain precedence.

Thread and agent indicators distinguish unread completed results from unresolved
attention. Read state is personal and persists across devices; a result is read
only when its rendered content is visible. Questions and approvals remain until
answered, and terminal failures have an explicit personal “Mark failure handled”
action that never repeats execution. Refresh failures preserve known indicators
and expose a retry notice. Conversation content retains contribution authorship, source-thread links and
contextual investigation controls. Unmatched delivery failures and resolution
evidence remain accessible beside the conversation; no duplicate activity feed
or automatic replay is introduced. Thread permissions in each thread's menu open
the existing editor for that thread and expose desired and applied revisions,
without changing the active conversation or its draft. While an agent is working, the same conversation composer
steers on Enter; Queue remains a secondary action and Stop stays beside it. Type
`/` in that composer to choose a configured explicit workflow. A rejected or
uncertain submission retains its draft and selected workflow for retry without
creating a second delivery. Files in each thread's menu opens that thread's
authorized workspace in a resizable read-only panel, including when another
conversation is selected. Browse folders and breadcrumbs, filter and order the
current listing, inspect metadata, refresh, preview supported files, and download
their original bytes. Large and unsupported content has an explicit preview
state. Opening or closing the panel preserves the conversation and draft; the
resize separator supports keyboard control. Selection is preserved in the page fragment so a
reload can reattach to the same authorized conversation.

The sidebar identifies the current organization with its configured icon and name.
Its compact menu supports keyboard selection and organization creation. Owners
and administrators can choose an emoji, upload a validated PNG/JPEG/WebP image,
or restore initials in organization settings. Image uploads are bounded and
normalized by the control plane; organization membership governs icon access.

The initial composer accepts text; file/image/PDF submission is disabled rather
than discarding attachments. Native edit/write diff presentation uses a narrow
supported tool slot; all other generic tools use the maintained ToolFallback.
Native questions use the adapter's pending-request hooks and preserve failures
without fabricating replies. Permission actions offer one-time allowance or
rejection; lasting grants are managed through the thread/organization policy.

The exact maintained UI source and license live in
[src/components/assistant-ui](src/components/assistant-ui/).
The [adapter patch](patches/README.md) documents upstream provenance and removal
conditions. `npm ci` applies it with `--error-on-fail`; drift fails installation.
Dependencies and their versions belong in `package.json` and `package-lock.json`.

Run `npm run check` for lint, behavior tests, types, and production build. Use the
[repository check](../docs/development.md) before delivery, and verify rendered
flows against real Python hosts and Docker execution. Tests alone do not establish
the retained full-system MVP acceptance. The [local retained-MVP guide](../docs/local-mvp.md)
documents browser login, host-local profile login, and the complete local startup
and recovery sequence; acceptance remains active in issue #18.
