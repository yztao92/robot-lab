import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const rig = JSON.parse(readFileSync(new URL('../config/rig.json', import.meta.url)));
const asset = JSON.parse(readFileSync(new URL('../assets/robot-mesh.json', import.meta.url)));
const branches = ['head', 'left_arm', 'right_arm'];
const add = (a, b) => a.map((v, i) => v + b[i]);
const subtract = (a, b) => a.map((v, i) => v - b[i]);
const rotate = (point, axis, angle) => {
  const c = Math.cos(angle), s = Math.sin(angle);
  const dot = point.reduce((v, p, i) => v + p * axis[i], 0);
  const cross = [axis[1] * point[2] - axis[2] * point[1], axis[2] * point[0] - axis[0] * point[2], axis[0] * point[1] - axis[1] * point[0]];
  return point.map((v, i) => v * c + cross[i] * s + axis[i] * dot * (1 - c));
};

test('STP neutral placement retains every mesh exactly once', () => {
  assert.equal(asset.meshes.length, 27);
  const owners = [...rig.base.fixed_meshes, ...rig.base.rotating_meshes];
  assert.equal(owners.length, 27);
  assert.equal(new Set(owners).size, 27);
  assert.deepEqual([...owners].sort((a, b) => a - b), Array.from({ length: 27 }, (_, i) => i));
  const moving = branches.flatMap(branch => rig[branch].meshes);
  assert.equal(new Set(moving).size, moving.length, 'joint branches must not share a mesh');
  for (const branch of branches) {
    const spec = rig[branch];
    for (const meshIndex of spec.meshes) {
      assert(rig.base.rotating_meshes.includes(meshIndex));
      const points = asset.meshes[meshIndex].attributes.position.array;
      for (let i = 0; i < points.length; i += 3) {
        const original = points.slice(i, i + 3);
        const local = subtract(original, spec.origin);
        const recovered = add(add(local, subtract(spec.origin, rig.base.origin)), rig.base.origin);
        for (let j = 0; j < 3; j++) assert(Math.abs(original[j] - recovered[j]) < 1e-9);
      }
    }
  }
});

test('positive semantic angles follow robot-self directions', () => {
  assert.equal(rig.coordinate_system.forward, '-Z');
  assert.equal(rig.coordinate_system.robot_left, '-X');
  const cases = [
    ['left_arm', 'arm_l_forward', [0, -35, 0], 2, -1],
    ['right_arm', 'arm_r_forward', [0, -35, 0], 2, -1],
    ['left_arm', 'arm_l_out', [0, -35, 0], 0, -1],
    ['right_arm', 'arm_r_out', [0, -35, 0], 0, 1],
    ['head', 'head_roll', [0, 30, 0], 0, -1],
    ['head', 'head_yaw', [0, 0, -30], 0, -1],
    ['head', 'head_pitch_down', [0, 0, -30], 1, -1]
  ];
  for (const [branch, key, point, direction, sign] of cases) {
    const axis = rig[branch].axes[key];
    assert(Math.abs(Math.hypot(...axis) - 1) < 1e-12, `${key} needs a unit axis`);
    assert(rotate(point, axis, Math.PI / 6)[direction] * sign > 0, key);
  }
  assert(rotate([0, 0, -1], rig.base.axis, Math.PI / 2)[0] < -0.999);
});

test('screen has real front triangles and supports undistorted contain fit', () => {
  const screen = asset.meshes[rig.screen.mesh];
  const p = screen.attributes.position.array, normals = screen.attributes.normal.array, indices = screen.index.array;
  let count = 0;
  for (let i = 0; i < indices.length; i += 3) {
    if (indices.slice(i, i + 3).every(index => normals[index * 3 + 2] < -0.99 && Math.abs(p[index * 3 + 2] - rig.screen.front_z) < 0.1)) count++;
  }
  assert(count > 0);
  const xs = p.filter((_, i) => i % 3 === 0), ys = p.filter((_, i) => i % 3 === 1);
  const width = Math.max(...xs) - Math.min(...xs), height = Math.max(...ys) - Math.min(...ys);
  assert(Math.abs(width - 73.76) < 0.01);
  assert(Math.abs(height - 51.3) < 0.01);
  assert.equal(rig.screen.fit, 'contain');
  assert.equal(rig.screen.effective_display_area_verified, false);
  const scale = Math.min(width / 682, height / 422);
  assert(682 * scale <= width + 1e-9 && 422 * scale <= height + 1e-9);
  assert(Math.abs((682 * scale) / (422 * scale) - 682 / 422) < 1e-12);
});

test('preview calibration and material uncertainty remain explicit', () => {
  assert.equal(rig.status, 'preview_hypothesis_not_mechanically_calibrated');
  for (const branch of [...branches, 'base']) assert.equal(rig[branch].verified, false);
  const colored = ['blue', 'silver', 'black'].flatMap(name => rig.materials[`${name}_meshes`]);
  assert.equal(new Set(colored).size, colored.length);
});
