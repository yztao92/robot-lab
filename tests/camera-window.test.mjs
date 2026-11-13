import test from 'node:test';
import assert from 'node:assert/strict';

// The module also auto-mounts in the page. Import with no matching elements, then mount explicitly.
globalThis.document = {getElementById: () => null};
const {setupCameraWindow} = await import('../lib/camera-window.js');

function fixture() {
  const calls = {paint: [], clear: 0};
  function element() {
    const node = {hidden: false, textContent: '', className: '', style: {}, width: 0, height: 0,
      addEventListener() {}, setAttribute() {}};
    node.classList = {
      add(name) { node.className += ' ' + name; },
      remove(name) { node.className = node.className.replace(name, ''); },
      contains(name) { return node.className.split(' ').includes(name); },
    };
    node.getContext = () => ({
      setTransform() {}, clearRect() { calls.clear++; },
      createImageData(w, h) { return {data: new Uint8ClampedArray(w * h * 4)}; },
      putImageData() {}, drawImage(source) { calls.paint.push([source.width, source.height]); },
      strokeRect() {}, fillRect() {},
    });
    return node;
  }
  const nodes = new Map();
  globalThis.document = {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  }, createElement: element};
  const listeners = new Map();
  globalThis.window = {addEventListener(name, handler) { listeners.set(name, handler); }};
  globalThis.localStorage = {getItem: () => null, setItem() {}};
  return {nodes, calls, listeners};
}

test('it requests 4 fps, paints 640x360, and clears a disconnected frame', async () => {
  const {nodes, calls} = fixture(), requests = [];
  let frames = 0;
  const raw = new Uint8Array(640 * 360 * 3 / 2).fill(128);
  const fetchImpl = async (url, options) => {
    requests.push({url, options});
    if (url === '/api/camera') return {json: async () => ({ok: true})};
    if (url === '/api/camera/status') return {json: async () => ({state: 'error', message: '源已断开'})};
    if (++frames === 1) return {status: 200,
      headers: new Map([['X-Frame-Width', '640'], ['X-Frame-Height', '360'], ['X-Frame-Seq', '10']]),
      arrayBuffer: async () => raw.buffer};
    return {status: 204};
  };
  const camera = setupCameraWindow({fetchImpl});
  await camera.start();
  // The polling loop is deliberately asynchronous to the open action.
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(JSON.parse(requests[0].options.body), {cmd: 'start', scale: 1, fps: 4});
  assert.deepEqual(calls.paint, [[640, 360]]);
  assert.equal(nodes.get('camera-canvas').width, 640);
  assert.equal(nodes.get('camera-canvas').height, 360);
  assert.equal(nodes.get('camera-status').textContent, '源已断开');
  assert.equal(nodes.get('camera-empty').hidden, false);
  assert.equal(calls.clear, 2, 'clear on opening and again when the source disconnects');
  camera.stop();
});


test('opening then closing before start completes leaves no reader running', async () => {
  const {nodes} = fixture(), requests = [];
  let finishStart;
  const started = new Promise(resolve => { finishStart = resolve; });
  const fetchImpl = async (url, options) => {
    const cmd = JSON.parse(options.body).cmd;
    requests.push({url, cmd, keepalive: options.keepalive});
    if (cmd === 'start') await started;
    return {json: async () => ({ok: true})};
  };
  const camera = setupCameraWindow({fetchImpl});
  assert.equal(requests.length, 0, 'mounting the page does not start a reader');
  const pending = camera.start();
  nodes.get('camera-close').onclick();
  finishStart();
  await pending;
  assert.equal(camera.isOpen(), false);
  assert.equal(nodes.get('camera-window').hidden, true);
  assert.deepEqual(requests.map(r => r.cmd), ['start', 'stop', 'stop'], 'late start is stopped again');
  assert.ok(requests.every(r => r.keepalive), 'closing the page may finish its stop request');
});

test('pagehide stops the reader and ignores an in-flight frame response', async () => {
  const {calls, listeners} = fixture(), requests = [];
  let finishFrame;
  const frame = new Promise(resolve => { finishFrame = resolve; });
  const fetchImpl = async (url, options) => {
    requests.push({url, options});
    if (options?.method === 'POST') return {json: async () => ({ok: true})};
    return frame;
  };
  const camera = setupCameraWindow({fetchImpl});
  await camera.start();
  listeners.get('pagehide')();
  finishFrame({status: 200, headers: new Map(), arrayBuffer: async () => new ArrayBuffer(0)});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(camera.isOpen(), false);
  assert.equal(calls.paint.length, 0);
  assert.deepEqual(requests.filter(r => r.options?.body).map(r => JSON.parse(r.options.body).cmd), ['start', 'stop']);
  assert.equal(requests.filter(r => r.url.startsWith('/api/camera/frame')).length, 1, 'no next request after pagehide');
});
