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
The selected native OpenCode or Codex adapter owns session attachment and event
reconciliation.
Fesnyng supplies organization navigation, reporting relationships, settings,
explicit memory, attributable delivery history, and the authorized connection.
The reporting chart is the organization overview. Each entry in the organization
picker offers chart and authorized settings actions for that organization.

New Docker agents recommend `gpt-6-astra`; an existing agent keeps its selected
model until an authorized configuration change. An agent is persistent identity,
rules, tools, skills, and memory that can own multiple threads.

The sidebar can switch between **Projects** and **Employees**. Projects group
authorized employee threads without changing their native session, harness, or
workspace. Existing threads begin **Ungrouped**. Project details support thread
searching, employee filtering, grouping changes, and complementary new-thread
selection. A Project may record a repository and required default checkout, but
repository-backed thread creation stays unavailable until host workspace
preparation is configured; the UI does not create an empty substitute workspace.

## Harnesses and frozen history

New employees choose **OpenCode** (the default) or **Codex** in Agent settings.
For Codex, selecting a home host and authenticated credential profile loads the
account’s complete native model catalog. Discovery errors remain visible and can
be retried. The saved model is retained while discovery is unavailable.
**Default thinking level** offers the selected model’s supported efforts and
**Model default**. Saved changes apply to subsequent turns in new and existing
conversations after the usual safe configuration boundary; active turns continue
with their existing settings. Changing the model resets an incompatible effort
to Model default.
An organization can use both, but every thread retains the immutable harness
binding it received when created. The workspace selects that original renderer
and facade URL from the thread inventory, rather than from the employee's
current setting. This keeps mixed history readable after a harness change.

Changing an existing employee is intentionally separate from an ordinary save.
Choose the target harness, use **Switch to …**, and wait for the old history to
be captured and permanently frozen. The target configuration is then pending:
use **Apply selected harness** only after the control-plane selection is
recorded. If the browser lost the switch acknowledgement, or the host reports a
capturing recovery state, use **Retry harness switch** first. A `frozen` state
also needs Retry when the target selection is not yet recorded; when the current
harness already matches the target, use **Apply selected harness**. A pending
target does not offer a new-thread composer; wait until settings show `applied`
before starting work.

Frozen history appears in the **Frozen history** section. It retains messages,
tool output, diffs, contribution authorship, source-thread links, operator
outcome evidence, and personal read acknowledgements. It is permanently
read-only: no compose, retry, approval/reply, title/archive, regenerate, edit,
or native recovery controls are available. A frozen snapshot does not require
the old native container or native server to keep running, but it is stored by
the originating agent host. That host must be reachable to read the snapshot;
an unavailable host is shown as a read failure rather than replaced with new
native history.

Codex supports only the policy controls that the pinned App Server can enforce
exactly: organization default permission `allow`, with no mandatory permission
rules and no thread overrides. Unsupported Codex policy is left pending with
the host's visible reason; it is never silently weakened. Credential profiles
remain host-owned for both harnesses. The browser and agent containers receive
neither a refresh credential nor an unmanaged provider login.

Threads are ordered by their latest incoming human or other-agent message;
the thread agent's own replies and tool activity do not bump them. Threads with
no incoming messages use creation time. The list refreshes while the app is
visible and when returning to it, preserving the selected conversation and draft.
Use a thread's menu to pin or unpin it. Pinned threads appear above the remaining
threads, retaining incoming-message recency within each group. Pins belong only
to the signed-in user and persist across devices and sign-in sessions.
Only the selected agent expands its maintained thread list in the page sidebar.
All pins and the selected thread remain visible alongside the first six recent
unpinned threads. The plus on the selected agent's role row opens a new thread.
Show more adds another page; User preferences beside sign-out contains the
thread-list page size, saved across devices. One organization-wide title search above Agents finds
threads beyond the visible page and groups them by agent. Navigation preserves
unsent text, selected workflows and retry identity within the current workspace;
drafts are not stored on the server.

New threads receive an agent-generated title once during their first turn. An
explicit title or later manual rename takes precedence; a failed naming attempt
leaves the current title and never rewrites or compacts conversation history.
Project rows show the thread title above its agent name.

The active thread header keeps its editable title and Files action visible, with
repository and branch beneath. Expand Workspace details for Git changes, native
child-session count and workspace lifecycle controls. Git totals compare
tracked staged and unstaged workspace changes against HEAD; untracked files and
binary changes are counted separately. These are workspace changes, not native
session-summary totals. An unborn repository has no HEAD comparison; a detached
checkout is labelled Detached HEAD. Repository subtitles use the same per-thread
workspace source, including pinned entries. No repository denotes established
absence; Repository unavailable denotes a failed or unknown lookup. Background
processes remain Unavailable when native data cannot establish thread ownership.
Context loads on selection and refreshes on the header Refresh action and relevant
native updates, without an additional polling loop. Memory, settings and Refresh
use labelled icons; the sidebar remains the thread navigation and return path.

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
creating a second delivery. The header Files action opens the current workspace
for both harnesses. Files in each thread's menu opens that thread's authorized
workspace in the same resizable read-only panel, including when another
conversation is selected. Browse folders and breadcrumbs, filter and order the
current listing, inspect metadata, refresh, preview supported files, and download
their original bytes. Large and unsupported content has an explicit preview
state. Opening or closing the panel preserves the conversation and draft; the
resize separator supports keyboard control. Relative and root-relative report
links open the referenced workspace file in this panel; external web links keep
their normal behavior. Missing files show a preview error. Routine replies to the
viewing human omit redundant attribution; replies to other humans or agents retain
it. Completed tool/reasoning-only entries use compact previews with accessible
disclosure and action controls. Selection is preserved in the page fragment so a
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

### File-panel layout regression check

The DOM-based tests do not calculate browser layout. For changes to the Files
panel or conversation sizing, repeat this rendered check for OpenCode and Codex
at desktop and narrow (390 × 844) viewport sizes:

1. Open a conversation long enough to scroll, containing a workspace link to a
   text report of at least 100 lines. Type an unsent draft, then open the link.
2. Verify the preview stays visible after the directory listing finishes loading,
   including when that listing is delayed relative to the file response.
3. Check that the preview and composer bounding rectangles remain within the
   conversation rectangle. A loaded preview in the DOM is not sufficient: the
   grid row must not grow beyond the conversation's available height.
4. Scroll the preview and chat separately; verify each scroll position changes
   without moving the other. Close and reopen Files and verify the draft remains.
5. Switch threads and verify that old file contents are not shown in the newly
   selected workspace; check the existing missing-file error path too.

Use fictional files in a test workspace or the existing API test fixtures. Do not
publish real report contents. This catches the failure where a content-sized grid
row pushes the preview and composer below an overflow-clipped conversation.
