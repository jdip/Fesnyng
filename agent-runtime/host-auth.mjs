import { readFile } from "node:fs/promises";

const RESPONSES_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses";

export default {
  id: "fesnyng.host-auth",
  server: async () => {
    const configuration = await loadConfiguration();
    return {
      auth: {
        provider: "openai",
        methods: [],
        loader: async () => ({
          apiKey: "fesnyng-host-managed",
          fetch: async (input, init) => {
            const original = new Request(input, init);
            if (!new URL(original.url).pathname.endsWith("/responses")) {
              throw new Error("Host auth only supports OpenAI Responses requests");
            }
            const reply = await fetch(new URL("/credential", configuration.broker), {
              headers: { authorization: `Bearer ${configuration.key}` },
              signal: AbortSignal.any([original.signal, AbortSignal.timeout(30_000)]),
              redirect: "error",
            });
            if (!reply.ok) {
              throw new Error(`Host credential unavailable (${reply.status})`);
            }
            const credential = await credentialResponse(reply, configuration.profile_id);
            const headers = new Headers(original.headers);
            headers.set("authorization", `Bearer ${credential.access}`);
            headers.delete("ChatGPT-Account-Id");
            if (credential.account_id) {
              headers.set("ChatGPT-Account-Id", credential.account_id);
            }
            headers.delete("x-openai-internal-codex-residency");
            if (credential.residency && credential.residency !== "no_constraint") {
              headers.set("x-openai-internal-codex-residency", credential.residency);
            }
            headers.delete("x-opencode-title");
            headers.set("originator", "opencode");
            headers.set("User-Agent", "opencode/1.18.30");
            return fetch(RESPONSES_ENDPOINT, {
              method: original.method,
              headers,
              body: await original.arrayBuffer(),
              signal: original.signal,
              redirect: "error",
            });
          },
        }),
      },
      "chat.params": async (input, output) => {
        if (input.model.providerID === "openai") {
          output.maxOutputTokens = undefined;
        }
      },
    };
  },
};

async function loadConfiguration() {
  const path = process.env.FESNYNG_AGENT_AUTH;
  if (!path) {
    throw new Error("FESNYNG_AGENT_AUTH is required");
  }
  let parsed;
  try {
    parsed = JSON.parse(await readFile(path, "utf8"));
  } catch {
    throw new Error("FESNYNG_AGENT_AUTH must contain valid JSON");
  }
  if (!isRecord(parsed) || !nonEmptyString(parsed.broker) || !nonEmptyString(parsed.key) || !nonEmptyString(parsed.profile_id)) {
    throw new Error("FESNYNG_AGENT_AUTH must contain broker, key, and profile_id");
  }
  let broker;
  try {
    broker = new URL(parsed.broker);
  } catch {
    throw new Error("Invalid host credential URL");
  }
  if (!["http:", "https:"].includes(broker.protocol) || broker.username || broker.password) {
    throw new Error("Invalid host credential URL");
  }
  return { broker, key: parsed.key, profile_id: parsed.profile_id };
}

async function credentialResponse(reply, profileId) {
  let credential;
  try {
    credential = await reply.json();
  } catch {
    throw new Error("Invalid host credential response");
  }
  if (
    !isRecord(credential) ||
    credential.profile_id !== profileId ||
    !nonEmptyString(credential.access) ||
    typeof credential.expires !== "number" ||
    credential.expires <= Date.now() / 1000 ||
    (credential.account_id !== null && credential.account_id !== undefined && !nonEmptyString(credential.account_id)) ||
    (credential.residency !== null && credential.residency !== undefined && !nonEmptyString(credential.residency))
  ) {
    throw new Error("Invalid host credential response");
  }
  return credential;
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}
