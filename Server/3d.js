/* =========================================================
   ALIJON MAHMADJONOV — 3D story scene
   A particle cloud that morphs through four shapes as the
   story scrolls: point -> sphere -> torus knot -> helix -> galaxy.
   Palette mirrors styles.css (gold #E8B24C on warm black).
   ========================================================= */

import * as THREE from './assets/three.module.min.js';

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const stage = document.querySelector('.stage');
const canvas = document.getElementById('gl');

/* ---------- panel reveal + chapter rail (works with or without WebGL) ---------- */
initPanels();

/* ---------- bail out cleanly if WebGL is unavailable ---------- */
if (!hasWebGL()) {
  stage.classList.add('no-webgl');
} else {
  initScene();
}

function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
  } catch (e) {
    return false;
  }
}

/* =========================================================
   Panels & rail
   ========================================================= */
function initPanels() {
  const panels = document.querySelectorAll('.panel');
  const railLinks = document.querySelectorAll('.rail a');

  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) entry.target.classList.add('is-visible');

      const ch = entry.target.dataset.chapter;
      if (ch && entry.intersectionRatio > 0.5) {
        railLinks.forEach((a) => a.classList.toggle('is-active', a.dataset.rail === ch));
      }
    });
  }, { threshold: [0.25, 0.55] });

  panels.forEach((p) => io.observe(p));
}

/* =========================================================
   Scene
   ========================================================= */
