// Cloudflare Worker：公开扫码页边缘代理 + 版本化缓存
// 部署说明见 worker-src/README.md
// 代码与 frontend/worker_setup.js 保持同步，并由测试强制校验。

const WORKER_VERSION = "7";
const PUBLIC_TOKEN_PATTERN = /^\/p\/([A-Za-z0-9_-]{20,128})$/;
const VERSION_FRESH_SECONDS = 60;
const VERSION_STALE_SECONDS = 24 * 60 * 60;

export function getApiBase(env) {
  const raw = String(env?.API_BASE || "").trim();
  if (!raw) throw new Error("API_BASE is not configured");

  const url = new URL(raw);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error("API_BASE must use http or https");
  }
  if (url.search || url.hash) {
    throw new Error("API_BASE must not contain a query or fragment");
  }

  return url.toString().replace(/\/+$/, "");
}

function workerResponse(body, status, extraHeaders = {}) {
  const headers = new Headers(extraHeaders);
  headers.set("X-Public-Card-Worker", WORKER_VERSION);
  return new Response(body, { status, headers });
}

function configurationError() {
  return workerResponse("Worker configuration error", 500, {
    "X-Worker-Error": "api-base",
  });
}

function originResult(body, status, stage, originStatus) {
  return workerResponse(body, status, {
    "X-Origin-Stage": stage,
    "X-Origin-Status": String(originStatus),
  });
}

function originError(stage, originStatus = 0) {
  return originResult("Origin error", 502, stage, originStatus);
}

function versionCacheKey(url, token) {
  return new Request(
    `${url.origin}/__public-card-version/${token}?worker=${WORKER_VERSION}`,
    { method: "GET" },
  );
}

async function readVersionCache(cache, key) {
  const response = await cache.match(key);
  if (!response) return null;
  try {
    const data = await response.json();
    const version = data.public_version;
    const checkedAt = Number(data.checked_at || 0);
    if (!version || !Number.isFinite(checkedAt) || checkedAt <= 0) return null;
    return {
      version,
      ageSeconds: Math.max(0, (Date.now() - checkedAt) / 1000),
    };
  } catch (_error) {
    return null;
  }
}

function storeVersionCache(cache, key, version, ctx) {
  const response = new Response(JSON.stringify({
    public_version: version,
    checked_at: Date.now(),
  }), {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${VERSION_STALE_SECONDS}`,
    },
  });
  ctx.waitUntil(cache.put(key, response));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const match = url.pathname.match(PUBLIC_TOKEN_PATTERN);
    if (!match) {
      return workerResponse("Not found", 404);
    }

    let apiBase;
    try {
      apiBase = getApiBase(env);
    } catch (_error) {
      return configurationError();
    }

    const cache = caches.default;
    const token = encodeURIComponent(match[1]);
    const metadataKey = versionCacheKey(url, token);
    const cachedVersion = await readVersionCache(cache, metadataKey);
    let version = cachedVersion?.version;
    let versionCacheState = cachedVersion ? "FRESH" : "MISS";
    let versionOriginStatus = 0;

    if (!cachedVersion || cachedVersion.ageSeconds >= VERSION_FRESH_SECONDS) {
      let versionResponse;
      try {
        versionResponse = await fetch(`${apiBase}/api/public/${token}/version`, {
          cf: { cacheTtl: 0, cacheEverything: false },
        });
      } catch (_error) {
        versionResponse = null;
      }
      versionOriginStatus = versionResponse?.status || 0;
      if (versionResponse?.status === 404) {
        return originResult("Not found", 404, "version", 404);
      }
      if (versionResponse?.status === 200) {
        try {
          const data = await versionResponse.json();
          version = data.public_version;
        } catch (_error) {
          version = null;
        }
        if (!version) {
          return originError("version", versionResponse.status);
        }
        versionCacheState = "REFRESHED";
        storeVersionCache(cache, metadataKey, version, ctx);
      } else if (
        cachedVersion
        && cachedVersion.ageSeconds <= VERSION_STALE_SECONDS
        && (!versionResponse || versionResponse.status >= 500)
      ) {
        version = cachedVersion.version;
        versionCacheState = "STALE";
      } else {
        return originError("version", versionOriginStatus);
      }
    }

    const cacheKey = new Request(
      `${url.origin}${url.pathname}?v=${encodeURIComponent(String(version))}&worker=${WORKER_VERSION}`,
      {
        method: "GET",
      }
    );
    const cached = await cache.match(cacheKey);
    if (cached) {
      const headers = new Headers(cached.headers);
      // Cache API 继续保留 30 天，但浏览器不得缓存固定二维码 URL，
      // 否则页面改版后手机仍可能展示旧 HTML。
      headers.set("Cache-Control", "no-store, max-age=0");
      headers.set("X-Cache", "HIT");
      headers.set("X-Public-Card-Worker", WORKER_VERSION);
      headers.set("X-Version-Cache", versionCacheState);
      if (versionOriginStatus) {
        headers.set("X-Origin-Stage", "version");
        headers.set("X-Origin-Status", String(versionOriginStatus));
      } else {
        headers.delete("X-Origin-Stage");
        headers.delete("X-Origin-Status");
      }
      return new Response(cached.body, {
        status: cached.status,
        headers,
      });
    }

    if (versionCacheState === "STALE") {
      return originError("version", versionOriginStatus);
    }

    let pageResponse;
    try {
      pageResponse = await fetch(`${apiBase}/p/${token}`, {
        headers: { "X-Forwarded-Host": url.host },
        cf: { cacheTtl: 0, cacheEverything: false },
      });
    } catch (_error) {
      return originError("page");
    }

    if (pageResponse.status === 404) {
      return originResult("Not found", 404, "page", 404);
    }
    if (pageResponse.status !== 200) {
      return originError("page", pageResponse.status);
    }

    const headers = new Headers(pageResponse.headers);
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("X-Frame-Options", "DENY");
    headers.set("Referrer-Policy", "no-referrer");
    headers.set(
      "Content-Security-Policy",
      "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
    );
    // Cloudflare Cache API 硬上限 30 天。
    headers.set("Cache-Control", "public, max-age=2592000");
    headers.set("X-Cache", "MISS");
    headers.set("X-Cache-Version", String(version));
    headers.set("X-Public-Card-Worker", WORKER_VERSION);
    headers.set("X-Version-Cache", versionCacheState);
    headers.set("X-Origin-Stage", "page");
    headers.set("X-Origin-Status", String(pageResponse.status));

    const cacheResponse = new Response(pageResponse.body, { status: 200, headers });
    ctx.waitUntil(cache.put(cacheKey, cacheResponse.clone()));

    const clientHeaders = new Headers(cacheResponse.headers);
    clientHeaders.set("Cache-Control", "no-store, max-age=0");
    return new Response(cacheResponse.body, { status: 200, headers: clientHeaders });
  },
};
