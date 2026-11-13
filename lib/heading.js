/** World-frame, robot-left-positive heading for the virtual base only. */
export class HeadingController {
  constructor(speed = 60) { this.current = 0; this.target = 0; this.speed = speed; }
  setTarget(degrees) {
    if (!Number.isFinite(degrees) || degrees < 0 || degrees >= 360) throw new RangeError('DOA 角度必须在 0（含）至 360（不含）之间');
    this.target = degrees;
  }
  tick(dt) {
    if (!Number.isFinite(dt) || dt < 0) throw new RangeError('Invalid time step');
    const delta = ((this.target - this.current + 540) % 360) - 180;
    const step = Math.min(Math.abs(delta), this.speed * dt) * Math.sign(delta);
    this.current = (this.current + step + 360) % 360;
    if (Math.abs(delta) < 1e-8) this.current = this.target;
    return this.current;
  }
  stop() { this.target = this.current; }
}