function initScene() {
  canvas.style.opacity = '0';
  const COUNT = window.innerWidth < 768 ? 4200 : 9000;
  const RADIUS = 2.3;

  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: window.devicePixelRatio < 2,
    alpha: true,
    powerPreference: 'high-performance'
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setClearColor(0x0b0a08, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0b0a08, 0.055);

  const camera = new THREE.PerspectiveCamera(52, window.innerWidth / window.innerHeight, 0.1, 100);
  camera.position.set(0, 0, 9);

  const world = new THREE.Group();
  scene.add(world);

  /* ---------- shape targets ---------- */
  const shapes = [
    sphereShape(COUNT, RADIUS),
    knotShape(COUNT, RADIUS),
    helixShape(COUNT, RADIUS),
    galaxyShape(COUNT, RADIUS)
  ];

  /* ---------- main particle cloud ---------- */
  const geometry = new THREE.BufferGeometry();
  const positions = new Float32Array(shapes[0]);
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('color', new THREE.BufferAttribute(particleColors(COUNT), 3));

  const sprite = softSprite();
  const material = new THREE.PointsMaterial({
    size: window.innerWidth < 768 ? 0.055 : 0.045,
    map: sprite,
    vertexColors: true,
    transparent: true,
    opacity: 0.95,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    sizeAttenuation: true
  });

  const points = new THREE.Points(geometry, material);
  world.add(points);

  /* ---------- ambient dust for depth ---------- */
  const dustCount = Math.round(COUNT * 0.18);
  const dustGeo = new THREE.BufferGeometry();
  const dustPos = new Float32Array(dustCount * 3);
  const rndDust = mulberry32(99);
  for (let i = 0; i < dustCount; i++) {
    dustPos[i * 3] = (rndDust() - 0.5) * 26;
    dustPos[i * 3 + 1] = (rndDust() - 0.5) * 16;
    dustPos[i * 3 + 2] = (rndDust() - 0.5) * 20 - 4;
  }
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPos, 3));
  const dust = new THREE.Points(dustGeo, new THREE.PointsMaterial({
    size: 0.035,
    map: sprite,
    color: 0x8a7f6e,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  }));
  scene.add(dust);

  /* ---------- core glow ---------- */
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({
    map: softSprite(),
    color: 0xE8B24C,
    transparent: true,
    opacity: 0.5,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  }));
  glow.scale.set(7, 7, 1);
  world.add(glow);

  /* ---------- scroll mapping: chapter centers -> shape index ---------- */
  let centers = [];
  function measure() {
    centers = [...document.querySelectorAll('[data-chapter]')].map((el) => {
      const r = el.getBoundingClientRect();
      return r.top + window.scrollY + r.height / 2 - window.innerHeight / 2;
    });
  }

  function storyProgress() {
    if (centers.length < 2) return 0;
    const y = window.scrollY;
    if (y <= centers[0]) return 0;
    if (y >= centers[centers.length - 1]) return centers.length - 1;
    for (let i = 0; i < centers.length - 1; i++) {
      if (y >= centers[i] && y < centers[i + 1]) {
        return i + (y - centers[i]) / (centers[i + 1] - centers[i]);
      }
    }
    return 0;
  }

  /* ---------- pointer parallax ---------- */
  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  window.addEventListener('pointermove', (e) => {
    pointer.tx = (e.clientX / window.innerWidth - 0.5) * 2;
    pointer.ty = (e.clientY / window.innerHeight - 0.5) * 2;
  }, { passive: true });

  /* ---------- resize ---------- */
  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    measure();
  }
  window.addEventListener('resize', debounce(() => {
    resize();
    if (REDUCED) renderer.render(scene, camera);
  }, 150));
  resize();

  /* ---------- reduced motion: one static frame, no autonomous animation ---------- */
  if (REDUCED) {
    renderer.render(scene, camera);
    revealCanvas();
    return;
  }

  /* ---------- loop ---------- */
  const clock = new THREE.Clock();
  let lastMorph = -1;
  let running = true;

  document.addEventListener('visibilitychange', () => {
    running = !document.hidden;
    if (running) { clock.getDelta(); tick(); }
  });

  function tick() {
    if (!running) return;
    requestAnimationFrame(tick);

    const dt = Math.min(clock.getDelta(), 0.05);
    const elapsed = clock.getElapsedTime();
    const p = storyProgress();

    /* morph positions only when the scroll actually moved */
    if (Math.abs(p - lastMorph) > 0.0005) {
      const i = Math.min(Math.floor(p), shapes.length - 2);
      const f = plateau(p - i);
      const a = shapes[i], b = shapes[i + 1];
      for (let j = 0; j < positions.length; j++) {
        positions[j] = a[j] + (b[j] - a[j]) * f;
      }
      geometry.attributes.position.needsUpdate = true;
      lastMorph = p;
    }

    /* breathing + idle rotation */
    world.rotation.y += dt * 0.09;
    world.rotation.x = Math.sin(elapsed * 0.25) * 0.09;
    points.material.size = (window.innerWidth < 768 ? 0.055 : 0.045) * (1 + Math.sin(elapsed * 1.1) * 0.06);
    glow.material.opacity = 0.42 + Math.sin(elapsed * 0.9) * 0.08;

    dust.rotation.y = elapsed * 0.012;

    /* camera: pulls in as the story advances, plus damped pointer parallax */
    pointer.x += (pointer.tx - pointer.x) * 0.045;
    pointer.y += (pointer.ty - pointer.y) * 0.045;

    const t = p / (shapes.length - 1);
    camera.position.x = pointer.x * 1.15;
    camera.position.y = -pointer.y * 0.75 + Math.sin(elapsed * 0.4) * 0.12;
    camera.position.z = 9 - t * 3.1;
    camera.lookAt(0, 0, 0);

    renderer.render(scene, camera);
  }

  tick();
  revealCanvas();
}

/* fade the canvas in only once a real frame has been drawn */
function revealCanvas() {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    canvas.style.transition = 'opacity 1.2s ease';
    canvas.style.opacity = '1';
  }));
}

/* =========================================================
   Shape generators — every shape returns COUNT * 3 floats
   ========================================================= */

/* 01 — Начало: an even sphere shell (Fibonacci distribution) */
function sphereShape(n, R) {
  const p = new Float32Array(n * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const th = golden * i;
    p[i * 3] = Math.cos(th) * r * R;
    p[i * 3 + 1] = y * R;
    p[i * 3 + 2] = Math.sin(th) * r * R;
  }
  return p;
}

