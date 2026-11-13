/**
 * Controller integration only: actual app.js with real motion/heading engines.
 * Tiny isolated DOM, media and SSE doubles; no browser, WebGL or visual assertions.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {MotionEngine, AXES, neutralPose} from '../lib/motion.js';
import {HeadingController} from '../lib/heading.js';

const [appSource, htmlSource, catalogJSON, protocolJSON] = await Promise.all([
  readFile(new URL('../app.js', import.meta.url), 'utf8'),
  readFile(new URL('../index.html', import.meta.url), 'utf8'),
  readFile(new URL('../data/catalog.json', import.meta.url), 'utf8'),
  readFile(new URL('../data/ServoActionList.json', import.meta.url), 'utf8'),
]);
const appWithoutImports = appSource.replace(/^import[^\n]*\n/gm, '');

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  emit(type, event = {}) {
    this['on' + type]?.(event);
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
}

function makeDOM() {
  const nodes = new Map(), dataNodes = [];
  class Element extends Events {
    constructor(tagName = 'div') {
      super(); this.tagName = tagName.toUpperCase(); this.children = []; this.parent = null;
      this.dataset = {}; this.attributes = {}; this.className = ''; this.hidden = false;
      this.disabled = false; this._text = ''; this._value = ''; this._id = '';
      this.classList = {
        contains: name => this.className.split(/\s+/).includes(name),
        add: name => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), name])].join(' '); },
        remove: name => { this.className = this.className.split(/\s+/).filter(x => x !== name).join(' '); },
        toggle: (name, force) => {
          const present = force ?? !this.classList.contains(name);
          this.classList[present ? 'add' : 'remove'](name); return present;
        },
      };
    }
    set id(value) { this._id = value; nodes.set(value, this); }
    get id() { return this._id; }
    set value(value) { this._value = String(value); }
    get value() { return this._value; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    get lastChild() { return this.children.at(-1); }
    get lastElementChild() { return this.children.at(-1); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    prepend(...children) { for (const child of children) child.parent = this; this.children.unshift(...children); }
    replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
    querySelector(selector) {
      for (const child of this.children) {
        if (selector.startsWith('.') && child.classList.contains(selector.slice(1))) return child;
        const found = child.querySelector(selector); if (found) return found;
      }
      return null;
    }
    click() { if (!this.disabled) return this.onclick?.({target: this}); }
  }
  class Video extends Element {
    constructor() { super('video'); this.paused = true; this.currentTime = 0; this.playbackRate = 1; this.loop = false; this.playCalls = 0; }
    load() { this.paused = true; this.currentTime = 0; }
    play() { this.paused = false; this.playCalls++; return Promise.resolve(); }
    pause() { this.paused = true; }
  }
  for (const match of htmlSource.matchAll(/<([a-z][\w-]*)\b([^>]*)>/g)) {
    const [, tag, attributes] = match;
    const id = attributes.match(/\bid="([^"]+)"/)?.[1];
    const data = [...attributes.matchAll(/\bdata-(\w+)="([^"]+)"/g)];
    if (!id && !data.length) continue;
    const node = tag === 'video' ? new Video() : new Element(tag);
    if (id) node.id = id;
    node.className = attributes.match(/\bclass="([^"]*)"/)?.[1] || '';
    node.value = attributes.match(/\bvalue="([^"]*)"/)?.[1] || '';
    node.hidden = /\bhidden\b/.test(attributes); node.disabled = /\bdisabled\b/.test(attributes);
    for (const [, key, value] of data) node.dataset[key] = value;
    if (data.length) dataNodes.push(node);
  }
  // Select defaults and the text-node child relied upon by connection updates.
  nodes.get('speed').value = '1'; nodes.get('event-example').value = 'action';
  nodes.get('connection').append(new Element('i'), new Element('#text'));
  const empty = new Element('p'); empty.className = 'empty'; nodes.get('event-list').append(empty);
  const document = new Events();
  Object.assign(document, {
    hidden: false, activeElement: null,
    getElementById: id => nodes.get(id) || null,
    createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
    querySelectorAll: selector => {
      const key = selector.match(/^\[data-(\w+)\]$/)?.[1];
      return key ? dataNodes.filter(node => key in node.dataset) : [];
    },
  });
  return {document, nodes};
}

async function launch({failFetch=false}={}) {
  const {document, nodes} = makeDOM();
  const streams = [], posts = [], window = new Events();
  let clock = 0, raf = null, reloadCount = 0;
  window.location = {reload: () => { reloadCount++; }};
  class EventSource extends Events {
    constructor(url) { super(); this.url = url; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
  }
  class RobotView {
    constructor(container) { assert.equal(container.id, 'viewport'); this.renderCount = 0; }
    async load() { this.loaded = true; }
    setFace(video) { this.video = video; }
    setPose(pose) { this.pose = {...pose}; }
    setBaseHeading(angle) { this.heading = angle; }
    resetCamera(view) { this.cameraView = view; }
    render() { this.renderCount++; }
  }
  const context = vm.createContext({
    document, window, EventSource, RobotView, MotionEngine, HeadingController, AXES,
    console, Blob, URL, setTimeout, performance: {now: () => clock},
    requestAnimationFrame: callback => { raf = callback; return 1; },
    fetch: async (url, options) => {
      if (failFetch) throw new TypeError('Failed to fetch');
      if (url === '/data/catalog.json') return {ok: true, json: async () => JSON.parse(catalogJSON)};
      if (url === '/data/ServoActionList.json') return {ok: true, json: async () => JSON.parse(protocolJSON)};
      if (url === '/api/events' && options?.method === 'POST') {
        const event = {...JSON.parse(options.body), seq: posts.length + 1}; posts.push(event);
        return {ok: true, json: async () => ({ok: true, event})};
      }
      throw new Error(`Unexpected fetch ${url}`);
    },
  });
  vm.runInContext(appWithoutImports + '\n;globalThis.__controller = {get engine(){return engine},get heading(){return heading},get view(){return view},get faceOwner(){return faceOwner},get events(){return eventLog}};', context, {filename: 'app.js'});
  await new Promise(resolve => setImmediate(resolve));
  const get = id => { const node = nodes.get(id); assert.ok(node, `DOM id ${id}`); return node; };
  if (!failFetch) {
    assert.equal(get('error').hidden, true, get('error').textContent);
    assert.ok(raf, 'boot scheduled an animation frame');
  }
  const api = {
    ...context.__controller, controller: context.__controller, document, get, posts, window,
    video: get('face-video'), stream: streams[0],
    get reloadCount() { return reloadCount; },
    frame(seconds) {
      clock += seconds * 1000;
      const video = get('face-video'); if (!video.paused) video.currentTime += seconds * video.playbackRate;
      const callback = raf; raf = null; callback(clock); assert.ok(raf, 'animation continues');
    },
    hidden(value) { document.hidden = value; document.emit('visibilitychange'); },
    signal(event) { streams[0].emit('message', {data: JSON.stringify({source: 'injected', ...event})}); },
  };
  return api;
}

function finishMotion(app) {
  for (let i = 0; i < 120 && app.engine.status === 'running'; i++) app.frame(0.5);
  assert.notEqual(app.engine.status, 'running', 'action finished');
}
const face = app => app.get('face-name').textContent.slice(0, 3);
const queueLogs = app => app.events.filter(e => e.type === 'sequence').map(e => e.description);

test('actual controller boots with catalog, seven controls, default face, stages, and bridge', async () => {
  const app = await launch(); app.stream.emit('open'); app.frame(0.1);
  assert.equal(app.get('startup-recovery').hidden, true);
  assert.equal(app.get('loading').hidden, true);
  assert.equal(app.get('selection-id').textContent, 'A02');
  assert.equal(app.get('library-list').children.length, 21);
  assert.equal(app.get('joint-controls').children.length, 7);
  assert.equal(app.get('stage-track').children.length, 3);
  assert.match(app.get('face-name').textContent, /^D01/);
  assert.equal(app.video.paused, false); assert.equal(app.video.loop, true);
  assert.equal(app.stream.url, '/api/events');
  assert.match(app.get('connection').textContent, /^在线$/);
  assert.equal(app.engine.status, 'idle'); assert.ok(app.view.renderCount > 0);
});

test('action controls use real motion and DOA continues independently during seven-axis pause', async () => {
  const app = await launch();
  app.get('play').click(); app.signal({type: 'doa', frame: 'world', angle_deg: 120});
  app.frame(0.25);
  assert.equal(app.engine.action.action_id, 1); assert.equal(app.engine.stageIndex, 1);
  assert.equal(app.engine.pose.arm_l_forward, 30); assert.equal(app.heading.current, 15);
  assert.match(app.get('face-name').textContent, /^F02/);
  app.get('pause').click();
  const pose = {...app.engine.pose}; app.frame(0.25);
  assert.deepEqual(app.engine.pose, pose); assert.equal(app.heading.current, 30);
  assert.equal(app.video.paused, true);
  app.get('pause').click(); assert.equal(app.video.paused, false);
  app.frame(20);
  assert.equal(app.engine.status, 'completed'); assert.deepEqual(app.engine.pose, neutralPose());
  assert.equal(app.heading.current, 120); assert.match(app.get('face-name').textContent, /^D01/);
});

test('state override retains screen ownership after the current action completes', async () => {
  const app = await launch(); app.get('play').click(); app.frame(0.1);
  app.signal({type: 'state', state_id: 'T03'}); const source = app.video.src;
  assert.equal(app.controller.faceOwner, 'signal'); app.frame(20);
  assert.equal(app.engine.status, 'completed'); assert.equal(app.video.src, source);
  assert.match(app.get('face-name').textContent, /^T03/); assert.equal(app.video.loop, true);
});

test('hidden page freezes media and motion and resumes without a long-frame jump', async () => {
  const app = await launch(); app.get('play').click(); app.frame(0.2);
  const pose = {...app.engine.pose}, mediaTime = app.video.currentTime;
  app.hidden(true); assert.equal(app.video.paused, true); app.frame(12);
  assert.deepEqual(app.engine.pose, pose); assert.equal(app.video.currentTime, mediaTime);
  app.hidden(false); assert.equal(app.video.paused, false); app.frame(0.1);
  assert.equal(app.engine.pose.arm_l_forward, pose.arm_l_forward + 12);
  assert.ok(Math.abs(app.video.currentTime - mediaTime - 0.1) < 1e-8);
  app.get('pause').click(); app.hidden(true); app.frame(8); app.hidden(false);
  assert.equal(app.video.paused, true); assert.equal(app.engine.paused, true);
});

test('new hidden events defer media playback, and a hidden stop prevents resumption', async () => {
  const app = await launch(); app.hidden(true);
  app.signal({type: 'action', action_id: 'A19'}); app.frame(4);
  assert.equal(app.engine.action.action_id, 18); assert.equal(app.engine.elapsed, 0);
  assert.match(app.get('face-name').textContent, /^F19/); assert.equal(app.video.paused, true);
  app.signal({type: 'state', state_id: 'R02'}); assert.equal(app.video.paused, true);
  assert.match(app.get('face-name').textContent, /^F19/, 'the state waits for the running action');
  app.hidden(false); assert.equal(app.video.paused, false);
  app.video.onended(); finishMotion(app); assert.match(app.get('face-name').textContent, /^R02/);
  app.hidden(true); app.signal({type: 'stop'}); app.frame(4); app.hidden(false);
  assert.equal(app.video.paused, true); assert.equal(app.engine.status, 'stopped');
});

test('stop holds current pose and heading; reset returns both and restores idle face', async () => {
  const app = await launch(); app.get('play').click(); app.signal({type: 'doa', frame: 'world', angle_deg: 120}); app.frame(0.3);
  const pose = {...app.engine.pose}, heading = app.heading.current;
  app.get('stop').click(); app.frame(10);
  assert.deepEqual(app.engine.pose, pose); assert.equal(app.heading.current, heading); assert.equal(app.video.paused, true);
  app.get('reset-all').click(); app.frame(10);
  assert.deepEqual(app.engine.pose, neutralPose()); assert.equal(app.heading.current, 0);
  assert.match(app.get('face-name').textContent, /^D01/); assert.equal(app.video.paused, false);
});

test('SSE disconnect and gap stop playback; reopen waits for a new command', async () => {
  const app = await launch(); app.stream.emit('open'); app.get('play').click(); app.frame(0.2);
  app.stream.emit('error');
  assert.equal(app.engine.status, 'stopped'); assert.equal(app.video.paused, true);
  assert.match(app.get('error').textContent, /已断开/); const pose = {...app.engine.pose};
  assert.equal(app.get('startup-recovery').hidden, true, 'ordinary connection errors do not expose startup reload help');
  app.stream.emit('open'); app.frame(5);
  assert.deepEqual(app.engine.pose, pose); assert.equal(app.engine.status, 'stopped');
  app.signal({type: 'action', action_id: 'A03'}); app.frame(0.1); assert.equal(app.engine.status, 'running');
  app.stream.emit('gap'); assert.equal(app.engine.status, 'stopped'); assert.equal(app.video.paused, true);
  assert.match(app.get('error').textContent, /不连续/);
  app.window.emit('pagehide'); assert.equal(app.stream.closed, true);
});

test('failed startup explains unavailable resources and offers an explicit reload', async () => {
  const app = await launch({failFetch:true});
  assert.equal(app.get('error').hidden, false);
  assert.match(app.get('error').textContent, /本地服务不可用或预览资源未能加载/);
  assert.equal(app.get('startup-recovery').hidden, false);
  assert.equal(app.get('loading').hidden, true);
  assert.equal(app.get('play').disabled, true);
  assert.equal(app.reloadCount, 0);
  app.get('reload-preview').click();
  assert.equal(app.reloadCount, 1);
  assert.equal(app.stream, undefined, 'failed startup does not claim an event connection');
});

test('UI event submission forces injected provenance instead of accepting a board claim', async () => {
  const app = await launch();
  app.get('event-json').value = JSON.stringify({type: 'action', source: 'board', action_id: 'A02'});
  await app.get('send-event').click();
  assert.equal(app.posts.length, 1); assert.equal(app.posts[0].source, 'injected');
  assert.match(app.get('event-result').textContent, /已提交事件 #1/);
  assert.equal(app.engine.status, 'idle', 'POST acceptance alone does not execute; SSE reception does');
});

test('a board expression played once returns the screen to idle, not to its loop', async () => {
  const app = await launch();
  app.signal({type: 'expression', source: 'board', expression_id: 'F02', mode: 'once', emotion: 'joy'});
  assert.match(app.get('face-name').textContent, /^F02/); assert.equal(app.video.loop, false);
  app.video.onended();
  assert.match(app.get('face-name').textContent, /^D01/); assert.equal(app.video.loop, true);
  assert.equal(app.engine.status, 'idle', 'no action_id, no motion');
});

test('a board emotion with its paired action plays face and body together, then the next waits', async () => {
  const app = await launch();
  app.signal({type: 'expression', source: 'board', expression_id: 'F02', mode: 'once', action_id: 'A02', emotion: 'joy'});
  app.signal({type: 'expression', source: 'board', expression_id: 'F05', mode: 'once', action_id: 'A05', emotion: 'sad'});
  app.frame(0.1);
  assert.equal(face(app), 'F02'); assert.equal(app.engine.action.action_id, 1); assert.equal(app.engine.status, 'running');
  assert.match(app.get('live-queue').textContent, /播放中 F02 喜悦 \+ A02.*排队 1/);
  app.video.onended(); assert.equal(face(app), 'D01', 'the face is done and idles while the body finishes');
  finishMotion(app);
  assert.equal(face(app), 'F05'); assert.equal(app.engine.action.action_id, 4);
});

test('signalled actions play in sequence: the next waits for both the motion and the face', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'}); app.frame(0.1);
  app.signal({type: 'action', action_id: 'A03'});
  assert.equal(app.engine.action.action_id, 1, 'A03 did not cut A02 short'); assert.equal(face(app), 'F02');
  assert.match(app.get('live-queue').textContent, /播放中 A02.*排队 1/);
  finishMotion(app);
  assert.equal(app.engine.status, 'completed'); assert.equal(face(app), 'F02', 'still waiting for the face');
  app.video.onended(); app.frame(0.1);
  assert.equal(app.engine.action.action_id, 2); assert.equal(app.engine.status, 'running'); assert.equal(face(app), 'F03');
  app.video.onended(); finishMotion(app);
  assert.equal(face(app), 'D01', 'an action hands the screen back to idle'); assert.equal(app.get('live-queue').textContent, '队列空');
});

test('a face that finishes first waits for its motion before the next starts', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'}); app.video.onended();
  app.signal({type: 'expression', source: 'board', expression_id: 'F05', mode: 'once', emotion: 'surprise'});
  assert.equal(face(app), 'F02', 'the action face moved on to its own loop, not to F05');
  finishMotion(app); assert.equal(face(app), 'F05'); assert.equal(app.video.loop, false);
  app.video.onended(); assert.equal(face(app), 'D01');
});

test('repeats of what is playing or queued merge, and a full queue drops the oldest', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'});
  app.signal({type: 'action', action_id: 'A02'});
  assert.match(queueLogs(app).at(-1), /A02 .*与正在播放的相同，合并/);
  const ids = ['F03', 'F04', 'F05', 'F06', 'F07', 'F08', 'F09'];
  for (const id of ids) app.signal({type: 'expression', source: 'board', expression_id: id, mode: 'once'});
  app.signal({type: 'expression', source: 'board', expression_id: 'F09', mode: 'once'});
  assert.match(queueLogs(app).at(-3), /已满，丢弃最早排队的 F03/);
  assert.equal(queueLogs(app).filter(t => t.includes('丢弃')).length, 1);
  assert.match(queueLogs(app).at(-1), /与队尾相同，合并/);
  app.video.onended(); finishMotion(app);
  const played = [];
  for (let i = 0; i < 8 && face(app) !== 'D01'; i++) { played.push(face(app)); app.video.onended(); }
  assert.deepEqual(played, ids.slice(1));
});

test('holding screens wait for the queue to drain, and the latest one wins', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'});
  app.signal({type: 'state', state_id: 'R02'}); app.signal({type: 'state', state_id: 'T03'});
  app.signal({type: 'expression', expression_id: 'F19', mode: 'loop'});
  assert.equal(face(app), 'F02'); assert.match(app.get('live-queue').textContent, /播放中 A02/);
  app.video.onended(); finishMotion(app);
  assert.equal(face(app), 'F19'); assert.equal(app.video.loop, true); assert.equal(app.controller.faceOwner, 'signal');
  app.signal({type: 'state', state_id: 'R02'}); assert.equal(face(app), 'R02', 'nothing queued: shown at once');
});

test('stop, and any manual playback, clear the queue', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'}); app.signal({type: 'action', action_id: 'A03'});
  app.signal({type: 'stop'});
  assert.match(queueLogs(app).at(-1), /外部停止事件，清空 2 个/); app.video.onended?.(); app.frame(30);
  assert.equal(app.engine.status, 'stopped', 'A03 never started');
  app.signal({type: 'action', action_id: 'A02'}); app.signal({type: 'action', action_id: 'A03'});
  app.get('play').click(); assert.match(queueLogs(app).at(-1), /手动播放，清空 2 个/);
  app.video.onended(); finishMotion(app); assert.equal(app.engine.action.action_id, 1);
});

test('a face that never reports its end is skipped after a grace period', async () => {
  const app = await launch();
  app.signal({type: 'action', action_id: 'A02'}); app.signal({type: 'action', action_id: 'A03'});
  finishMotion(app); assert.equal(face(app), 'F02');
  for (let i = 0; i < 21; i++) app.frame(1);
  assert.match(queueLogs(app).at(-1), /A02 .*的表情 20 秒没播完，跳过/);
  assert.equal(face(app), 'F03'); assert.equal(app.engine.action.action_id, 2);
});

test('the status bar shows CPU busy share, with the load average kept for the tooltip', async () => {
  const app = await launch();
  app.signal({type: 'board_status', source: 'board', board_online: true, cpu_temp_c: 40.2, cpu_pct: 6, load_1m: 0.5, uptime_s: 600});
  assert.equal(app.get('board-system').textContent, '40°C · CPU 6% · 开机 10 分');
  assert.equal(app.get('board-system').classList.contains('warn'), false);
  assert.match(app.get('board-system').title, /8 个核合计.*1 分钟平均负载 0\.50（8 核满载为 8）/);
  app.signal({type: 'board_status', source: 'board', cpu_pct: 95});
  assert.equal(app.get('board-system').classList.contains('warn'), true);
  assert.match(app.get('board-system').title, /CPU 占用偏高/);
});

test('the perf panel shows exactly four metrics and keeps missing timings empty', async () => {
  const app = await launch();
  app.signal({type: 'metric', source: 'board', turn_id: 't1', metrics: {server_first_ms: 682, e2e_ms: 1431, doa_ms: 0.42, intent_ms: 3601, reply_s: 1.8}});
  app.signal({type: 'metric', source: 'board', turn_id: 't2', metrics: {server_first_ms: 558}, note: '人声没有高出环境声'});
  const summary = app.get('perf-summary').children.map(c => c.textContent);
  assert.match(summary[0], /^首词 中位1431 ms.*1 轮$/);
  assert.match(summary[1], /^服务端 中位682 ms.*1 轮$/, 'a turn missing its first word stays out of statistics until validated');
  const [newest] = app.get('perf-rows').children;
  assert.equal(newest.children[1].textContent, '-');
  assert.equal(newest.children[1].title, '未测到：人声没有高出环境声');
  assert.equal(newest.children[2].textContent, '558ms');
  assert.equal(app.get('perf-summary').children.length, 4);
  assert.equal(newest.children.length, 5);
  assert.equal(newest.children[4].textContent, '-');
  assert.equal(newest.children[4].title, '未测到同轮说话开始通知或 DOA 方向');
  assert.match(summary[3], /^DOA 中位0\.42 ms.*1 轮$/);
});

test('face positions are not logged and do not disturb the screen', async () => {
  const app = await launch();
  const before = app.events.length;
  app.signal({type: 'faces', source: 'board', faces: [{id: 1, bbox: [0, 0, 1, 1]}]});
  assert.equal(app.events.length, before);
  assert.equal(face(app), 'D01');
});

test('the status bar shows NPU load and says when the CPU is throttled', async () => {
  const app = await launch();
  app.signal({type: 'board_status', source: 'board', cpu_temp_c: 60, cpu_pct: 20, npu_pct: 7, cpu_mhz: 2208, cpu_mhz_full: 2208, uptime_s: 600});
  assert.equal(app.get('board-system').textContent, '60°C · CPU 20% · NPU 7% · 开机 10 分');
  assert.equal(app.get('board-system').classList.contains('warn'), false);
  app.signal({type: 'board_status', source: 'board', cpu_mhz: 408});
  assert.equal(app.get('board-system').textContent, '60°C · CPU 20% · NPU 7% · 降频 408 MHz · 开机 10 分');
  assert.equal(app.get('board-system').classList.contains('warn'), true);
  assert.match(app.get('board-system').title, /过热降频到 408 MHz（满频 2208 MHz）/);
});


test('first word below server time invalidates the whole turn and preserves raw evidence', async () => {
  const app = await launch();
  const samples=[[322,512,4012],[1881,496,4933],[30,616,3382],[20,952,3430],[846,808,3525],[1482,643,2298],[1362,559,2152]];
  samples.forEach(([e2e_ms,server_first_ms,intent_ms],i)=>app.signal({type:'metric',source:'board',turn_id:'t'+i,metrics:{e2e_ms,server_first_ms,intent_ms}}));
  const stats=app.get('perf-summary').children.map(c=>c.textContent);
  assert.match(stats[0],/^首词 中位1422 msP90 1761\.3ms4 轮$/);
  assert.match(stats[1],/^服务端 中位601 ms.*4 轮$/);
  assert.match(stats[2],/^意图 中位2911\.5 ms.*4 轮$/);
  const rows=app.get('perf-rows').children;
  const invalid=rows.filter(r=>r.className==='perf-invalid');
  assert.equal(invalid.length,3);
  assert.deepEqual(invalid.map(r=>r.children[1].textContent),['20ms','30ms','322ms']);
  assert.match(invalid[0].children[0].textContent,/无效/);
  assert.match(invalid[0].title,/首词 20ms < 服务端 952ms.*整轮作废/);
});

test('validation waits for both timings, handles either order, and accepts equality', async () => {
  const app = await launch();
  const metric=(turn_id,metrics)=>app.signal({type:'metric',source:'board',turn_id,metrics});
  const stats=()=>app.get('perf-summary').children.map(c=>c.textContent);
  metric('bad',{e2e_ms:20,intent_ms:3000,doa_ms:1});
  assert.ok(stats().every(s=>s.endsWith('0 轮')));
  assert.equal(app.get('perf-rows').children[0].className,'perf-pending');
  metric('bad',{server_first_ms:952});
  assert.ok(stats().every(s=>s.endsWith('0 轮')));
  assert.equal(app.get('perf-rows').children[0].className,'perf-invalid');
  metric('good',{server_first_ms:616,intent_ms:3382,doa_ms:0.5});
  assert.ok(stats().every(s=>s.endsWith('0 轮')));
  metric('good',{e2e_ms:616});
  assert.ok(stats().every(s=>s.endsWith('1 轮')));
  assert.match(stats()[0],/^首词 中位616 ms/);
  assert.match(stats()[3],/^DOA 中位0\.5 ms/);
  metric('bad',{intent_ms:9000});
  assert.ok(stats().every(s=>s.endsWith('1 轮')));
});
