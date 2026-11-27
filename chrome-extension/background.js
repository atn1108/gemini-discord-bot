try {
  importScripts("local_config.js");
} catch {
  // optional local hardcoded config (gitignored)
}

const EXT_VERSION = "1.0.0";
const ENDPOINT_INIT = "https://gemini.google.com/app?hl=en";
const ENDPOINT_BATCH = "https://gemini.google.com/_/BardChatUi/data/batchexecute";
const ENDPOINT_GENERATE = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate";

let ws = null;
let connectPromise = null;
let pingTimer = null;
let reconnectTimer = null;
let reconnectDelayMs = 2000;
let state = {
  connected: false,
  lastStatus: "Chưa cấu hình",
  lastJobAt: null,
  lastError: "",
};

function setStatus(patch) {
  state = { ...state, ...patch };
}

async function ensureNetRules() {
  const rules = [
    {
      id: 1,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders: [
          { header: "Origin", operation: "set", value: "https://gemini.google.com" },
          { header: "Referer", operation: "set", value: "https://gemini.google.com/" },
          { header: "X-Same-Domain", operation: "set", value: "1" },
        ],
      },
      condition: {
        urlFilter: "||gemini.google.com/",
        tabIds: [-1],
        resourceTypes: ["xmlhttprequest", "other"],
      },
    },
    {
      id: 2,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders: [
          { header: "Referer", operation: "set", value: "https://gemini.google.com/" },
          { header: "Origin", operation: "remove" },
        ],
      },
      condition: {
        urlFilter: "||googleusercontent.com/",
        tabIds: [-1],
        resourceTypes: ["xmlhttprequest", "other", "image"],
      },
    },
    {
      id: 3,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders: [
          { header: "Referer", operation: "set", value: "https://gemini.google.com/" },
          { header: "Origin", operation: "remove" },
        ],
      },
      condition: {
        urlFilter: "||work.fife.usercontent.google.com/",
        tabIds: [-1],
        resourceTypes: ["xmlhttprequest", "other", "image"],
      },
    },
  ];
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [1, 2, 3],
    addRules: rules,
  });
}

function normalizeToken(raw) {
  let t = (raw || "").trim();
  if (t.startsWith("token=")) t = t.slice(6).trim();
  return t;
}

async function getConfig() {
  const fallback = self.LOCAL_RELAY_CONFIG || {};
  const res = await chrome.storage.local.get({
    wsUrl: "",
    token: "",
    tempChat: fallback.tempChat !== false,
  });
  const wsUrl = (res.wsUrl || fallback.wsUrl || "wss://gemini-discord-bot-production-f4e6267e.up.railway.app/ws/relay").trim();
  const token = normalizeToken(res.token || fallback.token || "");
  return {
    wsUrl,
    token,
    tempChat: res.tempChat !== false,
  };
}

function randomReqId() {
  return String(Math.floor(Math.random() * 90000) + 10000);
}

function makeUuidUpper() {
  return crypto.randomUUID().toUpperCase();
}

function arrayBufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function detectImageMime(bytes, headerMime) {
  if (bytes.length >= 4) {
    if (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return "image/png";
    if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
    if (bytes.length >= 12 && bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[8] === 0x57 && bytes[9] === 0x45) {
      return "image/webp";
    }
  }
  if (headerMime && headerMime.toLowerCase().startsWith("image/")) {
    return headerMime.split(";")[0].trim().toLowerCase();
  }
  return "";
}

function parseBatchexecuteFrames(text) {
  const cleaned = text.replace(/^\)\]\}'\s*/, "");
  const frames = [];
  for (const line of cleaned.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || /^\d+$/.test(trimmed)) continue;
    try {
      frames.push(JSON.parse(trimmed));
    } catch {
      // ignore partial/non-JSON lines
    }
  }
  return frames;
}

function collectRpcRecords(val, rpcId, out) {
  if (!Array.isArray(val)) return;
  if (val.length >= 2 && (val[0] === "wrb.fr" || val[0] === "er") && val[1] === rpcId) {
    out.push(val);
    return;
  }
  for (const item of val) collectRpcRecords(item, rpcId, out);
}

