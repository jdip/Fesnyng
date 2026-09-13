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

Owners and admins manage agents, organization permissions, membership, and named
credential profiles. Host registration and networking remain installation/ops
work. A profile is independently authorized on each host; the UI receives device
login instructions and status, never host management or refresh credentials.
Members can use agents, inspect/edit memory, and set allowed thread overrides.
Mandatory organization limits retain precedence.

Thread activity links an agent contribution to its originating thread. Activity
reports results, failures, and pending input while preserving healthy-host
results when another host is unavailable. Thread permissions expose desired and
applied revisions. The steering/workflow panel submits through the existing
host-owned durable queue; an uncertain outcome must be investigated before
recording its resolution. The Files panel reads text artifacts relative to the
selected thread's workspace. Selection is preserved in the page fragment so a
reload can reattach to the same authorized conversation.

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
the retained full-system MVP acceptance.
