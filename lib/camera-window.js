/**
 * The board camera window: a floating panel that opens the camera on the board
 * (the ISP's self path, beside visiond) and long-polls /api/camera/frame for raw NV12 frames.
 * Closing the window, or the page, closes the reader; the server also lets go of it on its own
 * once nobody has asked for a frame for a while.
 */
import { nv12ToRgba } from './nv12.js';
import { rotation } from './rotate.js';

const $ = id => document.getElementById(id);
const ROTATION_KEY = 'robotlab.camera.rotation';
const POINTS_KEY = 'robotlab.camera.points';
// visiond looks about twice a second; points older than this are not drawn over a newer picture.
const FACES_STALE_MS = 1500;

function savedPoints() {
  try { return localStorage.getItem(POINTS_KEY) !== '0'; } catch { return true; }
}

// Faces are fractions of visiond's frame, drawn in the source's pixels under the same turn as the picture.
function drawFaces(ctx, faces, width, height) {
  for (const face of faces) {
    if (!face.bbox) continue;
    const [x, y, w, h] = face.bbox;
    ctx.lineWidth = 2;
    ctx.strokeStyle = face.talking ? '#ffb020' : '#35e07a';
    ctx.strokeRect(x * width, y * height, w * width, h * height);
    ctx.fillStyle = '#4fe3ff';
    for (const [px, py] of face.landmarks || []) ctx.fillRect(px * width - 1, py * height - 1, 2, 2);
  }
}

function savedRotation() {
  try { const deg = Number(localStorage.getItem(ROTATION_KEY)); return [90, 180, 270].includes(deg) ? deg : 0; } catch { return 0; }
}