/* 02 — Основа: a (2,3) torus knot — structure emerging from chaos */
function knotShape(n, R) {
  const p = new Float32Array(n * 3);
  const rnd = mulberry32(7);
  const P = 2, Q = 3, tube = 0.09 * R, scale = R * 0.46;
  for (let i = 0; i < n; i++) {
    const t = (i / n) * Math.PI * 2;
    const r = Math.cos(Q * t) + 2;
    const cx = r * Math.cos(P * t) * scale;
    const cy = r * Math.sin(P * t) * scale;
    const cz = -Math.sin(Q * t) * scale * 1.6;
    /* scatter inside the tube so the curve reads as volume */
    const a = rnd() * Math.PI * 2;
    const rr = Math.sqrt(rnd()) * tube;
    p[i * 3] = cx + Math.cos(a) * rr;
    p[i * 3 + 1] = cy + Math.sin(a) * rr;
    p[i * 3 + 2] = cz + (rnd() - 0.5) * tube * 1.4;
  }
  return p;
}

/* 03 — Практика: a double helix — growth by repetition, one level up */
function helixShape(n, R) {
  const p = new Float32Array(n * 3);
  const rnd = mulberry32(21);
  const turns = 3.4, height = R * 2.9, radius = R * 0.72;
  for (let i = 0; i < n; i++) {
    const u = i / n;
    const strand = i % 2;
    /* every 9th particle becomes a rung between the strands */
    if (i % 9 === 0) {
      const t = u * turns * Math.PI * 2;
      const k = rnd() * 2 - 1;
      p[i * 3] = Math.cos(t) * radius * k;
      p[i * 3 + 1] = (u - 0.5) * height;
      p[i * 3 + 2] = Math.sin(t) * radius * k;
      continue;
    }
    const t = u * turns * Math.PI * 2 + strand * Math.PI;
    const jitter = (rnd() - 0.5) * 0.09 * R;
    p[i * 3] = Math.cos(t) * radius + jitter;
    p[i * 3 + 1] = (u - 0.5) * height + jitter;
    p[i * 3 + 2] = Math.sin(t) * radius + jitter;
  }
  return p;
}

/* 04 — Связь: a spiral galaxy — code reaching outward without borders */
function galaxyShape(n, R) {
  const p = new Float32Array(n * 3);
  const rnd = mulberry32(1337);
  const arms = 4, maxR = R * 1.25, spin = 2.2;
  /* the disc is tilted in the geometry itself, otherwise the scene's
     near-level camera would show it edge-on as a flat streak */
  const tilt = 0.55, cosT = Math.cos(tilt), sinT = Math.sin(tilt);
  for (let i = 0; i < n; i++) {
    const r = Math.pow(rnd(), 0.85) * maxR;
    const arm = (i % arms) / arms * Math.PI * 2;
    const angle = arm + r * spin;
    /* scatter tightens toward the core */
    const spread = (1 - r / maxR) * 0.22 + 0.10;
    const x = Math.cos(angle) * r + (rnd() - 0.5) * spread;
    const y = (rnd() - 0.5) * spread * 1.4;
    const z = Math.sin(angle) * r + (rnd() - 0.5) * spread;
    p[i * 3] = x;
    p[i * 3 + 1] = y * cosT - z * sinT;
    p[i * 3 + 2] = y * sinT + z * cosT;
  }
  return p;
}

/* =========================================================
   Helpers
   ========================================================= */

/* gold -> warm white, weighted toward gold */
function particleColors(n) {
  const c = new Float32Array(n * 3);
  const rnd = mulberry32(3);
  const gold = new THREE.Color(0xE8B24C);
  const ink = new THREE.Color(0xF6F2EA);
  const muted = new THREE.Color(0x948D80);
  const tmp = new THREE.Color();
  for (let i = 0; i < n; i++) {
    const r = rnd();
    if (r < 0.62) tmp.copy(gold).lerp(ink, rnd() * 0.35);
    else if (r < 0.9) tmp.copy(ink);
    else tmp.copy(muted);
    c[i * 3] = tmp.r; c[i * 3 + 1] = tmp.g; c[i * 3 + 2] = tmp.b;
  }
  return c;
}

/* soft round particle sprite, generated so the page needs no image assets */
function softSprite() {
  const size = 64;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,.65)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/* hold each shape for the middle 50% of its segment, morph across the rest */
function plateau(f) {
  const x = Math.min(Math.max((f - 0.25) / 0.5, 0), 1);
  return x * x * (3 - 2 * x);
}

/* deterministic PRNG so shapes are identical on every load */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function debounce(fn, ms) {
  let id;
  return function () { clearTimeout(id); id = setTimeout(fn, ms); };
}
