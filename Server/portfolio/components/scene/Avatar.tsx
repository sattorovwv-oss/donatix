'use client';

/**
 * Абстрактная фигура разработчика.
 *
 * Готовой 3D-модели человека нет, и плохая модель испортила бы весь сайт.
 * Поэтому фигура собирается из частиц по капсулам скелета: получается
 * голограмма, которая честно выглядит намеренной, а не дешёвой.
 */

import { useEffect, useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createSoftSprite } from '@/lib/softSprite';

/** Скелет: пары точек и толщина «кости» */
const BONES: Array<{ a: [number, number, number]; b: [number, number, number]; r: number; w: number }> = [
  // голова
  { a: [0, 1.62, 0], b: [0, 1.84, 0], r: 0.15, w: 1.4 },
  // шея и корпус
  { a: [0, 1.5, 0], b: [0, 1.6, 0], r: 0.06, w: 0.4 },
  { a: [0, 0.95, 0], b: [0, 1.5, 0], r: 0.19, w: 2.6 },
  // плечи
  { a: [-0.34, 1.45, 0], b: [0.34, 1.45, 0], r: 0.08, w: 0.9 },
  // руки
  { a: [-0.34, 1.45, 0], b: [-0.46, 1.05, 0.06], r: 0.07, w: 0.8 },
  { a: [-0.46, 1.05, 0.06], b: [-0.4, 0.68, 0.16], r: 0.06, w: 0.7 },
  { a: [0.34, 1.45, 0], b: [0.46, 1.05, 0.06], r: 0.07, w: 0.8 },
  { a: [0.46, 1.05, 0.06], b: [0.4, 0.68, 0.16], r: 0.06, w: 0.7 },
  // таз
  { a: [-0.18, 0.92, 0], b: [0.18, 0.92, 0], r: 0.09, w: 0.6 },
  // ноги
  { a: [-0.18, 0.92, 0], b: [-0.2, 0.48, 0], r: 0.08, w: 1.0 },
  { a: [-0.2, 0.48, 0], b: [-0.2, 0.04, 0], r: 0.07, w: 0.9 },
  { a: [0.18, 0.92, 0], b: [0.2, 0.48, 0], r: 0.08, w: 1.0 },
  { a: [0.2, 0.48, 0], b: [0.2, 0.04, 0], r: 0.07, w: 0.9 },
];

export default function Avatar({
  count = 2400,
  position = [0, 0, -46] as [number, number, number],
  scale = 7,
}) {
  const groupRef = useRef<THREE.Group>(null);
  const pointsRef = useRef<THREE.Points>(null);
  const sprite = useMemo(() => createSoftSprite(), []);

  useEffect(() => () => sprite.dispose(), [sprite]);

  const { positions, colors, offsets } = useMemo(() => {
    let state = 4242;
    const rand = () => {
      state = (state * 1664525 + 1013904223) % 4294967296;
      return state / 4294967296;
    };

    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const offsets = new Float32Array(count);

    // Частицы распределяются по костям пропорционально их «весу»,
    // иначе тонкие руки получат столько же точек, сколько корпус
    const totalWeight = BONES.reduce((sum, bone) => sum + bone.w, 0);

    const cyan = new THREE.Color('#38E0FF');
    const blue = new THREE.Color('#2F7BFF');
    const pale = new THREE.Color('#EAF1FF');
    const tmp = new THREE.Color();

    let i = 0;
    for (const bone of BONES) {
      const share = Math.round((bone.w / totalWeight) * count);

      for (let k = 0; k < share && i < count; k++, i++) {
        const t = rand();
        const x = bone.a[0] + (bone.b[0] - bone.a[0]) * t;
        const y = bone.a[1] + (bone.b[1] - bone.a[1]) * t;
        const z = bone.a[2] + (bone.b[2] - bone.a[2]) * t;

        // Точка сдвигается в случайную сторону от оси кости — получается объём
        const theta = rand() * Math.PI * 2;
        const phi = Math.acos(2 * rand() - 1);
        const radius = bone.r * (0.55 + rand() * 0.45);

        positions[i * 3] = x + Math.sin(phi) * Math.cos(theta) * radius;
        positions[i * 3 + 1] = y + Math.cos(phi) * radius * 0.7;
        positions[i * 3 + 2] = z + Math.sin(phi) * Math.sin(theta) * radius;

        offsets[i] = rand() * Math.PI * 2;

        // Голова и кисти светлее — взгляд цепляется за них первыми
        const bright = y > 1.55 || y < 0.15;
        const pick = rand();
        tmp.copy(bright && pick < 0.6 ? pale : pick < 0.6 ? cyan : blue);
        colors[i * 3] = tmp.r;
        colors[i * 3 + 1] = tmp.g;
        colors[i * 3 + 2] = tmp.b;
      }
    }

    // Хвост массива, если из-за округления долей не хватило точек
    for (; i < count; i++) {
      positions[i * 3] = 0;
      positions[i * 3 + 1] = 1.2;
      positions[i * 3 + 2] = 0;
      offsets[i] = rand() * Math.PI * 2;
      colors[i * 3] = cyan.r;
      colors[i * 3 + 1] = cyan.g;
      colors[i * 3 + 2] = cyan.b;
    }

    return { positions, colors, offsets };
  }, [count]);

  /** Исходные координаты — от них считается дыхание фигуры */
  const base = useMemo(() => positions.slice(), [positions]);

  useFrame((state, delta) => {
    const group = groupRef.current;
    const geometry = pointsRef.current?.geometry;
    if (!group || !geometry) return;

    const time = state.clock.elapsedTime;
    group.rotation.y += delta * 0.16;

    // Лёгкое дрожание частиц: фигура «дышит» и не выглядит замороженной
    const array = geometry.attributes.position.array as Float32Array;
    for (let i = 0; i < count; i++) {
      const wobble = Math.sin(time * 1.6 + offsets[i]) * 0.012;
      array[i * 3] = base[i * 3] + wobble;
      array[i * 3 + 1] = base[i * 3 + 1] + Math.cos(time * 1.3 + offsets[i]) * 0.012;
      array[i * 3 + 2] = base[i * 3 + 2] + wobble;
    }
    geometry.attributes.position.needsUpdate = true;
  });

  return (
    <group ref={groupRef} position={position} scale={scale}>
      <points ref={pointsRef} frustumCulled={false}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[positions, 3]} />
          <bufferAttribute attach="attributes-color" args={[colors, 3]} />
        </bufferGeometry>
        <pointsMaterial
          size={0.028}
          map={sprite}
          vertexColors
          transparent
          opacity={0.95}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          sizeAttenuation
        />
      </points>
    </group>
  );
}