function computeCapacity(tiers, caps) {
  const has = (arr, target) => Array.isArray(arr) && arr.some((x) => Number(x) === target);
  if (has(tiers, 21)) return { capacity: 1, capacityField: 13 };
  if (has(tiers, 22)) return { capacity: 2, capacityField: 13 };
  if (has(caps, 115)) return { capacity: 4, capacityField: 12 };
  if (has(tiers, 16) || has(caps, 106)) return { capacity: 3, capacityField: 12 };
  if (has(tiers, 8) || has(caps, 19)) return { capacity: 2, capacityField: 12 };
  return { capacity: 1, capacityField: 12 };
}

async function initGeminiSession() {
  const resp = await fetch(ENDPOINT_INIT, { credentials: "include" });
  if (!resp.ok) throw new Error(`Gemini init HTTP ${resp.status}`);
  const html = await resp.text();
  const atMatch = html.match(/"SNlM0e":"([^"]+)"/) || html.match(/\["SNlM0e","([^"]+)"\]/);
  if (!atMatch) {
    throw new Error("Chrome profile chưa đăng nhập gemini.google.com (không thấy SNlM0e)");
  }
  const blMatch = html.match(/"cfb2h":"([^"]+)"/);
  const sidMatch = html.match(/"FdrFJe":"([^"]+)"/);
  const langMatch = html.match(/"TuX5cc":"([^"]+)"/);
  return {
    at: atMatch[1],
    bl: blMatch ? blMatch[1] : "",
    sid: sidMatch ? sidMatch[1] : "",
    lang: langMatch ? langMatch[1] : "en",
    generationId: makeUuidUpper(),
  };
}

