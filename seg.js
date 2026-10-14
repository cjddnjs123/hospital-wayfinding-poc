// FIT-549: on-device floor/wall segmentation (DeepLab ADE20K, self-hosted). Frames never leave the device.
const WALK = new Set([4, 29, 53, 7, 12]); // floor, rug, path, road, sidewalk
const BLOCK_FLOOR_RUN = 0.2; // free floor run (fraction of image height) below which the way ahead is treated as blocked
const V = '20261014v';
export const SIZE = Number(new URLSearchParams(location.search).get('segsize')) || 129; // internal working size (model default 513)
let model = null, loading = null, worker = null, ready = false, seqId = 0, busy = null;
const pending = new Map();

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = resolve; s.onerror = () => reject(new Error('script load failed: ' + src));
    document.head.appendChild(s);
  });
}

function call(msg, transfer) {
  return new Promise((resolve, reject) => {
    const id = ++seqId;
    pending.set(id, { resolve, reject });
    worker.postMessage({ ...msg, id }, transfer || []);
  });
}

async function loadInWorker(base) {
  if (typeof Worker === 'undefined' || typeof OffscreenCanvas === 'undefined') throw new Error('worker unsupported');
  worker = new Worker(new URL('./seg-worker.js?v=' + V, import.meta.url));
  worker.onmessage = ({ data }) => { const p = pending.get(data.id); if (!p) return; pending.delete(data.id); data.ok ? p.resolve(data) : p.reject(new Error(data.error)); };
  worker.onerror = e => { for (const p of pending.values()) p.reject(new Error('worker error: ' + (e.message || ''))); pending.clear(); };
  const abs = new URL(base, location.href).href;
  const r = await Promise.race([call({ type: 'load', base: abs, backend: window.__tfBackend || 'webgl', size: SIZE, v: V }), new Promise((_, rej) => setTimeout(() => rej(new Error('worker load timeout')), 45000))]);
  return { backend: 'worker-' + r.backend };
}

async function loadInMain(base) {
  if (!window.tf) await loadScript(base + 'tf.min.js');
  await window.tf.ready();
  if (window.__tfBackend) await window.tf.setBackend(window.__tfBackend);
  if (!self.fit549LoadSegModel) await loadScript('./seg-patch.js?v=' + V);
  model = await self.fit549LoadSegModel(window.tf, base, SIZE);
  const z = window.tf.zeros([1, 64, 48, 3], 'int32');
  const o = model.predict(z);
  await o.data();
  o.dispose(); z.dispose();
  return { backend: window.tf.getBackend() };
}

// Prefers a Web Worker (keeps the camera/AR view smooth); falls back to the main thread.
export function loadSegmenter(base = './assets/seg/') {
  if (loading) return loading;
  loading = (async () => {
    let info;
    if (!window.__segMainThread) {
      try { info = await loadInWorker(base); } catch (e) { try { worker?.terminate(); } catch {} worker = null; info = null; }
    }
    if (!info) info = await loadInMain(base);
    ready = true;
    return info;
  })().catch(e => { loading = null; model = null; ready = false; throw e; });
  return loading;
}

export function isReady() { return ready; }
export function isBusy() { return !!busy; }
export function usesWorker() { return !!worker; }
// Capture size that fits the model's working size (keeps aspect, longest side = SIZE - 1).
export function captureSize(aspect) { const m = SIZE - 1; return aspect >= 1 ? [m, Math.max(16, Math.round(m / aspect))] : [Math.max(16, Math.round(m * aspect)), m]; }

// canvas -> { w, h, mask(Uint8Array of class ids), ms }
export async function segment(canvas) {
  if (busy) return busy;
  busy = (async () => {
    if (worker) {
      const img = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
      const r = await call({ type: 'segment', pixels: img.data.buffer, w: canvas.width, h: canvas.height }, [img.data.buffer]);
      return { h: r.h, w: r.w, mask: r.mask, ms: r.ms };
    }
    const tf = window.tf, t0 = performance.now();
    const input = tf.tidy(() => tf.browser.fromPixels(canvas).toInt().expandDims(0));
    let out;
    try {
      out = model.predict(input);
      const shape = out.shape.slice(-2);
      const data = await out.data();
      return { h: shape[0], w: shape[1], mask: Uint8Array.from(data), ms: Math.round(performance.now() - t0) };
    } finally {
      input.dispose();
      if (out) out.dispose();
    }
  })();
  try { return await busy; } finally { busy = null; }
}

export function isWalkable(cls) { return WALK.has(cls); }

// Free floor run per horizontal bin, measured upward from the bottom of the image.
export function analyze(seg, bins = 15) {
  const { w, h, mask } = seg;
  const depth = new Array(bins).fill(0);
  let bottomWalk = 0, bottomTotal = 0;
  for (let b = 0; b < bins; b++) {
    const x0 = Math.floor(b * w / bins), x1 = Math.max(x0 + 1, Math.floor((b + 1) * w / bins));
    let top = h, misses = 0;
    for (let y = h - 1; y >= 0; y--) {
      let ok = 0;
      for (let x = x0; x < x1; x++) if (WALK.has(mask[y * w + x])) ok++;
      const good = ok / (x1 - x0) >= 0.6;
      if (y >= h * 0.6) { bottomTotal += x1 - x0; bottomWalk += ok; }
      if (good) { top = y; misses = 0; }
      else if (++misses > Math.max(3, Math.round(h * 0.04))) break;
    }
    depth[b] = top === h ? 0 : (h - top) / h;
  }
  const smooth = depth.map((d, i) => (d * 2 + (depth[i - 1] ?? d) + (depth[i + 1] ?? d)) / 4);
  const c = (bins - 1) / 2;
  let best = 0, bestScore = -1;
  smooth.forEach((d, i) => { const sc = d - 0.04 * Math.abs(i - c) / c; if (sc > bestScore) { bestScore = sc; best = i; } });
  const floorFrac = bottomTotal ? bottomWalk / bottomTotal : 0;
  return {
    bins, depth: smooth, best, xNorm: (best + 0.5) / bins, run: smooth[best], floorFrac,
    blocked: smooth[best] < BLOCK_FLOOR_RUN || floorFrac < 0.12,
  };
}

// Share of walkable cells in a small window around a normalized image point (0..1).
export function walkableAt(seg, nx, ny, r = 0.03) {
  const { w, h, mask } = seg;
  const cx = Math.round(nx * (w - 1)), cy = Math.round(ny * (h - 1)), rx = Math.max(1, Math.round(r * w)), ry = Math.max(1, Math.round(r * h));
  let ok = 0, n = 0;
  for (let y = Math.max(0, cy - ry); y <= Math.min(h - 1, cy + ry); y++)
    for (let x = Math.max(0, cx - rx); x <= Math.min(w - 1, cx + rx); x++) { n++; if (WALK.has(mask[y * w + x])) ok++; }
  return n ? ok / n : 0;
}
