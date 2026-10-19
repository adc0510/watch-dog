// PC Watchdog Tabs: answers requests from the PC Watchdog desktop app via native messaging.
const HOST = "io.github.pcwatchdog.brave";
const favCache = new Map();
let port = null;

function connect() {
  port = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(async (msg) => {
    let res;
    try {
      res = await handle(msg);
    } catch (e) {
      res = { error: String(e && e.message || e) };
    }
    if (port) port.postMessage({ id: msg.id, ...res });
  });
  port.onDisconnect.addListener(() => {
    port = null;
    setTimeout(connect, 5000);
  });
}

async function favicon(url) {
  let origin;
  try { origin = new URL(url).origin; } catch { return ""; }
  if (favCache.has(origin)) return favCache.get(origin);
  let data = "";
  try {
    const u = chrome.runtime.getURL(`/_favicon/?pageUrl=${encodeURIComponent(url)}&size=32`);
    const buf = new Uint8Array(await (await fetch(u)).arrayBuffer());
    let s = "";
    for (const b of buf) s += String.fromCharCode(b);
    data = btoa(s);
  } catch {}
  favCache.set(origin, data);
  return data;
}

async function handle(m) {
  switch (m.cmd) {
    case "ping":
      return { ok: true, version: chrome.runtime.getManifest().version };
    case "list": {
      const tabs = await chrome.tabs.query({});
      const out = [];
      for (const t of tabs) {
        const url = t.url || t.pendingUrl || "";
        out.push({
          id: t.id, windowId: t.windowId, title: t.title || url, url,
          active: t.active, audible: !!t.audible, muted: !!(t.mutedInfo && t.mutedInfo.muted),
          discarded: !!t.discarded, pinned: !!t.pinned, status: t.status || "",
          lastAccessed: t.lastAccessed || 0,
          icon: m.icons ? await favicon(url) : "",
        });
      }
      return { ok: true, tabs: out };
    }
    case "burn": {
      // Busy-loop briefly inside the tab so the app can see which process it lives in.
      await chrome.scripting.executeScript({
        target: { tabId: m.tabId },
        injectImmediately: true,
        func: (ms) => { const end = performance.now() + ms; while (performance.now() < end) {} return 1; },
        args: [m.ms || 250],
      });
      return { ok: true };
    }
    case "focus": {
      const t = await chrome.tabs.update(m.tabId, { active: true });
      await chrome.windows.update(t.windowId, { focused: true });
      return { ok: true };
    }
    case "discard":
      await chrome.tabs.discard(m.tabId);
      return { ok: true };
    case "close":
      await chrome.tabs.remove(m.tabId);
      return { ok: true };
    case "sleepBackground": {
      const tabs = await chrome.tabs.query({ active: false, discarded: false, audible: false, pinned: false });
      let n = 0;
      for (const t of tabs) {
        try { await chrome.tabs.discard(t.id); n++; } catch {}
      }
      return { ok: true, count: n };
    }
  }
  return { error: "unknown command" };
}

chrome.runtime.onStartup.addListener(() => {});
chrome.runtime.onInstalled.addListener(() => {});
connect();