async function fetchGeminiAutoModel(session) {
  const q = new URLSearchParams({
    rpcids: "otAQ7b",
    hl: session.lang,
    _reqid: randomReqId(),
    rt: "c",
    "source-path": "/app",
  });
  if (session.bl) q.set("bl", session.bl);
  if (session.sid) q.set("f.sid", session.sid);

  const batchHeader = new Array(17).fill(null);
  batchHeader[0] = 1;
  batchHeader[8] = [4, 5, 6, 8];
  batchHeader[16] = session.generationId;

  const form = new URLSearchParams({
    at: session.at,
    "f.req": '[[["otAQ7b","[]",null,"generic"]]]',
  });

  const resp = await fetch(`${ENDPOINT_BATCH}?${q.toString()}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
      "x-goog-ext-525001261-jspb": JSON.stringify(batchHeader),
      "x-goog-ext-73010989-jspb": "[0]",
    },
    body: form.toString(),
  });
  if (!resp.ok) throw new Error(`Model discovery HTTP ${resp.status}`);
  const text = await resp.text();
  const frames = parseBatchexecuteFrames(text);
  const records = [];
  for (const frame of frames) collectRpcRecords(frame, "otAQ7b", records);

  const models = [];
  for (const rec of records) {
    let status = rec[2];
    if (typeof status === "string") {
      try {
        status = JSON.parse(status);
      } catch {
        continue;
      }
    }
    if (!Array.isArray(status)) continue;
    const { capacity, capacityField } = computeCapacity(status[16], status[17]);
    const entries = Array.isArray(status[15]) ? status[15] : [];
    for (const entry of entries) {
      if (!Array.isArray(entry) || typeof entry[0] !== "string" || !entry[0]) continue;
      const rawNum = entry[17] ?? entry[9];
      const modelNumber = typeof rawNum === "number" && rawNum > 0 ? rawNum : 1;
      const display = String(entry[11] || entry[19] || entry[1] || "").toLowerCase();
      models.push({
        modelId: entry[0],
        modelNumber,
        capacity,
        capacityField,
        display,
      });
    }
  }
  if (models.length === 0) {
    throw new Error("Không lấy được danh sách model từ Gemini Web (cookie có thể đã hết hạn)");
  }
  return models.find((m) => m.display.includes("auto")) || models[0];
}

function buildModelHeaders(model, requestId, generationId) {
  const offset = model.capacityField - 12;
  const header = new Array(17 + offset).fill(null);
  header[0] = 1;
  header[4] = model.modelId;
  header[7] = 0;
  header[8] = [4, 5, 6, 8];
  header[model.capacityField - 1] = model.capacity;
  header[14 + offset] = model.modelNumber;
  header[15 + offset] = 1; // thinkingLevel = low
  header[16 + offset] = generationId;
  return {
    "x-goog-ext-525001261-jspb": JSON.stringify(header),
    "x-goog-ext-525005358-jspb": JSON.stringify([requestId, 1]),
    "x-goog-ext-73010989-jspb": "[0]",
    "x-goog-ext-73010990-jspb": "[0,0,0]",
  };
}

function extractGeneratedImageUrls(respText) {
  const urls = [];
  const seen = new Set();
  const addUrl = (u) => {
    if (typeof u !== "string") return;
    let clean = u.replace(/\\u003d/g, "=").replace(/\\\//g, "/").trim();
    if (clean.startsWith("//")) clean = "https:" + clean;
    if (!clean.startsWith("https://lh3.googleusercontent.com/gg-dl/")) return;
    clean = clean.replace(/=(?:s|w|h)\d+[^/]*$/, "");
    if (!seen.has(clean)) {
      seen.add(clean);
      urls.push(clean);
    }
  };

  const frames = parseBatchexecuteFrames(respText);
  for (const frame of frames) {
    const items = Array.isArray(frame) ? frame : [];
    for (const item of items) {
      if (!Array.isArray(item) || item[0] !== "wrb.fr") continue;
      let inner = item[2];
      if (typeof inner === "string") {
        try {
          inner = JSON.parse(inner);
        } catch {
          continue;
        }
      }
      const candidates = Array.isArray(inner) && Array.isArray(inner[4]) ? inner[4] : [];
      for (const cand of candidates) {
        const media = Array.isArray(cand) && Array.isArray(cand[12]) ? cand[12][7] : null;
        const generated = Array.isArray(media) && Array.isArray(media[0]) ? media[0] : [];
        for (const node of generated) {
          const u = node?.[0]?.[3]?.[3];
          addUrl(u);
        }
      }
    }
  }

  if (urls.length === 0) {
    const unescaped = respText.replace(/\\u003d/g, "=").replace(/\\\//g, "/");
    const matches = unescaped.match(/https:\/\/lh3\.googleusercontent\.com\/gg-dl\/[A-Za-z0-9_=-]+/g) || [];
    for (const m of matches) addUrl(m);
  }
  return urls;
}

async function fetchImageBytesAsBase64(baseUrl) {
  const candidates = [`${baseUrl}=s2048`, `${baseUrl}=s0`, baseUrl];
  let lastStatus = "";
  for (const url of candidates) {
    try {
      const resp = await fetch(url, { credentials: "include" });
      if (!resp.ok) {
        lastStatus = `HTTP ${resp.status}`;
        continue;
      }
      const buf = await resp.arrayBuffer();
      const bytes = new Uint8Array(buf);
      const mime = detectImageMime(bytes, resp.headers.get("content-type") || "");
      if (!mime || bytes.length < 64) {
        lastStatus = `non-image (${resp.headers.get("content-type") || "unknown"})`;
        continue;
      }
      return {
        b64: arrayBufferToBase64(buf),
        mime,
      };
    } catch (e) {
      lastStatus = e.message || String(e);
    }
  }
  throw new Error(`Tải ảnh gg-dl thất bại (${lastStatus || "unknown"})`);
}

async function generateAndFetchImage(prompt, tempChat = true) {
  await ensureNetRules();
  const session = await initGeminiSession();
  const model = await fetchGeminiAutoModel(session);

  const imagePrompt =
    "Generate an image from this prompt. Return the generated image, not only a text description.\n\nPrompt: " +
    prompt;
  const requestId = makeUuidUpper();
  const inner = new Array(81).fill(null);
  inner[0] = [imagePrompt];
  inner[1] = [session.lang];
  inner[2] = ["", "", "", null, null, null, null, null, null, ""];
  inner[6] = [1];
  inner[7] = 1;
  inner[10] = 1;
  inner[11] = 0;
  inner[17] = [[0]];
  inner[18] = 0;
  inner[27] = 1;
  inner[30] = [4];
  inner[41] = [1];
  inner[53] = 0;
  inner[59] = requestId;
  inner[61] = [];
  inner[68] = 1;
  inner[79] = model.modelNumber;
  inner[80] = 1;
  if (tempChat) inner[45] = 1;

  const q = new URLSearchParams({
    at: session.at,
    hl: session.lang,
    _reqid: randomReqId(),
    rt: "c",
  });
  if (session.bl) q.set("bl", session.bl);
  if (session.sid) q.set("f.sid", session.sid);

  const form = new URLSearchParams({
    at: session.at,
    "f.req": JSON.stringify([null, JSON.stringify(inner)]),
  });

  const resp = await fetch(`${ENDPOINT_GENERATE}?${q.toString()}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
      ...buildModelHeaders(model, requestId, session.generationId),
    },
    body: form.toString(),
  });
  if (!resp.ok) throw new Error(`StreamGenerate HTTP ${resp.status}`);
  const body = await resp.text();
  const urls = extractGeneratedImageUrls(body);
  if (urls.length === 0) {
    throw new Error("Gemini không trả về ảnh (có thể prompt bị từ chối hoặc acc hết lượt)");
  }
  return await fetchImageBytesAsBase64(urls[0]);
}