export function setupCameraWindow({fetchImpl = (...args) => fetch(...args)} = {}) {
  const win = $('camera-window'), canvas = $('camera-canvas'), toggle = $('camera-toggle');
  if (!win || !canvas || !toggle) return null;
  const ctx = canvas.getContext('2d');
  const source = document.createElement('canvas'), sourceCtx = source.getContext('2d');
  let open = false, seq = 0, loop = 0, image = null, hasFrame = false, turn = savedRotation();
  let showPoints = savedPoints(), faces = [], facesAt = 0;
  let shown = 0, since = performance.now();

  const status = (text, kind = '') => {
    $('camera-status').textContent = text;
    $('camera-status').title = text;
    $('camera-status').className = 'camera-status ' + kind;
  };
  const post = body => fetchImpl('/api/camera', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify(body), keepalive: true});

  function clearFrame() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    image = null; hasFrame = false;
    $('camera-empty').hidden = false;
  }

  function draw(bytes, width, height) {
    if (source.width !== width || source.height !== height || !image) {
      source.width = width; source.height = height; image = sourceCtx.createImageData(width, height);
    }
    nv12ToRgba(bytes, width, height, image.data);
    sourceCtx.putImageData(image, 0, 0);
    hasFrame = true;
    paint();
    $('camera-empty').hidden = true;
  }

  function displayed(width, height) {
    shown++;
    const now = performance.now();
    if (now - since >= 2000) {
      status(`${width}×${height} · ${(shown * 1000 / (now - since)).toFixed(1)} fps`, 'live');
      shown = 0; since = now;
    } else if (!$('camera-status').classList.contains('live')) status(`${width}×${height}`, 'live');
  }

  // Turned here, in the page: the board sends the same frames at any angle.
  function paint() {
    const {width, height, matrix} = rotation(turn, source.width, source.height);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width; canvas.height = height; canvas.style.aspectRatio = width + '/' + height;
    }
    ctx.setTransform(...matrix);
    ctx.drawImage(source, 0, 0);
    if (showPoints && performance.now() - facesAt < FACES_STALE_MS) drawFaces(ctx, faces, source.width, source.height);
  }

  function showTurn() {
    const button = $('camera-rotate');
    button.textContent = '旋转 ' + turn + '°';
    button.setAttribute('aria-label', '顺时针旋转画面 90 度，当前 ' + turn + ' 度');
  }

  async function poll(run) {
    shown = 0; since = performance.now();
    while (open && run === loop) {
      let resp;
      try {
        resp = await fetchImpl('/api/camera/frame?after=' + seq, {cache: 'no-store'});
      } catch {
        if (run !== loop || !open) return;
        clearFrame(); status('本地服务不可用', 'error'); await new Promise(r => setTimeout(r, 2000)); continue;
      }
      if (run !== loop) return;
      if (resp.status === 200) {
        seq = Number(resp.headers.get('X-Frame-Seq')) || seq + 1;
        const width = Number(resp.headers.get('X-Frame-Width')), height = Number(resp.headers.get('X-Frame-Height'));
        try {
          const bytes = new Uint8Array(await resp.arrayBuffer());
          if (run !== loop || !open) return;
          draw(bytes, width, height);
        } catch (err) {
          if (run !== loop || !open) return;
          clearFrame(); status('画面解码失败：' + err.message, 'error');
          $('camera-retry').hidden = false;
          post({cmd: 'stop'}).catch(() => {});
          return;
        }
        displayed(width, height);
        continue;
      }
      // No new frame in the poll window: say why, and stop asking if the camera is gone.
      const st = await fetchImpl('/api/camera/status', {cache: 'no-store'}).then(r => r.json()).catch(() => null);
      if (run !== loop) return;
      if (!st || st.state === 'off' || st.state === 'error') {
        clearFrame();
        status(st?.message || '画面已断开', 'error');
        $('camera-retry').hidden = false;
        return;
      }
      status(st.message || '等待画面…');
    }
  }

  async function start() {
    open = true; seq = 0; const run = ++loop;
    win.hidden = false; toggle.classList.add('active');
    clearFrame();
    $('camera-empty').hidden = false; $('camera-retry').hidden = true;
    status('正在打开摄像头…');
    try {
      const resp = await post({cmd: 'start', scale: 1, fps: 4});
      const data = await resp.json();
      // A slow start may finish after the user closed the window and its first stop was served.
      if (run !== loop || !open) {
        if (!open) post({cmd: 'stop'}).catch(() => {});
        return;
      }
      if (!data.ok) { status(data.error || data.message || '打不开摄像头', 'error'); $('camera-retry').hidden = false; return; }
      status(data.message || '等待画面…');
    } catch (err) {
      if (run !== loop || !open) return;
      status('打不开摄像头：' + err.message, 'error'); $('camera-retry').hidden = false; return;
    }
    poll(run);
  }

  function stop() {
    if (!open) return;
    open = false; loop++;
    clearFrame();
    win.hidden = true; toggle.classList.remove('active');
    post({cmd: 'stop'}).catch(() => {});
  }

  toggle.onclick = () => (open ? stop() : start());
  $('camera-rotate').onclick = () => {
    turn = (turn + 90) % 360;
    try { localStorage.setItem(ROTATION_KEY, String(turn)); } catch {}
    showTurn();
    if (hasFrame) paint();
  };
  showTurn();
  const showPointsLabel = () => { $('camera-points').textContent = '点位 ' + (showPoints ? '开' : '关'); };
  $('camera-points').onclick = () => {
    showPoints = !showPoints;
    try { localStorage.setItem(POINTS_KEY, showPoints ? '1' : '0'); } catch {}
    showPointsLabel();
    if (hasFrame) paint();
  };
  showPointsLabel();
  window.addEventListener('board-faces', event => { faces = event.detail || []; facesAt = performance.now(); });
  $('camera-close').onclick = stop;
  $('camera-retry').onclick = start;
  window.addEventListener('pagehide', stop);

  // Drag by the title bar; the panel's own corner resizes it (CSS resize).
  const bar = $('camera-titlebar');
  bar.addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;
    const rect = win.getBoundingClientRect(), dx = e.clientX - rect.left, dy = e.clientY - rect.top;
    bar.setPointerCapture(e.pointerId);
    const move = ev => {
      const x = Math.min(Math.max(0, ev.clientX - dx), window.innerWidth - 80);
      const y = Math.min(Math.max(0, ev.clientY - dy), window.innerHeight - 40);
      Object.assign(win.style, {left: x + 'px', top: y + 'px', right: 'auto', bottom: 'auto'});
    };
    const up = () => { bar.removeEventListener('pointermove', move); bar.removeEventListener('pointerup', up); };
    bar.addEventListener('pointermove', move); bar.addEventListener('pointerup', up);
  });
  return {start, stop, isOpen: () => open};
}

setupCameraWindow();
