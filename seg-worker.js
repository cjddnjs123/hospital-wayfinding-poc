// FIT-549: runs the floor/wall segmentation model off the main thread so the camera/AR view keeps rendering.
let model = null;
self.onmessage = async ({ data }) => {
  try {
    if (data.type === 'load') {
      importScripts(data.base + 'tf.min.js');
      await tf.ready();
      // WebGL in the worker; the wasm/CPU backend measured ~30 s per frame on a Galaxy S8+.
      await tf.setBackend(data.backend || 'webgl');
      importScripts(data.base.replace(/assets\/seg\/$/, '') + 'seg-patch.js?v=' + data.v);
      model = await self.fit549LoadSegModel(tf, data.base, data.size);
      const z = tf.zeros([1, 64, 48, 3], 'int32'), o = model.predict(z);
      await o.data(); o.dispose(); z.dispose();
      self.postMessage({ id: data.id, ok: true, backend: tf.getBackend() });
    } else if (data.type === 'segment') {
      const t0 = performance.now();
      const input = tf.tidy(() => tf.tensor3d(new Uint8Array(data.pixels), [data.h, data.w, 4], 'int32').slice([0, 0, 0], [data.h, data.w, 3]).expandDims(0));
      const out = model.predict(input);
      const shape = out.shape.slice(-2), arr = await out.data();
      input.dispose(); out.dispose();
      const mask = Uint8Array.from(arr);
      self.postMessage({ id: data.id, ok: true, h: shape[0], w: shape[1], mask, ms: Math.round(performance.now() - t0) }, [mask.buffer]);
    }
  } catch (e) {
    self.postMessage({ id: data.id, ok: false, error: String(e && e.message || e) });
  }
};
