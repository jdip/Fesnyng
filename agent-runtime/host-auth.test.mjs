import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const moduleUrl = new URL("./host-auth.mjs", import.meta.url);

async function withConfiguration(configuration, action) {
  const directory = await mkdtemp(join(tmpdir(), "fesnyng-host-auth-"));
  const path = join(directory, "agent-auth.json");
  const original = process.env.FESNYNG_AGENT_AUTH;
  try {
    await writeFile(path, JSON.stringify(configuration), { mode: 0o600 });
    process.env.FESNYNG_AGENT_AUTH = path;
    await action();
  } finally {
    if (original === undefined) delete process.env.FESNYNG_AGENT_AUTH;
    else process.env.FESNYNG_AGENT_AUTH = original;
    await rm(directory, { recursive: true, force: true });
  }
}

test("loads an assigned host credential and forwards only to the official Responses endpoint", async () => {
  await withConfiguration(
    { broker: "http://127.0.0.1:8001", profile_id: "profile-a", key: "agent-key" },
    async () => {
      const originalFetch = globalThis.fetch;
      const calls = [];
      globalThis.fetch = async (input, init) => {
        const request = new Request(input, init);
        calls.push(request);
        if (new URL(request.url).pathname === "/credential") {
          return Response.json({
            profile_id: "profile-a",
            access: "short-lived-access",
            account_id: "account-a",
            expires: Date.now() / 1000 + 60,
            residency: "no_constraint",
            generation: 3,
          });
        }
        return new Response("ok");
      };
      try {
        const plugin = (await import(`${moduleUrl.href}?test=${Date.now()}`)).default;
        assert.equal(plugin.id, "fesnyng.host-auth");
        const server = await plugin.server();
        assert.deepEqual(server.auth.methods, []);
        const loader = await server.auth.loader();

        const response = await loader.fetch(
          "https://api.openai.com/v1/responses",
          {
            method: "POST",
            headers: {
              authorization: "Bearer stale-container-token",
              "ChatGPT-Account-Id": "wrong-account",
              "x-openai-internal-codex-residency": "wrong-residency",
            },
            body: "{}",
          },
        );

        assert.equal(await response.text(), "ok");
        assert.equal(calls.length, 2);
        assert.equal(calls[0].headers.get("authorization"), "Bearer agent-key");
        assert.equal(calls[1].url, "https://chatgpt.com/backend-api/codex/responses");
        assert.equal(calls[1].headers.get("authorization"), "Bearer short-lived-access");
        assert.equal(calls[1].headers.get("ChatGPT-Account-Id"), "account-a");
        assert.equal(calls[1].headers.get("x-openai-internal-codex-residency"), null);
      } finally {
        globalThis.fetch = originalFetch;
      }
    },
  );
});

test("rejects a host response for another assigned profile", async () => {
  await withConfiguration(
    { broker: "http://127.0.0.1:8001", profile_id: "profile-a", key: "agent-key" },
    async () => {
      const originalFetch = globalThis.fetch;
      globalThis.fetch = async () =>
        Response.json({
          profile_id: "profile-b",
          access: "short-lived-access",
          expires: Date.now() / 1000 + 60,
        });
      try {
        const plugin = (await import(`${moduleUrl.href}?test=${Date.now()}`)).default;
        const loader = await (await plugin.server()).auth.loader();
        await assert.rejects(
          loader.fetch("https://api.openai.com/v1/responses", { method: "POST", body: "{}" }),
          /Invalid host credential response/,
        );
      } finally {
        globalThis.fetch = originalFetch;
      }
    },
  );
});
