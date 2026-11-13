import test from 'node:test';
import assert from 'node:assert/strict';
import {nv12ToRgba} from '../lib/nv12.js';

test('NV12 full-range black, white and a saturated chroma convert as BT.601', () => {
  // 4x2: left 2x2 block black (Y0, neutral chroma), right block white (Y255, neutral chroma).
  const black = nv12ToRgba(Uint8Array.from([0, 0, 255, 255, 0, 0, 255, 255, 128, 128, 128, 128]), 4, 2);
  assert.deepEqual([...black.slice(0, 4)], [0, 0, 0, 255]);
  assert.deepEqual([...black.slice(8, 12)], [255, 255, 255, 255]);
  assert.deepEqual([...black.slice(16, 20)], [0, 0, 0, 255], 'row two shares the first chroma row');
  // A mid grey stays that grey: limited-range maths would have stretched it to 130.
  assert.equal(nv12ToRgba(Uint8Array.from([128, 128, 128, 128, 128, 128]), 2, 2)[0], 128);
  const red = nv12ToRgba(Uint8Array.from([76, 76, 76, 76, 85, 255]), 2, 2);
  assert.ok(red[0] > 240 && red[1] < 10 && red[2] < 10, `red came out ${[...red.slice(0, 3)]}`);
});

test('odd sizes and short frames are refused', () => {
  assert.throws(() => nv12ToRgba(new Uint8Array(6), 3, 2), RangeError);
  assert.throws(() => nv12ToRgba(new Uint8Array(5), 2, 2), RangeError);
});
