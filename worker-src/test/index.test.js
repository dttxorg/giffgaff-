import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import test, { afterEach, beforeEach } from "node:test";

import worker, { getApiBase } from "../src/index.js";

const TOKEN = "real_public_token_1234567890";
const ORIGINAL_FETCH = globalThis.fetch;

let cacheEntries;
let waitUntilPromises;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(handler) {
  globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    return handler(url, init);
  };
}

async function requestWorker(env = { API_BASE: "https://gg.6667766.xyz" }) {
  const response = await worker.fetch(
    new Request(`https://card.6667766.xyz/p/${TOKEN}`),
    env,
    { waitUntil(promise) { waitUntilPromises.push(promise); } },
  );
  await Promise.all(waitUntilPromises);
  return response;
}

beforeEach(() => {
  cacheEntries = new Map();
  waitUntilPromises = [];
  globalThis.caches = {
    default: {
      async match(request) {
        const response = cacheEntries.get(request.url);
        return response ? response.clone() : undefined;
      },
      async put(request, response) {
        cacheEntries.set(request.url, response.clone());
      },
    },
  };
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  delete globalThis.caches;
});

test("getApiBase accepts only configured http/https URLs", () => {
  assert.equal(getApiBase({ API_BASE: " https://gg.6667766.xyz/// " }), "https://gg.6667766.xyz");
  assert.throws(() => getApiBase({}), /not configured/);
  assert.throws(() => getApiBase({ API_BASE: "ftp://gg.6667766.xyz" }), /http or https/);
  assert.throws(() => getApiBase({ API_BASE: "not a url" }));
});

for (const apiBase of [
  "https://gg.6667766.xyz",
  "https://gg.6667766.xyz/",
  "https://gg.6667766.xyz////",
]) {
  test(`normalizes API_BASE and calls exact origin paths: ${apiBase}`, async () => {
    const calls = [];
    installFetch((url) => {
      calls.push(url);
      if (url.endsWith("/version")) return jsonResponse({ public_version: 1 });
      return new Response("<html>customer</html>", { status: 200 });
    });

    const response = await requestWorker({ API_BASE: apiBase });

    assert.equal(response.status, 200);
    assert.deepEqual(calls, [
      `https://gg.6667766.xyz/api/public/${TOKEN}/version`,
      `https://gg.6667766.xyz/p/${TOKEN}`,
    ]);
    assert.ok(calls.every((url) => !new URL(url).pathname.startsWith("//")));
    assert.equal(response.headers.get("X-Public-Card-Worker"), "7");
    assert.equal(response.headers.get("X-Origin-Stage"), "page");
    assert.equal(response.headers.get("X-Origin-Status"), "200");
  });
}

test("missing API_BASE returns an explicit configuration error", async () => {
  let fetchCalled = false;
  installFetch(() => {
    fetchCalled = true;
    throw new Error("must not be called");
  });

  const response = await requestWorker({});

  assert.equal(response.status, 500);
  assert.equal(await response.text(), "Worker configuration error");
  assert.equal(response.headers.get("X-Worker-Error"), "api-base");
  assert.equal(response.headers.get("X-Public-Card-Worker"), "7");
  assert.equal(fetchCalled, false);
});

test("a successful version lookup continues to the public page", async () => {
  const calls = [];
  installFetch((url) => {
    calls.push(url);
    if (calls.length === 1) return jsonResponse({ public_version: 7 });
    return new Response("real customer page", { status: 200 });
  });

  const response = await requestWorker();

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "real customer page");
  assert.equal(calls.length, 2);
  assert.equal(response.headers.get("X-Cache-Version"), "7");
});

test("version 404 remains a 404", async () => {
  installFetch(() => new Response("missing", { status: 404 }));

  const response = await requestWorker();

  assert.equal(response.status, 404);
  assert.equal(await response.text(), "Not found");
  assert.equal(response.headers.get("X-Origin-Stage"), "version");
  assert.equal(response.headers.get("X-Origin-Status"), "404");
});

