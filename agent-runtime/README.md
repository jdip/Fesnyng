# Fesnyng agent runtime

This directory builds the pinned OpenCode runtime image used by Fesnyng agent hosts. It is an OpenCode server image, not a separate agent loop: the host owns container lifecycle and credentials, while OpenCode owns native sessions, model requests, tools, and skills. It does not establish a general preinstalled command-tool baseline for agents.

The image includes Git solely for the host's scoped, read-only thread-context lookup. It is not exposed as a control-plane mutation or a general host filesystem capability.

Build it from the repository root:

```bash
docker build -t fesnyng-agent:local agent-runtime
```

The image starts `opencode serve` on its internal port `4096`. Do not publish or run it as an independently administered agent service. Start an agent host from [the backend runtime instructions](../backend/README.md#install-an-agent-host); the host creates and labels each agent container, supplies its server password, and binds the native port to host loopback only.

## Host-managed OpenAI authentication

`host-auth.mjs` is a native OpenCode plugin for the pinned `opencode-ai` runtime. For every assigned agent, the host writes a mode-`0600` JSON file and sets `FESNYNG_AGENT_AUTH` to its path. The file contains only:

```json
{
  "broker": "http://host-reachable-agent-host",
  "key": "host-issued-agent-key",
  "profile_id": "assigned-profile-id"
}
```

The key selects an assigned host-local profile. The plugin sends it only to the host's `/credential` endpoint, validates that the returned profile matches the configured profile, and receives a short-lived access token plus account, expiry, residency, and generation metadata. Refresh credentials never enter the container.

The plugin disables native OpenAI login and refresh methods, sends model traffic only to `https://chatgpt.com/backend-api/codex/responses`, and rejects non-Responses requests and invalid broker replies. It has no fixture endpoint or configurable upstream subscription URL. The host performs device login and serialized token refresh; a container cannot perform either operation.

The configured broker URL must be reachable from the container. Configure this host-side value with `FESNYNG_AGENT_HOST_CREDENTIAL_URL`; it is independent for every host instance.

## Validate the runtime adapter

Install the locked Node dependencies and run the adapter checks:

```bash
npm ci --prefix agent-runtime
npm run check --prefix agent-runtime
```

The command runs ESLint, TypeScript checking for the JavaScript plugin, and its Node behavioral tests. Building the image or starting a host does not itself prove an authenticated provider request; that requires the separately authorized host and provider validation.