function stopPing() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = reconnectDelayMs;
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
  try {
    chrome.alarms.create("relay-reconnect", { when: Date.now() + delay });
  } catch {}
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    chrome.alarms.create("relay-keepalive", { periodInMinutes: 0.5 });
connectRelay();
  }, delay);
}

async function connectRelay(force = false) {
  if (connectPromise) {
    if (!force) return connectPromise;
    await connectPromise.catch(() => {});
  }
  connectPromise = _connectRelayImpl(force).finally(() => {
    connectPromise = null;
  });
  return connectPromise;
}

async function _connectRelayImpl(force = false) {
  if (force && ws) {
    const old = ws;
    ws = null;
    stopPing();
    try {
      old.close(1000, "reconnect");
    } catch {}
  }
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const cfg = await getConfig();
  if (!cfg.wsUrl || !cfg.token) {
    setStatus({ connected: false, lastStatus: "Chưa nhập WS URL / Token", lastError: "" });
    return;
  }

  let targetUrl = cfg.wsUrl;
  let authCheckUrl = "";
  try {
    const u = new URL(cfg.wsUrl);
    u.searchParams.set("token", cfg.token);
    targetUrl = u.toString();
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    authCheckUrl = `${proto}//${u.host}/relay/auth-check?token=${encodeURIComponent(cfg.token)}`;
  } catch {
    setStatus({ connected: false, lastStatus: "WS URL không hợp lệ", lastError: cfg.wsUrl });
    return;
  }

  if (authCheckUrl) {
    try {
      const check = await fetch(authCheckUrl);
      if (check.status === 401 || check.status === 503) {
        const fallbackTok = normalizeToken(self.LOCAL_RELAY_CONFIG?.token || "");
        if (check.status === 401 && fallbackTok && fallbackTok !== cfg.token) {
          await chrome.storage.local.set({ token: fallbackTok });
          setTimeout(() => connectRelay(true), 50);
          return;
        }
        setStatus({ connected: false, lastStatus: `Từ chối kết nối (HTTP ${check.status})`, lastError: "Sai Relay Token hoặc server chưa đặt RELAY_SECRET" });
        scheduleReconnect();
        return;
      }
    } catch (e) {
      // ignore precheck network error and still let WS attempt / report
    }
  }

  setStatus({ connected: false, lastStatus: "Đang kết nối...", lastError: "" });
  const sock = new WebSocket(targetUrl);
  ws = sock;

  sock.onopen = () => {
    if (ws !== sock) {
      try { sock.close(1000, "superseded"); } catch {}
      return;
    }
    reconnectDelayMs = 2000;
    setStatus({ connected: true, lastStatus: "Đã kết nối bot", lastError: "" });
    sock.send(JSON.stringify({ type: "hello", version: EXT_VERSION, temp_chat: cfg.tempChat }));
    stopPing();
    pingTimer = setInterval(() => {
      if (ws === sock && sock.readyState === WebSocket.OPEN) {
        sock.send(JSON.stringify({ type: "ping", ts: Date.now() }));
      }
    }, 20000);
  };

  sock.onmessage = async (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch {
      return;
    }
    if (msg.type === "generate_image") {
      const latestCfg = await getConfig();
      setStatus({ lastJobAt: Date.now(), lastStatus: "Đang sinh ảnh..." });
      try {
        const result = await generateAndFetchImage(msg.prompt || "", latestCfg.tempChat);
        if (sock.readyState === WebSocket.OPEN) {
          sock.send(
            JSON.stringify({
              type: "image_result",
              id: msg.id,
              ok: true,
              b64: result.b64,
              mime: result.mime,
            })
          );
        }
        setStatus({ lastStatus: "Đã kết nối bot (xong job gần nhất)", lastError: "" });
      } catch (err) {
        const errText = err?.message || String(err);
        setStatus({ lastStatus: "Đã kết nối bot (job lỗi)", lastError: errText });
        if (sock.readyState === WebSocket.OPEN) {
          sock.send(
            JSON.stringify({
              type: "image_result",
              id: msg.id,
              ok: false,
              error: errText,
            })
          );
        }
      }
    }
  };

  sock.onerror = () => {
    if (ws !== sock) return;
    setStatus({ connected: false, lastStatus: "Lỗi kết nối WS", lastError: "WebSocket error" });
  };

  sock.onclose = (evt) => {
    if (ws !== sock) return;
    stopPing();
    ws = null;
    setStatus({
      connected: false,
      lastStatus: `Mất kết nối (${evt.code || "closed"})`,
    });
    // Don't fight a newer socket that intentionally replaced this one.
    if (evt.reason === "replaced" || evt.reason === "superseded") return;
    scheduleReconnect();
  };
}

