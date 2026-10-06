'use client';

/**
 * Цифровые частицы, висящие в объёме города.
 * Медленно поднимаются вверх и зацикливаются — создают ощущение живого воздуха
 * между зданиями и дают глазу опору на глубину.
 */

import { useEffect, useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createSoftSprite } from '@/lib/softSprite';

const SPREAD_X = 150;
const SPREAD_Y = 90;
const SPREAD_Z = 380;

export default function Particles({ count }: { count: number }) {
  const pointsRef = useRef<THREE.Points>(null);
  const sprite = useMemo(() => createSoftSprite(), []);

  // Текстура создаётся вручную, поэтому и освобождать её нужно вручную
  useEffect(() => () => sprite.dispose(), [sprite]);

  const { positions, speeds, colors } = useMemo(() => {
    let state = 90210;
    const rand = () => {
      state = (state * 1664525 + 1013904223) % 4294967296;
      return state / 4294967296;
    };

    const positions = new Float32Array(count * 3);
    const speeds = new Float32Array(count);
    const colors = new Float32Array(count * 3);

    const blue = new THREE.Color('#5EA8FF');
    const cyan = new THREE.Color('#38E0FF');
    const pale = new THREE.Color('#DCE8FF');
    const tmp = new THREE.Color();

    for (let i = 0; i < count; i++) {
      positions[i * 3] = (rand() - 0.5) * SPREAD_X;
      positions[i * 3 + 1] = rand() * SPREAD_Y;
      positions[i * 3 + 2] = -rand() * SPREAD_Z + 60;

      speeds[i] = 0.6 + rand() * 2.2;

      const pick = rand();
      tmp.copy(pick < 0.55 ? blue : pick < 0.85 ? cyan : pale);
      colors[i * 3] = tmp.r;
      colors[i * 3 + 1] = tmp.g;
      colors[i * 3 + 2] = tmp.b;
    }

    return { positions, speeds, colors };
  }, [count]);

  useFrame((_, delta) => {
    const geometry = pointsRef.current?.geometry;
    if (!geometry) return;

    const array = geometry.attributes.position.array as Float32Array;
    const step = Math.min(delta, 0.05);

    for (let i = 0; i < count; i++) {
      const y = i * 3 + 1;
      array[y] += speeds[i] * step;
      // Улетела за потолок — возвращаем под землю, поток не прерывается
      if (array[y] > SPREAD_Y) array[y] = -4;
    }
    geometry.attributes.position.needsUpdate = true;
  });

  return (
    <points ref={pointsRef} frustumCulled={false}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-color" args={[colors, 3]} />
      </bufferGeometry>
      <pointsMaterial
        size={0.42}
        map={sprite}
        vertexColors
        transparent
        opacity={0.85}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
        sizeAttenuation
      />
    </points>
  );
}
