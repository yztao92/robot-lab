import test from 'node:test';
import assert from 'node:assert/strict';
import {rotation} from '../lib/rotate.js';

const place = ({matrix: [a, b, c, d, e, f]}, x, y) => [a * x + c * y + e, b * x + d * y + f];

test('each angle turns the image clockwise and stays inside the canvas', () => {
  const w = 640, h = 360;
  // Where the source's four corners land: top-left, top-right, bottom-right, bottom-left.
  const corners = deg => [[0, 0], [w, 0], [w, h], [0, h]].map(([x, y]) => place(rotation(deg, w, h), x, y));
  assert.deepEqual(corners(0), [[0, 0], [w, 0], [w, h], [0, h]]);
  assert.deepEqual(corners(90), [[h, 0], [h, w], [0, w], [0, 0]]);
  assert.deepEqual(corners(180), [[w, h], [0, h], [0, 0], [w, 0]]);
  assert.deepEqual(corners(270), [[0, w], [0, 0], [h, 0], [h, w]]);
});

test('a quarter turn swaps the canvas size; an unknown angle draws upright', () => {
  assert.deepEqual([rotation(90, 640, 360).width, rotation(90, 640, 360).height], [360, 640]);
  assert.deepEqual([rotation(270, 640, 360).width, rotation(270, 640, 360).height], [360, 640]);
  assert.deepEqual([rotation(180, 640, 360).width, rotation(180, 640, 360).height], [640, 360]);
  assert.deepEqual(rotation(45, 640, 360), rotation(0, 640, 360));
});
