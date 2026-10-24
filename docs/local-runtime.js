/* Property Scout – standalone runtime.
   Gives the app the same small API it had inside Claude (db, assets, downloads),
   but backed by:
     - data/db.json      the scanner's file, refreshed every hour on GitHub
     - localStorage      your own changes on this device (shortlist, searches, settings)
     - IndexedDB         photos you add on this device                                    */
(function () {
  "use strict";
  const LOCAL_KEY = "scoutLocalEdits_v1";
  let base = { collections: {} };     // from data/db.json
  let edits = {};                      // path -> {op:"set"|"patch"|"del", data}
  const listeners = new Set();
  let ready = null;

  try { edits = JSON.parse(localStorage.getItem(LOCAL_KEY) || "{}") || {}; } catch (e) { edits = {}; }
  const saveEdits = () => { try { localStorage.setItem(LOCAL_KEY, JSON.stringify(edits)); } catch (e) {} };

  async function load() {
    const r = await fetch("data/db.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    base = await r.json();
  }
  function rawCol(name) { return (base.collections && base.collections[name]) || {}; }
  function getDoc(col, id) {
    const p = col + "/" + id;
    let d = rawCol(col)[id];
    d = d ? JSON.parse(JSON.stringify(d)) : undefined;
    const e = edits[p];
    if (e) {
      if (e.op === "del") return undefined;
      if (e.op === "set") d = JSON.parse(JSON.stringify(e.data));
      if (e.op === "patch") d = Object.assign(d || {}, JSON.parse(JSON.stringify(e.data)));
    }
    return d;
  }
  function listCol(col) {
    const ids = new Set(Object.keys(rawCol(col)));
    for (const p of Object.keys(edits)) { const [c, id] = p.split("/"); if (c === col && id) ids.add(id); }
    const out = [];
    for (const id of ids) { const d = getDoc(col, id); if (d) out.push({ id, d }); }
    return out;
  }
  const snapDoc = (id, d) => ({ id, exists: !!d, data: () => d });
  function emit() { for (const l of listeners) { try { l(); } catch (e) { console.error(e); } } }
  function write(path, op, data) {
    const prev = edits[path];
    if (op === "patch" && prev && prev.op === "set") edits[path] = { op: "set", data: Object.assign({}, prev.data, data) };
    else if (op === "patch" && prev && prev.op === "patch") edits[path] = { op: "patch", data: Object.assign({}, prev.data, data) };
    else edits[path] = { op, data };
    saveEdits(); emit(); return Promise.resolve();
  }

  function docRef(path) {
    const [col, id] = path.split("/");
    return {
      id,
      onSnapshot(cb, err) {
        const fn = () => cb(snapDoc(id, getDoc(col, id)));
        listeners.add(fn); ready.then(fn).catch(e => err && err({ code: "unavailable", message: String(e) }));
        return () => listeners.delete(fn);
      },
      get: async () => { await ready; return snapDoc(id, getDoc(col, id)); },
      set: (data) => write(path, "set", data),
      update: (data) => write(path, "patch", data),
      delete: () => write(path, "del", null),
    };
  }
  function colRef(col, order, lim) {
    return {
      orderBy: (f, dir) => colRef(col, { f, dir }, lim),
      limit: (n) => colRef(col, order, Math.max(n, 2000)),
      doc: (id) => docRef(col + "/" + id),
      add: (data) => { const id = "local-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); return write(col + "/" + id, "set", data).then(() => ({ id })); },
      onSnapshot(cb, err) {
        const fn = () => {
          let rows = listCol(col);
          if (order) rows.sort((a, b) => { const x = a.d[order.f] || "", y = b.d[order.f] || ""; return (x < y ? -1 : x > y ? 1 : 0) * (order.dir === "desc" ? -1 : 1); });
          if (lim) rows = rows.slice(0, lim);
          cb({ docs: rows.map(r => ({ id: r.id, exists: true, data: () => r.d })) });
        };
        listeners.add(fn); ready.then(fn).catch(e => err && err({ code: "unavailable", message: String(e) }));
        return () => listeners.delete(fn);
      },
    };
  }
  const db = { doc: docRef, collection: (c) => colRef(c) };

  // ---- photos you add (IndexedDB, this device only)
  const blobUrls = new Map();
  function idb() {
    return new Promise((res, rej) => { const r = indexedDB.open("scoutPhotos", 1); r.onupgradeneeded = () => r.result.createObjectStore("p"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  }
  async function idbAll() {
    const d = await idb();
    return new Promise((res) => { const out = []; const tx = d.transaction("p"); const st = tx.objectStore("p"); const c = st.openCursor();
      c.onsuccess = () => { const cur = c.result; if (cur) { out.push([cur.key, cur.value]); cur.continue(); } else res(out); }; c.onerror = () => res(out); });
  }
  const assets = {
    async upload(blob) {
      const id = "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      const d = await idb();
      await new Promise((res, rej) => { const tx = d.transaction("p", "readwrite"); tx.objectStore("p").put(blob, id); tx.oncomplete = res; tx.onerror = () => rej(tx.error); });
      blobUrls.set(id, URL.createObjectURL(blob)); return { id };
    },
    async delete(id) {
      const d = await idb();
      await new Promise((res) => { const tx = d.transaction("p", "readwrite"); tx.objectStore("p").delete(id); tx.oncomplete = res; tx.onerror = res; });
      blobUrls.delete(id);
    },
  };
  window.__blobUrl = (id) => blobUrls.get(id) || "";
  const photosReady = (async () => { try { for (const [k, v] of await idbAll()) blobUrls.set(k, URL.createObjectURL(v)); } catch (e) {} })();

  // ---- saving files
  const downloads = {
    async save({ filename, data }) {
      const type = /\.csv$/i.test(filename) ? "text/csv" : "application/json";
      const blob = data instanceof Blob ? data : new Blob([data], { type });
      const file = new File([blob], filename, { type });
      if (navigator.canShare && /iPhone|iPad/i.test(navigator.userAgent) && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file] }); return; } catch (e) { if (e.name === "AbortError") { const x = new Error("declined"); x.code = "declined"; throw x; } }
      }
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = filename;
      document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
    },
  };

  ready = load();
  ready.then(() => photosReady).then(emit).catch(() => {});
  // pick up the scanner's new results while the app is open
  const refresh = () => load().then(emit).catch(() => {});
  setInterval(refresh, 10 * 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });

  window.claude = {
    use: async (name) => {
      if (name === "db") { try { await ready; return db; } catch (e) { return null; } }
      if (name === "assets") { if (!window.indexedDB) throw new Error("no storage"); return assets; }
      if (name === "downloads") return downloads;
      return null;
    },
  };
})();
