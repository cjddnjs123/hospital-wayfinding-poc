// FIT-549: loads the DeepLab graph with a smaller internal working size (default 513 -> 129/257) so it runs on phones.
// Only the size constants are rewritten in memory; weights and model files are unchanged.
self.fit549LoadSegModel = async function (tf, base, size) {
  const mj = await (await fetch(base + 'model.json')).json();
  const groups = mj.weightsManifest, specs = [], bufs = [];
  for (const g of groups) { specs.push(...g.weights); for (const p of g.paths) bufs.push(await (await fetch(base + p)).arrayBuffer()); }
  const total = bufs.reduce((a, b) => a + b.byteLength, 0), data = new Uint8Array(total);
  let o = 0; for (const b of bufs) { data.set(new Uint8Array(b), o); o += b.byteLength; }
  if (size && size !== 513) {
    if ((size - 1) % 32 !== 0) throw new Error('size must be 32n+1');
    const f8 = (size - 1) / 8 + 1, f4 = (size - 1) / 4 + 1;
    const patch = { 'sub_1/x': [size], 'strided_slice_6': [size, size], 'ResizeBilinear/size': [f8, f8], 'decoder/ResizeBilinear/size': [f4, f4] };
    const dv = new DataView(data.buffer);
    let off = 0, done = 0;
    for (const w of specs) {
      const n = w.shape.reduce((a, b) => a * b, 1), q = w.quantization;
      const bytes = n * (q ? (q.dtype === 'uint8' ? 1 : 2) : 4);
      if (patch[w.name] && w.dtype === 'int32' && !q) { patch[w.name].forEach((v, i) => dv.setInt32(off + i * 4, v, true)); done++; }
      off += bytes;
    }
    if (done !== 4) throw new Error('model size patch failed');
    let pools = 0;
    for (const n of mj.modelTopology.node) {
      const k = n.op === 'AvgPool' && n.attr?.ksize?.list?.i;
      if (k && Number(k[1]) === 65 && Number(k[2]) === 65) { k[1] = String(f8); k[2] = String(f8); pools++; }
    }
    if (pools !== 1) throw new Error('model pool patch failed');
  }
  return tf.loadGraphModel(tf.io.fromMemory({ modelTopology: mj.modelTopology, weightSpecs: specs, weightData: data.buffer, format: mj.format, generatedBy: mj.generatedBy, convertedBy: mj.convertedBy }));
};
