import * as THREE from 'three';

const DEG = Math.PI / 180;
const FLOOR_Y = -120.25, FLOOR_Z = 44.9;
const AXIS_KEYS = ['head_yaw', 'head_pitch_down', 'head_roll', 'arm_l_out', 'arm_l_forward', 'arm_r_out', 'arm_r_forward'];

/** STP geometry preview. Kinematics in rig.json are explicitly uncalibrated. */
export class RobotView {
  constructor(container, { onReady } = {}) {
    this.container = container;
    this.onReady = onReady;
    this.scene = new THREE.Scene();
    // Transparent clear: the page draws a plain backdrop behind the canvas.
    this.scene.add(new THREE.HemisphereLight(0xfafcff, 0x8b97aa, 0.65));
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 0.85;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.domElement.setAttribute('aria-label', '按 STP 还原的机器人三维模型；拖动旋转视角，滚轮缩放');
    this.renderer.domElement.style.cssText = 'display:block;width:100%;height:100%;touch-action:none;cursor:grab';
    container.appendChild(this.renderer.domElement);
    // Near plane well away from 1 mm: perspective depth precision falls with distance², and the
    // face overlay sits only overlay_offset_mm in front of the screen mesh (~800 mm from camera).
    this.camera = new THREE.PerspectiveCamera(30, 1, 40, 6000);
    this.radius = 620;
    // Model spans y −120…80 mm and x ±66 mm; aim at its middle.
    this.target = new THREE.Vector3(0, -20, 34.4);
    this.axisNodes = new Map();
    this.meshes = [];
    this.materials = [];
    this.pose = Object.fromEntries(AXIS_KEYS.map(key => [key, 0]));
    this.baseHeading = 0;
    this.zoom = 1;
    this.ready = false;
    this.disposed = false;
    this._listeners = [];
    this._makeLighting();
    this._makeGround();
    this._makeFaceCanvas();
    this._bindOrbit();
    this.resetCamera('front');
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
  }

