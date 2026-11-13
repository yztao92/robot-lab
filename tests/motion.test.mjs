import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {MotionEngine, AXES, decodeServo, validateActions, neutralPose} from '../lib/motion.js';

const source = JSON.parse(await readFile(new URL('../data/ServoActionList.json', import.meta.url), 'utf8'));
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-7, `${a} ≠ ${b}`);
const servoStage = (enabled = {}, hold_ms = 0) => ({hold_ms, servos: Array.from({length: 8}, (_, id) =>
  ({id, en: 0, dir: 0, pos: 0, speed: 0, ...(enabled[id] || {})}))});
const action = (...stages) => [{action_id: 0, action: 'test', stages: stages.map((s, i) => ({...s, total_stages: stages.length, current_stage: i + 1}))}];

test('all 21 supplied actions complete within bounds with neutral end and explicit simulated events', () => {
  assert.equal(validateActions(source).length, 21);
  for (const a of source.ServoActionList) {
    const events = [], engine = new MotionEngine(source, {onEvent: e => events.push(e)});
    engine.setPose({head_yaw: 47, arm_l_forward: 175, arm_r_out: 34});
    engine.tick(2);
    engine.start(a.action_id);
    for (let i = 0; i < 3000 && engine.status === 'running'; i++) {
      engine.tick(1 / 60);
      for (const axis of AXES) assert.ok(engine.pose[axis.key] >= axis.min && engine.pose[axis.key] <= axis.max);
    }
    assert.equal(engine.status, 'completed');
    assert.deepEqual(engine.pose, neutralPose());
    assert.equal(events.filter(e => e.type === 'stage_completed').length, a.stages.length);
    assert.equal(events.at(-1).type, 'action_completed');
    assert.ok(events.every(e => e.simulated && e.source === 'simulator'));
    assert.ok(!Object.keys(engine.pose).some(k => /base/.test(k)));
  }
});

test('protocol signs match HTML and disabled fields really are ignored', () => {
  assert.equal(decodeServo({id: 1, en: 1, dir: 0, pos: 10, speed: 60}).value, 10);
  assert.equal(decodeServo({id: 7, en: 1, dir: 0, pos: 180, speed: 120}).value, 180);
  assert.equal(decodeServo({id: 0, en: 1, dir: 0, pos: 30, speed: 60}).value, -30);
  assert.equal(decodeServo({id: 2, en: 0, dir: 'ignored', pos: Infinity, speed: 0}), null);
  assert.equal(decodeServo({id: 3, en: 0}), null);
  assert.throws(() => decodeServo({id: 3, en: 1}), /DOA/);
  assert.throws(() => decodeServo({id: 4, en: 1, dir: 0, pos: 10, speed: 90}), /outside/);
});

test('en=0 holds preceding pose instead of returning to neutral', () => {
  const engine = new MotionEngine(action(servoStage({2: {en: 1, dir: 1, pos: 30, speed: 30}}), servoStage({}, 1000)));
  engine.start(0);
  engine.tick(1.5);
  assert.equal(engine.stageIndex, 1);
  assert.equal(engine.pose.head_yaw, 30);
  close(engine.holdRemaining, 0.5);
  engine.tick(0.5);
  assert.equal(engine.pose.head_yaw, 30);
  assert.equal(engine.status, 'completed');
});

test('holding starts after slowest axis arrives, with deterministic large ticks', () => {
  const fixture = action(servoStage({2: {en: 1, dir: 1, pos: 60, speed: 30}, 4: {en: 1, dir: 1, pos: 30, speed: 60}}, 1000));
  const events = [], engine = new MotionEngine(fixture, {onEvent: e => events.push(e)});
  engine.start(0);
  engine.tick(1);
  assert.equal(engine.phase, 'moving');
  assert.equal(engine.pose.arm_r_out, 30);
  assert.equal(engine.pose.head_yaw, 30);
  assert.equal(engine.holdRemaining, 1);
  engine.tick(1.25);
  close(engine.holdRemaining, 0.75);
  close(events.find(e => e.type === 'simulated_arrival').time, 2);
  engine.tick(0.75);
  assert.equal(engine.status, 'completed');
  close(engine.elapsed, 3);
  const oneTick = new MotionEngine(fixture); oneTick.start(0); oneTick.tick(100);
  assert.deepEqual(oneTick.snapshot(), engine.snapshot());
});

