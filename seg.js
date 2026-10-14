// FIT-549: on-device floor/wall segmentation (DeepLab ADE20K, self-hosted). Frames never leave the device.
const WALK = new Set([4, 29, 53, 7, 12]); // floor, rug, path, road, sidewalk
const BLOCK_FLOOR_RUN = 0.2; // free floor run (fraction of image height) below which the way ahead is treated as blocked
let model = null, loading = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = resolve; s.onerror = () => reject(new Error('script load failed: ' + src));
    document.head.appendChild(s);
  });
}

export function loadSegmenter(base = './assets/seg/') {
  if (loading) return loading;
  loading = (async () => {
    if (!window.tf) await loadScript(base + 'tf.min.js');
    await window.tf.ready();
    if (window.__tfBackend) await window.tf.setBackend(window.__tfBackend);
    model = await window.tf.loadGraphModel(base + 'model.json');
    const z = window.tf.zeros([1, 64, 48, 3], 'int32');
    const o = model.predict(z);
    await o.data();
    o.dispose(); z.dispose();
    return { backend: window.tf.getBackend() };
  })().catch(e => { loading = null; model = null; throw e; });
  return loading;
}

export function isReady() { return !!model; }

// canvas -> { w, h, mask(Uint8Array of class ids), ms }
export async function segment(canvas) {
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