chrome.runtime.onInstalled.addListener(() => {
  ensureNetRules().catch(() => {});
  chrome.alarms.create("relay-keepalive", { periodInMinutes: 0.5 });
  connectRelay();
});

chrome.runtime.onStartup.addListener(() => {
  ensureNetRules().catch(() => {});
  chrome.alarms.create("relay-keepalive", { periodInMinutes: 0.5 });
  connectRelay();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "relay-keepalive" || alarm.name === "relay-reconnect") {
    connectRelay();
  }
});

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req?.action === "get_status") {
    getConfig().then((cfg) => sendResponse({ state, cfg }));
    return true;
  }
  if (req?.action === "save_config") {
    chrome.storage.local
      .set({
        wsUrl: (req.wsUrl || "").trim(),
        token: normalizeToken(req.token),
        tempChat: req.tempChat !== false,
      })
      .then(() => connectRelay(true))
      .then(() => sendResponse({ ok: true, state }));
    return true;
  }
  if (req?.action === "test_generate") {
    getConfig()
      .then((cfg) => generateAndFetchImage(req.prompt || "a cute cat wearing glasses", cfg.tempChat))
      .then((res) => sendResponse({ ok: true, mime: res.mime, bytesLen: Math.floor((res.b64.length * 3) / 4) }))
      .catch((err) => sendResponse({ ok: false, error: err?.message || String(err) }));
    return true;
  }
});

chrome.alarms.create("relay-keepalive", { periodInMinutes: 0.5 });
connectRelay();
