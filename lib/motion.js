/**
 * Deterministic seven-axis preview player for the supplied PY32 action JSON.
 * Angles here follow 展示.html: pitch down, roll left, yaw left, arms forward/out.
 * Arrival events are simulated, never motor feedback. DOA/base axis 3 is excluded.
 */
export const AXES = Object.freeze([
  {id: 0, key: 'head_pitch_down', label: '头部俯仰', min: -30, max: 12, speed: 60},
  {id: 1, key: 'head_roll', label: '头部侧倾', min: -10, max: 10, speed: 60},
  {id: 2, key: 'head_yaw', label: '头部转向', min: -60, max: 60, speed: 60},
  {id: 4, key: 'arm_r_out', label: '右臂外张', min: -6, max: 35, speed: 90},
  {id: 5, key: 'arm_r_forward', label: '右臂前抬', min: -20, max: 180, speed: 120},
  {id: 6, key: 'arm_l_out', label: '左臂外张', min: -6, max: 35, speed: 90},
  {id: 7, key: 'arm_l_forward', label: '左臂前抬', min: -20, max: 180, speed: 120},
].map(Object.freeze));

const BY_ID = new Map(AXES.map(axis => [axis.id, axis]));
const BY_KEY = new Map(AXES.map(axis => [axis.key, axis]));
const EPS = 1e-9;
const finite = n => typeof n === 'number' && Number.isFinite(n);
export const neutralPose = () => Object.fromEntries(AXES.map(axis => [axis.key, 0]));

/** Decode one slot; disabled slots deliberately ignore dir/pos/speed. */
export function decodeServo(servo) {
  if (!servo || !Number.isInteger(servo.id) || servo.id < 0 || servo.id > 7) {
    throw new TypeError('Servo id must be an integer in 0..7');
  }
  if (servo.en !== 0 && servo.en !== 1) throw new TypeError('Servo en must be 0 or 1');
  if (!servo.en) return null;
  if (servo.id === 3) throw new RangeError('Base axis 3 belongs to DOA, not expression actions');
  if (servo.dir !== 0 && servo.dir !== 1) throw new TypeError('Enabled servo dir must be 0 or 1');
  if (!finite(servo.pos) || servo.pos < 0) throw new RangeError('Servo pos must be a finite nonnegative magnitude');
  if (!finite(servo.speed) || servo.speed <= 0) throw new RangeError('Enabled servo speed must be positive');
  const axis = BY_ID.get(servo.id);
  const semanticSign = servo.id === 1 || servo.id === 7 ? -1 : 1;
  const value = servo.pos === 0 ? 0 : servo.pos * (servo.dir === 1 ? 1 : -1) * semanticSign;
  if (value < axis.min || value > axis.max) throw new RangeError(`${axis.key} target ${value} outside ${axis.min}..${axis.max}`);
  return {id: servo.id, key: axis.key, value, speed: servo.speed};
}

/** Validate first so a malformed action never partially changes the preview. */
export function validateActions(data) {
  const actions = Array.isArray(data) ? data : data?.ServoActionList;
  if (!Array.isArray(actions)) throw new TypeError('Expected ServoActionList or an action array');
  if (!Array.isArray(data) && data.total_actions !== actions.length) throw new RangeError('total_actions does not match action count');
  const ids = new Set();
  for (const action of actions) {
    if (!Number.isInteger(action.action_id) || action.action_id < 0 || ids.has(action.action_id)) throw new TypeError('Action ids must be unique nonnegative integers');
    ids.add(action.action_id);
    if (!Array.isArray(action.stages) || !action.stages.length) throw new TypeError('Each action must have stages');
    action.stages.forEach((stage, index) => {
      if (stage.total_stages !== action.stages.length || stage.current_stage !== index + 1) throw new RangeError('Stage numbering/count mismatch');
      if (!finite(stage.hold_ms) || stage.hold_ms < 0) throw new RangeError('hold_ms must be finite and nonnegative');
      if (!Array.isArray(stage.servos) || stage.servos.length !== 8 || stage.servos.some((servo, id) => servo.id !== id)) throw new TypeError('Every stage must contain ordered fixed slots 0..7');
      stage.servos.forEach(decodeServo);
    });
  }
  return actions;
}

export class MotionEngine {
  constructor(actions, {onEvent = () => {}} = {}) {
    // Clone to prevent a caller changing validated data during a movement.
    this.actions = JSON.parse(JSON.stringify(validateActions(actions)));
    this.onEvent = onEvent;
    this.pose = neutralPose();
    this.target = neutralPose();
    this.speeds = Object.fromEntries(AXES.map(axis => [axis.key, axis.speed]));
    this.status = 'idle';
    this.phase = 'idle';
    this.paused = false;
    this.action = null;
    this.stageIndex = -1;
    this.holdRemaining = 0;
    this.time = 0;
    this.elapsed = 0;
    this.mode = 'idle';
  }

  _emit(type, extra = {}) {
    this.onEvent({type, simulated: true, source: 'simulator', actionId: this.action?.action_id ?? null,
      stageIndex: this.stageIndex, time: this.time, ...extra});
  }

  _interrupt(reason) {
    if (this.status === 'running' || this.paused) this._emit('interrupted', {reason});
    this.paused = false;
    this.holdRemaining = 0;
  }

