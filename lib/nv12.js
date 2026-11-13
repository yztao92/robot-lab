/** NV12 (Y plane, then interleaved U,V per 2x2 block) → RGBA, BT.601 full range: the RK3576 ISP reports
 *  `Quantization: Full Range` on its self path (v4l2-ctl --get-fmt-video). */
export function nv12ToRgba(src, width, height, out = new Uint8ClampedArray(width * height * 4)) {
  if (width % 2 || height % 2) throw new RangeError('NV12 needs even dimensions');
  if (src.length < width * height * 3 / 2) throw new RangeError('NV12 frame is short');
  const uv = width * height;
  for (let y = 0; y < height; y++) {
    const yRow = y * width, cRow = uv + (y >> 1) * width;
    for (let x = 0; x < width; x++) {
      const c = cRow + (x & ~1);
      const l = src[yRow + x], u = src[c] - 128, v = src[c + 1] - 128;
      const o = (yRow + x) * 4;
      out[o] = l + 1.402 * v;
      out[o + 1] = l - 0.344 * u - 0.714 * v;
      out[o + 2] = l + 1.772 * u;
      out[o + 3] = 255;
    }
  }
  return out;
}