test('pause freezes pose and hold time, stop freezes actual pose, restart initializes smoothly', () => {
  const engine = new MotionEngine(source);
  engine.setPose({arm_l_forward: 180}); engine.tick(0.5);
  assert.equal(engine.pose.arm_l_forward, 60);
  engine.pause(); const before = engine.snapshot(); engine.tick(100);
  assert.deepEqual(engine.snapshot(), before);
  engine.resume(); engine.tick(0.25);
  assert.equal(engine.pose.arm_l_forward, 90);
  engine.stop(); engine.tick(100);
  assert.equal(engine.pose.arm_l_forward, 90);
  assert.equal(engine.target.arm_l_forward, 90);
  engine.start(1);
  assert.equal(engine.stageIndex, 0);
  assert.equal(engine.pose.arm_l_forward, 90);
  engine.tick(0.25);
  assert.equal(engine.pose.arm_l_forward, 60);
  engine.reset(); engine.tick(0.25);
  assert.equal(engine.pose.arm_l_forward, 30);
  engine.tick(0.25);
  assert.deepEqual(engine.pose, neutralPose());
});

test('180 degree arm uses linear displacement, no shortest wrap', () => {
  const engine = new MotionEngine(source);
  engine.setPose({arm_l_forward: -20}); engine.tick(1);
  engine.setPose({arm_l_forward: 180}); engine.tick(1);
  assert.equal(engine.pose.arm_l_forward, 100);
  engine.tick(2 / 3);
  assert.equal(engine.pose.arm_l_forward, 180);
});

test('rapid manual axis edits preserve pending goals before the next frame', () => {
  const engine = new MotionEngine(source);
  engine.setPose({head_yaw: 60}); engine.tick(1);
  engine.setPose({head_yaw: 0});
  engine.setPose({head_pitch_down: -30});
  assert.equal(engine.pose.head_yaw, 60);
  assert.equal(engine.target.head_yaw, 0);
  assert.equal(engine.target.head_pitch_down, -30);
  engine.tick(1);
  assert.deepEqual(engine.pose, {...neutralPose(), head_pitch_down: -30});
});

test('manual edits interrupt actions and hold untouched joints at their actual positions', () => {
  const engine = new MotionEngine(action(servoStage({2: {en: 1, dir: 1, pos: 60, speed: 30}})));
  engine.start(0); engine.tick(0.5);
  assert.equal(engine.pose.head_yaw, 15);
  assert.equal(engine.target.head_yaw, 60);
  engine.setPose({head_pitch_down: -30});
  assert.equal(engine.mode, 'manual');
  assert.equal(engine.action, null);
  assert.equal(engine.target.head_yaw, 15);
  engine.tick(1);
  assert.deepEqual(engine.pose, {...neutralPose(), head_yaw: 15, head_pitch_down: -30});
});

test('manual edits after stop do not resume a discarded pending goal', () => {
  const engine = new MotionEngine(source);
  engine.setPose({head_yaw: 60}); engine.tick(0.25);
  engine.stop();
  engine.setPose({head_pitch_down: -30});
  assert.equal(engine.target.head_yaw, 15);
  engine.tick(1);
  assert.deepEqual(engine.pose, {...neutralPose(), head_yaw: 15, head_pitch_down: -30});
});

test('a manual edit resumes paused manual goals without losing untouched targets', () => {
  const engine = new MotionEngine(source);
  engine.setPose({head_yaw: 60}); engine.tick(0.25); engine.pause();
  engine.setPose({head_pitch_down: -30});
  assert.equal(engine.paused, false);
  assert.equal(engine.target.head_yaw, 60);
  engine.tick(1);
  assert.deepEqual(engine.pose, {...neutralPose(), head_yaw: 60, head_pitch_down: -30});
});

test('invalid requests are atomic; manual clamp must be requested; base remains separate', () => {
  const engine = new MotionEngine(source); engine.start(1); engine.tick(0.1);
  const before = engine.snapshot();
  assert.throws(() => engine.setPose({head_yaw: 10, head_pitch_down: 13}), /outside/);
  assert.throws(() => engine.setPose({base_yaw: 90}), /Unknown expression/);
  assert.throws(() => engine.start(999), /Unknown action/);
  assert.throws(() => engine.tick(NaN), /dt/);
  assert.deepEqual(engine.snapshot(), before);
  engine.setPose({head_pitch_down: 99}, {clamp: true}); engine.tick(10);
  assert.equal(engine.pose.head_pitch_down, 12);
  const bad = structuredClone(source); bad.ServoActionList[0].stages[0].servos[3].en = 1;
  assert.throws(() => new MotionEngine(bad), /DOA/);
});

test('zero duration stages terminate and frame partition preserves the result', () => {
  const empty = new MotionEngine(action(...Array.from({length: 100}, () => servoStage())));
  empty.start(0); assert.equal(empty.status, 'completed');
  const a = new MotionEngine(source), b = new MotionEngine(source);
  a.start(17); b.start(17); a.tick(100);
  for (let i = 0; i < 10000 && b.status === 'running'; i++) b.tick(0.01);
  assert.deepEqual(a.pose, b.pose); close(a.elapsed, b.elapsed);
});