  start(actionId) {
    const action = this.actions.find(item => item.action_id === actionId);
    if (!action) throw new RangeError(`Unknown action id: ${actionId}`);
    this._interrupt('new_action');
    this.action = action;
    this.stageIndex = 0;
    this.status = 'running';
    this.mode = 'action';
    this.elapsed = 0;
    this._emit('action_started');
    this._beginStage();
    this.tick(0);
    return this.snapshot();
  }

  _beginStage() {
    const stage = this.action.stages[this.stageIndex];
    // en=0 holds actual current pose, including when starting after interruption.
    this.target = {...this.pose};
    for (const servo of stage.servos) {
      const decoded = decodeServo(servo);
      if (!decoded) continue;
      this.target[decoded.key] = decoded.value;
      this.speeds[decoded.key] = decoded.speed;
    }
    this.holdRemaining = stage.hold_ms / 1000;
    this.phase = 'moving';
    this._emit('stage_started', {target: {...this.target}, holdSeconds: this.holdRemaining});
  }

  /** Advance simulated time; independent of frame rate, including long frames. */
  tick(dt) {
    if (!finite(dt) || dt < 0) throw new RangeError('dt must be finite nonnegative seconds');
    if (this.status !== 'running' || this.paused) return this.snapshot();
    let remaining = dt;
    const maxTransitions = (this.action?.stages.length ?? 1) * 3 + 6;
    let transitions = 0;
    while (this.status === 'running') {
      if (++transitions > maxTransitions) throw new Error('Motion transition guard exceeded');
      if (this.phase === 'moving') {
        let duration = 0;
        for (const axis of AXES) duration = Math.max(duration, Math.abs(this.target[axis.key] - this.pose[axis.key]) / this.speeds[axis.key]);
        const used = Math.min(remaining, duration);
        for (const axis of AXES) {
          const key = axis.key, delta = this.target[key] - this.pose[key];
          const step = this.speeds[key] * used;
          this.pose[key] = Math.abs(delta) <= step + EPS ? this.target[key] : this.pose[key] + Math.sign(delta) * step;
        }
        remaining = Math.max(0, remaining - used);
        this.time += used;
        this.elapsed += used;
        if (duration > used + EPS) break;
        this.pose = {...this.target};
        this.phase = 'holding';
        this._emit('simulated_arrival', {pose: {...this.pose}});
      }
      if (this.phase === 'holding') {
        const used = Math.min(remaining, this.holdRemaining);
        this.holdRemaining = Math.max(0, this.holdRemaining - used);
        remaining = Math.max(0, remaining - used);
        this.time += used;
        this.elapsed += used;
        if (this.holdRemaining > EPS) break;
        this.holdRemaining = 0;
        if (this.mode === 'action') {
          this._emit('stage_completed');
          if (this.stageIndex + 1 < this.action.stages.length) {
            this.stageIndex++;
            this._beginStage();
            continue;
          }
        }
        this.status = 'completed';
        this.phase = 'completed';
        this._emit(this.mode === 'action' ? 'action_completed' : this.mode === 'reset' ? 'reset_completed' : 'pose_completed');
      }
    }
    return this.snapshot();
  }

  pause() {
    if (this.status === 'running') {
      this.paused = true;
      this.status = 'paused';
      this._emit('paused');
    }
    return this.snapshot();
  }

  resume() {
    if (this.paused) {
      this.paused = false;
      this.status = 'running';
      this._emit('resumed');
    }
    return this.snapshot();
  }

  stop() {
    this._interrupt('stop');
    this.target = {...this.pose};
    this.status = 'stopped';
    this.phase = 'stopped';
    this._emit('stopped');
    return this.snapshot();
  }

  reset() {
    return this._poseMove(neutralPose(), 'reset');
  }

  /** Move a subset of semantic joints; validation is atomic, clamp is explicit. */
  setPose(patch, {clamp = false} = {}) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new TypeError('Pose patch must be an object');
    const checked = {};
    for (const [key, value] of Object.entries(patch)) {
      const axis = BY_KEY.get(key);
      if (!axis) throw new RangeError(`Unknown expression axis: ${key}`);
      if (!finite(value)) throw new TypeError(`${key} must be a finite angle`);
      if (!clamp && (value < axis.min || value > axis.max)) throw new RangeError(`${key} outside ${axis.min}..${axis.max}`);
      checked[key] = Math.min(axis.max, Math.max(axis.min, value));
    }
    // Rapid manual edits share pending goals; interrupting other modes holds actual joints.
    const base = this.mode === 'manual' && (this.status === 'running' || this.status === 'paused')
      ? this.target : this.pose;
    return this._poseMove({...base, ...checked}, 'manual');
  }

  _poseMove(target, mode) {
    this._interrupt(mode);
    this.action = null;
    this.stageIndex = -1;
    this.mode = mode;
    this.status = 'running';
    this.phase = 'moving';
    this.target = {...target};
    this.speeds = Object.fromEntries(AXES.map(axis => [axis.key, axis.speed]));
    this.elapsed = 0;
    this._emit(mode === 'reset' ? 'reset_started' : 'pose_started', {target: {...target}});
    this.tick(0);
    return this.snapshot();
  }

  snapshot() {
    return {pose: {...this.pose}, target: {...this.target}, speeds: {...this.speeds},
      status: this.status, phase: this.phase, paused: this.paused, mode: this.mode,
      actionId: this.action?.action_id ?? null, actionName: this.action?.action ?? null,
      stageIndex: this.stageIndex, stageCount: this.action?.stages.length ?? 0,
      holdRemaining: this.holdRemaining, elapsed: this.elapsed, time: this.time,
      simulated: true};
  }
}
