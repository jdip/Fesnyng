# Fesnyng agent runtime

This directory builds the agent image with OpenCode and Codex App Server versions pinned in `package.json` and `package-lock.json`. The host selects one harness per employee and owns container lifecycle and credentials. The selected native harness owns sessions, model requests, tools, and skills. The image does not establish a general preinstalled command-tool baseline for agents.

The image includes Git solely for the host's scoped, read-only thread-context lookup. It is not exposed as a control-plane mutation or a general host filesystem capability.

Build it from the repository root:

```bash
docker build -t fesnyng-agent:local agent-runtime
```

The entrypoint starts `opencode serve` or `codex app-server` on internal port `4096`, according to the host-supplied `FESNYNG_RUNTIME_TYPE`. Start an agent host from [the backend runtime instructions](../backend/README.md#install-an-agent-host); the host creates and labels each agent container, supplies its private native server credential, and binds the native port to host loopback only.

The runtime type is immutable for each mapped thread. Existing employees switch
only through the host's freeze-then-apply workflow: the host captures verified
native history and ancestry, permanently freezes the source mappings, rebuilds
the container using the selected harness, and only then accepts new threads.
Frozen history is served by the host snapshot facade; this image must not try to
resume, mutate, or reinterpret a frozen thread.

Codex uses a capability-authenticated WebSocket. The host initializes its experimental API and supplies access tokens through native `chatgptAuthTokens` login. Native token rejection requests return to the same host-owned profile broker, including rejection before expiry. Refresh credentials stay on the host. Managed Codex instructions and skills live under `/home/agent/.codex`; thread working directories and retained files live under `/workspace`.

The Codex adapter accepts only the policy it can verify: Fesnyng's default
permission must be `allow`, with no mandatory permission rules and no per-thread
overrides. The host rejects an unsupported Codex configuration before freezing an
old harness. The Docker container and its scoped mounts remain the execution
boundary for the App Server's required `danger-full-access` sandbox setting.

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

After building the image, the credential-free Codex integration checks exercise
Fesnyng dispatch, interruption, persisted native history, reconnection, and
OpenCode-to-Codex-to-OpenCode permanent thread freezing:

```bash
FESNYNG_CODEX_DOCKER_TESTS=true FESNYNG_CODEX_TEST_IMAGE=fesnyng-agent:local \
  uv run --locked --project backend pytest \
  backend/tests/test_codex_docker_integration.py \
  backend/tests/test_harness_switch_docker.py -q -s
```

The normal OpenCode configuration, replacement, and scoped-file test uses the
same image:

```bash
FESNYNG_DOCKER_TESTS=true uv run --locked --project backend \
  pytest backend/tests/test_docker_integration.py -q -s
```

Set `FESNYNG_CODEX_TEST_IMAGE` when testing a separately tagged image.
Successful checks remove their isolated container, volumes and state. Failed
Codex and harness-switch checks stop their container and retain diagnostic state;
a failed OpenCode check may leave its task-owned container running for inspection
before cleanup. These checks do not make an authenticated provider request; a
ChatGPT profile login and its host-side evidence remain separate.