for (const status of [401, 500]) {
  test(`version ${status} becomes a 502 origin error`, async () => {
    installFetch(() => new Response("upstream details must not leak", { status }));

    const response = await requestWorker();

    assert.equal(response.status, 502);
    assert.equal(await response.text(), "Origin error");
    assert.equal(response.headers.get("X-Origin-Stage"), "version");
    assert.equal(response.headers.get("X-Origin-Status"), String(status));
  });
}

test("page 404 remains 404 while other page failures become 502", async (t) => {
  for (const [originStatus, workerStatus] of [[404, 404], [403, 502], [500, 502]]) {
    await t.test(String(originStatus), async () => {
      installFetch((url) => {
        if (url.endsWith("/version")) return jsonResponse({ public_version: 1 });
        return new Response("origin details", { status: originStatus });
      });

      const response = await requestWorker();

      assert.equal(response.status, workerStatus);
      assert.equal(response.headers.get("X-Origin-Stage"), "page");
      assert.equal(response.headers.get("X-Origin-Status"), String(originStatus));
    });
  }
});

test("first page request is MISS and the next request is HIT", async () => {
  let versionCalls = 0;
  let pageCalls = 0;
  installFetch((url) => {
    if (url.endsWith("/version")) {
      versionCalls += 1;
      return jsonResponse({ public_version: 1 });
    }
    pageCalls += 1;
    return new Response("cached customer page", { status: 200 });
  });

  const first = await requestWorker();
  waitUntilPromises = [];
  const second = await requestWorker();

  assert.equal(first.headers.get("X-Cache"), "MISS");
  assert.equal(second.headers.get("X-Cache"), "HIT");
  assert.equal(first.headers.get("Cache-Control"), "no-store, max-age=0");
  assert.equal(second.headers.get("Cache-Control"), "no-store, max-age=0");
  assert.equal(second.headers.get("X-Public-Card-Worker"), "7");
  assert.equal(second.headers.get("X-Version-Cache"), "FRESH");
  const [storedUrl, storedResponse] = [...cacheEntries.entries()].find(
    ([url]) => url.includes("?v="),
  );
  assert.ok(storedUrl.includes("&worker=7"));
  assert.equal(storedResponse.headers.get("Cache-Control"), "public, max-age=2592000");
  assert.equal(versionCalls, 1);
  assert.equal(pageCalls, 1);
});

test("stale version metadata serves an existing page during origin outage", async () => {
  installFetch((url) => {
    if (url.endsWith("/version")) return jsonResponse({ public_version: 3 });
    return new Response("stale-capable page", { status: 200 });
  });
  const first = await requestWorker();
  assert.equal(first.status, 200);

  const [metadataUrl, metadataResponse] = [...cacheEntries.entries()].find(
    ([url]) => url.includes("/__public-card-version/"),
  );
  const metadata = await metadataResponse.json();
  metadata.checked_at = Date.now() - 2 * 60 * 1000;
  cacheEntries.set(
    metadataUrl,
    new Response(JSON.stringify(metadata), {
      headers: { "Content-Type": "application/json" },
    }),
  );
  let originCalls = 0;
  installFetch(() => {
    originCalls += 1;
    throw new Error("origin unavailable");
  });
  waitUntilPromises = [];

  const second = await requestWorker();

  assert.equal(second.status, 200);
  assert.equal(await second.text(), "stale-capable page");
  assert.equal(second.headers.get("X-Cache"), "HIT");
  assert.equal(second.headers.get("X-Version-Cache"), "STALE");
  assert.equal(originCalls, 1);
});

test("frontend deployment snippet stays identical to worker source", () => {
  const canonical = readFileSync(new URL("../src/index.js", import.meta.url), "utf8").trim();
  const setupSource = readFileSync(new URL("../../frontend/worker_setup.js", import.meta.url), "utf8");
  const sandbox = { window: {} };
  runInNewContext(setupSource, sandbox);

  assert.equal(sandbox.window.PUBLIC_WORKER_JS_CODE.trim(), canonical);
  assert.match(
    sandbox.window.PUBLIC_WORKER_WRANGLER_TOML,
    /API_BASE = "__API_BASE__"/,
  );
});
