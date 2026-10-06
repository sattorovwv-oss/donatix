'use client';

/**
 * Процедурный город будущего.
 *
 * Все здания — один InstancedMesh: тысяча отдельных мешей уронила бы FPS,
 * а так это один вызов отрисовки. Окна не текстура, а шейдер: он рисует
 * сетку окон прямо на грани куба и зажигает их псевдослучайно, поэтому
 * ни одной картинки грузить не нужно.
 */

import { useLayoutEffect, useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';

const CORRIDOR_HALF_WIDTH = 7; // ширина «улицы», по которой летит камера

const vertexShader = /* glsl */ `
  attribute float aSeed;
  varying vec2 vUv;
  varying float vSeed;
  varying vec3 vNormalW;
  varying float vFogDepth;

  void main() {
    vUv = uv;
    vSeed = aSeed;
    vNormalW = normalize(mat3(instanceMatrix) * normal);

    vec4 instancePos = instanceMatrix * vec4(position, 1.0);
    vec4 viewPos = modelViewMatrix * instancePos;

    // Туман считаем сами: three не подставляет свои fog-чанки в кастомный шейдер
    vFogDepth = -viewPos.z;

    gl_Position = projectionMatrix * viewPos;
  }
`;

const fragmentShader = /* glsl */ `
  precision highp float;

  uniform float uTime;
  uniform vec3  uWindow;
  uniform vec3  uAccent;
  uniform vec3  uFogColor;
  uniform float uFogDensity;

  varying vec2  vUv;
  varying float vSeed;
  varying vec3  vNormalW;
  varying float vFogDepth;

  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }

  void main() {
    // Крыша — без окон, иначе здание сверху выглядит как коробка в клетку
    float isRoof = step(0.7, abs(vNormalW.y));

    vec2 grid = vec2(5.0, 16.0);
    vec2 cell = floor(vUv * grid);
    vec2 f    = fract(vUv * grid);

    // Рамка вокруг каждого окна
    float win =
      step(0.18, f.x) * step(f.x, 0.82) *
      step(0.10, f.y) * step(f.y, 0.80);

    // Горит меньшая часть окон: в доме, где светятся все, глаз не верит,
    // и вдобавок сплошное свечение выжигает передний план
    float lit = step(0.62, hash(cell + vSeed * 37.0));

    // Медленное мерцание: у каждого окна своя фаза
    float phase   = hash(cell + vSeed * 11.0) * 6.28318;
    float flicker = 0.78 + 0.22 * sin(uTime * 1.4 + phase);

    vec3 base  = vec3(0.010, 0.014, 0.026);
    vec3 glow  = mix(uWindow, uAccent, hash(cell + vSeed * 5.0));
    vec3 color = base + glow * win * lit * flicker * (1.0 - isRoof) * 0.95;

    // Лёгкая подсветка граней, чтобы силуэты читались на фоне тумана
    float rim = pow(1.0 - abs(dot(normalize(vNormalW), vec3(0.0, 1.0, 0.0))), 3.0);
    color += uAccent * rim * 0.05;

    // Экспоненциальный туман: дальние кварталы растворяются в фоне,
    // и город кажется бесконечным, хотя зданий всего пара сотен
    float fogFactor = 1.0 - exp(-uFogDensity * uFogDensity * vFogDepth * vFogDepth);
    color = mix(color, uFogColor, clamp(fogFactor, 0.0, 1.0));

    gl_FragColor = vec4(color, 1.0);
  }
`;

export default function City({ count }: { count: number }) {
  const meshRef = useRef<THREE.InstancedMesh>(null);
  const materialRef = useRef<THREE.ShaderMaterial>(null);

  /** Раскладка города считается один раз и не меняется между кадрами. */
  const { seeds, matrices } = useMemo(() => {
    // Свой генератор вместо Math.random: одинаковый город при каждой загрузке
    let state = 1337;
    const rand = () => {
      state = (state * 1664525 + 1013904223) % 4294967296;
      return state / 4294967296;
    };

    const seeds = new Float32Array(count);
    const matrices: THREE.Matrix4[] = [];
    const dummy = new THREE.Object3D();

    let placed = 0;
    let guard = 0;
    while (placed < count && guard < count * 40) {
      guard++;

      // Широкая и близкая застройка: узкий город читался островом в пустоте,
      // а нужно, чтобы он уходил за края кадра
      const x = (rand() - 0.5) * 300;
      const z = -rand() * 430 + 110;

      // Оставляем свободный коридор по центру — это «улица» для полёта камеры
      if (Math.abs(x) < CORRIDOR_HALF_WIDTH) continue;

      // Ближе к центру — выше: так улица превращается в ущелье
      const distance = Math.abs(x);
      const nearCenter = Math.max(0, 1 - distance / 70);
      const height = 6 + rand() * 26 + nearCenter * 42;

      const width = 3 + rand() * 5;
      const depth = 3 + rand() * 5;

      dummy.position.set(x, height / 2, z);
      dummy.scale.set(width, height, depth);
      dummy.rotation.y = Math.round(rand() * 4) * (Math.PI / 2);
      dummy.updateMatrix();

      matrices.push(dummy.matrix.clone());
      seeds[placed] = rand() * 100;
      placed++;
    }

    return { seeds, matrices };
  }, [count]);

  useLayoutEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    matrices.forEach((matrix, i) => mesh.setMatrixAt(i, matrix));
    mesh.instanceMatrix.needsUpdate = true;
    mesh.count = matrices.length;
  }, [matrices]);

  useFrame((_, delta) => {
    if (materialRef.current) materialRef.current.uniforms.uTime.value += delta;
  });

  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uWindow: { value: new THREE.Color('#5EA8FF') },
      uAccent: { value: new THREE.Color('#38E0FF') },
      uFogColor: { value: new THREE.Color('#05070D') },
      uFogDensity: { value: 0.0075 },
    }),
    [],
  );

  return (
    <instancedMesh
      ref={meshRef}
      args={[undefined, undefined, count]}
      frustumCulled={false}
    >
      <boxGeometry args={[1, 1, 1]}>
        <instancedBufferAttribute attach="attributes-aSeed" args={[seeds, 1]} />
      </boxGeometry>
      <shaderMaterial
        ref={materialRef}
        vertexShader={vertexShader}
        fragmentShader={fragmentShader}
        uniforms={uniforms}
      />
    </instancedMesh>
  );
}