  _makeLighting() {
    for (const [position, power] of [ [[-90, 300, -90], 2.0], [[180, 80, -220], 1.3], [[0, 130, 220], 1.0] ]) {
      const light = new THREE.DirectionalLight(0xffffff, power);
      light.position.set(...position);
      if (position[1] === 300) {
        light.castShadow = true;
        light.shadow.mapSize.set(2048, 2048);
        Object.assign(light.shadow.camera, { left: -200, right: 200, top: 200, bottom: -200, near: 1, far: 700 });
        light.shadow.normalBias = 0.5;
        light.shadow.bias = -0.00015;
        light.shadow.radius = 4;
      }
      this.scene.add(light);
    }
    const canvas = document.createElement('canvas');
    canvas.width = 1024; canvas.height = 512;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 512);
    gradient.addColorStop(0, '#f9fbff'); gradient.addColorStop(0.5, '#b7c1d1'); gradient.addColorStop(1, '#555f70');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, 1024, 512);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(80, 80, 100, 280); ctx.fillRect(680, 40, 180, 180);
    this.environmentTexture = new THREE.CanvasTexture(canvas);
    this.environmentTexture.mapping = THREE.EquirectangularReflectionMapping;
    this.environmentTexture.colorSpace = THREE.SRGBColorSpace;
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.environmentTarget = pmrem.fromEquirectangular(this.environmentTexture);
    this.scene.environment = this.environmentTarget.texture;
    this.scene.environmentIntensity = 0.65;
    pmrem.dispose();
  }

  _makeGround() {
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(2400, 2400), new THREE.ShadowMaterial({ opacity: 0.35 }));
    this.ground.rotation.x = -Math.PI / 2;
    this.ground.position.set(0, FLOOR_Y, FLOOR_Z);
    this.ground.receiveShadow = true;
    this.scene.add(this.ground);
  }

  _makeFaceCanvas() {
    this.faceCanvas = document.createElement('canvas');
    this.faceCanvas.width = 1024; this.faceCanvas.height = 712;
    this.faceContext = this.faceCanvas.getContext('2d', { alpha: false });
    this.faceContext.fillStyle = '#000000';
    this.faceContext.fillRect(0, 0, 1024, 712);
    this.faceTexture = new THREE.CanvasTexture(this.faceCanvas);
    this.faceTexture.colorSpace = THREE.SRGBColorSpace;
    this.faceTexture.minFilter = THREE.LinearFilter;
    this.faceTexture.magFilter = THREE.LinearFilter;
    this.faceTexture.generateMipmaps = false;
  }

  async load(meshURL = '/assets/robot-mesh.json', rigURL = '/config/rig.json') {
    const getJSON = async url => {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`模型资源加载失败 (${response.status}): ${url}`);
      return response.json();
    };
    const [data, rig] = await Promise.all([getJSON(meshURL), getJSON(rigURL)]);
    if (this.disposed) return this;
    this.rig = rig;
    this.robot = new THREE.Group();
    this.robot.name = 'STP original assembly';
    this.scene.add(this.robot);
    this.baseNode = new THREE.Group();
    this.baseNode.name = 'DOA upper assembly (uncalibrated)';
    this.baseNode.position.fromArray(rig.base.origin);
    this.robot.add(this.baseNode);
    const branchOwners = new Map();
    for (const branchName of ['head', 'left_arm', 'right_arm']) {
      const config = rig[branchName];
      let parent = this.baseNode;
      config.order.forEach((key, index) => {
        const node = new THREE.Group(); node.name = key;
        if (index === 0) node.position.fromArray(config.origin).sub(new THREE.Vector3(...rig.base.origin));
        node.userData.axis = new THREE.Vector3(...config.axes[key]).normalize();
        parent.add(node); this.axisNodes.set(key, node); parent = node;
      });
      for (const index of config.meshes) branchOwners.set(index, { parent, origin: config.origin });
    }
    const materialByName = {};
    for (const key of ['blue', 'white', 'silver', 'black']) {
      materialByName[key] = new THREE.MeshStandardMaterial({ ...rig.materials[key], side: THREE.DoubleSide });
      this.materials.push(materialByName[key]);
    }
    data.meshes.forEach((meshData, index) => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(meshData.attributes.position.array, 3));
      geometry.setAttribute('normal', new THREE.Float32BufferAttribute(meshData.attributes.normal.array, 3));
      geometry.setIndex(meshData.index.array);
      geometry.computeBoundingSphere();
      let material = materialByName.white;
      for (const key of ['blue', 'silver', 'black']) if (rig.materials[`${key}_meshes`].includes(index)) material = materialByName[key];
      const mesh = new THREE.Mesh(geometry, material);
      mesh.name = `${index}: ${meshData.name || 'STP part'}`;
      mesh.castShadow = true; mesh.receiveShadow = true;
      const branch = branchOwners.get(index);
      if (branch) {
        mesh.position.fromArray(branch.origin).multiplyScalar(-1);
        branch.parent.add(mesh);
      } else if (rig.base.fixed_meshes.includes(index)) {
        this.robot.add(mesh);
      } else {
        mesh.position.fromArray(rig.base.origin).multiplyScalar(-1);
        this.baseNode.add(mesh);
      }
      this.meshes[index] = mesh;
      if (index === rig.screen.mesh) this._attachFace(mesh, meshData, rig.screen);
    });
    this.ready = true;
    this.setPose(this.pose); this.setBaseHeading(this.baseHeading);
    this.resize(); this.render();
    this.onReady?.(this);
    return this;
  }

  _attachFace(screenMesh, meshData, config) {
    const p = meshData.attributes.position.array;
    const normals = meshData.attributes.normal.array;
    const indices = meshData.index.array;
    const positions = [], uv = [];
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < p.length; i += 3) {
      minX = Math.min(minX, p[i]); maxX = Math.max(maxX, p[i]);
      minY = Math.min(minY, p[i + 1]); maxY = Math.max(maxY, p[i + 1]);
    }
    for (let i = 0; i < indices.length; i += 3) {
      const triangle = indices.slice(i, i + 3);
      if (!triangle.every(index => normals[index * 3 + 2] < -0.99 && Math.abs(p[index * 3 + 2] - config.front_z) < 0.1)) continue;
      for (const index of triangle) {
        const offset = index * 3;
        positions.push(p[offset], p[offset + 1], p[offset + 2] - config.overlay_offset_mm);
        // Facing −Z: screen-right is model −X. Video must not be mirrored.
        uv.push((maxX - p[offset]) / (maxX - minX), (p[offset + 1] - minY) / (maxY - minY));
      }
    }
    if (!positions.length) throw new Error('STP 前屏表面没有找到，停止贴图以避免错误映射');
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    // Polygon offset keeps the video in front of the coplanar black cover at any camera distance.
    const material = new THREE.MeshBasicMaterial({ map: this.faceTexture, side: THREE.DoubleSide, toneMapped: false,
      polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 });
    this.materials.push(material);
    this.faceMesh = new THREE.Mesh(geometry, material);
    this.faceMesh.name = 'Face video on STP front cover';
    screenMesh.add(this.faceMesh);
    this.faceAspect = (maxX - minX) / (maxY - minY);
    this.faceCanvas.height = Math.round(this.faceCanvas.width / this.faceAspect);
  }

  setPose(pose) {
    for (const key of AXIS_KEYS) {
      if (Number.isFinite(pose[key])) this.pose[key] = pose[key];
      const node = this.axisNodes.get(key);
      if (node) node.quaternion.setFromAxisAngle(node.userData.axis, this.pose[key] * DEG);
    }
  }

  setBaseHeading(degrees) {
    if (!Number.isFinite(degrees)) return;
    this.baseHeading = degrees;
    if (this.baseNode) this.baseNode.quaternion.setFromAxisAngle(new THREE.Vector3(...this.rig.base.axis).normalize(), degrees * DEG);
  }

  setFace(videoElement) {
    this.video = videoElement;
    this.lastVideoTime = -1;
    this.lastVideoSource = '';
    this.faceContext.fillStyle = '#000000';
    this.faceContext.fillRect(0, 0, this.faceCanvas.width, this.faceCanvas.height);
    this.faceTexture.needsUpdate = true;
  }

  _updateFace() {
    const video = this.video;
    if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return;
    if (video.currentTime === this.lastVideoTime && video.currentSrc === this.lastVideoSource) return;
    this.lastVideoTime = video.currentTime; this.lastVideoSource = video.currentSrc;
    const width = this.faceCanvas.width, height = this.faceCanvas.height;
    const scale = Math.min(width / video.videoWidth, height / video.videoHeight);
    const dw = video.videoWidth * scale, dh = video.videoHeight * scale;
    this.faceContext.fillStyle = '#000000'; this.faceContext.fillRect(0, 0, width, height);
    this.faceContext.drawImage(video, (width - dw) / 2, (height - dh) / 2, dw, dh);
    this.faceTexture.needsUpdate = true;
  }

  resetCamera(view = 'front') {
    this.azimuth = view === 'back' ? Math.PI - 0.45 : view === 'quarter' ? 0.36 : 0;
    this.elevation = view === 'front' ? 0.12 : 0.26;
    this.zoom = 1;
    this._updateCamera();
  }

  _updateCamera() {
    const radius = this.radius;
    this.camera.position.set(this.target.x + Math.sin(this.azimuth) * Math.cos(this.elevation) * radius,
      this.target.y + Math.sin(this.elevation) * radius, this.target.z - Math.cos(this.azimuth) * Math.cos(this.elevation) * radius);
    this.camera.lookAt(this.target);
    this.camera.zoom = this.zoom;
    this.camera.updateProjectionMatrix();
  }

  _bindOrbit() {
    const canvas = this.renderer.domElement;
    let drag = null;
    const on = (name, callback, options) => {
      canvas.addEventListener(name, callback, options);
      this._listeners.push([name, callback, options]);
    };
    on('pointerdown', event => {
      if (event.button !== 0) return;
      drag = { x: event.clientX, y: event.clientY };
      canvas.setPointerCapture(event.pointerId); canvas.style.cursor = 'grabbing';
    });
    on('pointermove', event => {
      if (!drag) return;
      this.azimuth -= (event.clientX - drag.x) * 0.007;
      this.elevation = THREE.MathUtils.clamp(this.elevation + (event.clientY - drag.y) * 0.005, -0.25, 1.15);
      drag = { x: event.clientX, y: event.clientY }; this._updateCamera();
    });
    const endDrag = () => { drag = null; canvas.style.cursor = 'grab'; };
    on('pointerup', endDrag); on('pointercancel', endDrag); on('lostpointercapture', endDrag);
    on('wheel', event => {
      event.preventDefault();
      this.zoom = THREE.MathUtils.clamp(this.zoom * Math.exp(-event.deltaY * 0.001), 0.6, 1.65);
      this._updateCamera();
    }, { passive: false });
  }

  resize() {
    if (this.disposed) return;
    const width = Math.max(this.container.clientWidth, 1), height = Math.max(this.container.clientHeight, 1);
    this.renderer.setSize(width, height, false);
    const aspect = width / height;
    // Frame ~256 mm tall and ~190 mm wide: the 200 mm robot fills most of the stage.
    const t = Math.tan(this.camera.fov * DEG / 2);
    this.radius = Math.min(860, Math.max(128 / t, 95 / (t * aspect)));
    this.camera.aspect = aspect;
    this._updateCamera();
  }

  render() {
    if (this.disposed) return;
    this._updateFace();
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this.disposed = true;
    this.resizeObserver.disconnect();
    for (const [name, callback, options] of this._listeners) this.renderer.domElement.removeEventListener(name, callback, options);
    this.scene.traverse(object => { object.geometry?.dispose(); });
    for (const material of this.materials) material.dispose();
    this.ground.material.dispose();
    this.faceTexture.dispose(); this.environmentTexture.dispose(); this.environmentTarget.dispose();
    this.renderer.dispose(); this.renderer.domElement.remove();
  }
}
