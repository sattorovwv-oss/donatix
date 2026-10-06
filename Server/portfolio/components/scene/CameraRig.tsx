'use client';

/**
 * Полёт камеры по улице, привязанный к прокрутке.
 *
 * Сценарий: сначала камера висит высоко над городом, затем снижается
 * в «ущелье» между зданиями и всю остальную страницу летит вперёд —
 * к светящейся фигуре в конце улицы. Фигура приближается постепенно,
 * поэтому у прокрутки появляется цель.
 */

import { useEffect, useRef, type RefObject } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';

const easeOutCubic = (x: number) => 1 - Math.pow(1 - x, 3);

/** Доля прокрутки, за которую камера снижается с высоты на улицу */
const DESCENT = 0.16;

export default function CameraRig({ progress }: { progress: RefObject<number> }) {
  const { camera } = useThree();
  const pointer = useRef({ x: 0, y: 0, targetX: 0, targetY: 0 });
  const lookAt = useRef(new THREE.Vector3(0, 8, -40));

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      pointer.current.targetX = (event.clientX / window.innerWidth - 0.5) * 2;
      pointer.current.targetY = (event.clientY / window.innerHeight - 0.5) * 2;
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    return () => window.removeEventListener('pointermove', onMove);
  }, []);

  useFrame((state, delta) => {
    const p = progress.current;
    const time = state.clock.elapsedTime;

    // Снижение отрабатывает в начале прокрутки, дальше камера идёт ровно.
    // Старт намеренно высокий и далёкий: сперва город виден силуэтом целиком,
    // и только потом камера ныряет в «ущелье» между зданиями
    const descent = easeOutCubic(Math.min(p / DESCENT, 1));
    const height = 62 - descent * 52;              // 62 → 10
    const depth = 150 - p * 235;                   // 150 → −85

    // С высоты нужно смотреть ДАЛЕКО вперёд, иначе взгляд упирается
    // в пустую землю перед городом. У земли — наоборот, коротко,
    // тогда здания проносятся мимо и появляется ощущение скорости
    const lookAhead = 150 - descent * 105;         // 150 → 45
    const lookHeight = 27 - descent * 20;          // 27 → 7

    // Кадр всегда чуть «дышит», иначе сцена выглядит замершей скриншотом
    const bob = Math.sin(time * 0.5) * 0.35;

    // Плавное догоняние вместо резкого присвоения — так нет рывков на тачпаде
    const damp = 1 - Math.pow(0.001, delta);
    pointer.current.x += (pointer.current.targetX - pointer.current.x) * damp;
    pointer.current.y += (pointer.current.targetY - pointer.current.y) * damp;

    camera.position.set(
      pointer.current.x * 3.2,
      height + bob - pointer.current.y * 1.6,
      depth,
    );

    // Цель взгляда уезжает вперёд вместе с камерой и к концу пути
    // естественным образом наводится на фигуру в конце улицы
    lookAt.current.set(
      pointer.current.x * 1.4,
      lookHeight,
      depth - lookAhead,
    );
    camera.lookAt(lookAt.current);
  });

  return null;
}
