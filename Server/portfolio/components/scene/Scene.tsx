'use client';

/**
 * Фоновая 3D-сцена: город, частицы, фигура и свечение.
 *
 * Canvas монтируется только после гидратации — WebGL нельзя отрисовать
 * во время статической сборки, а попытка это сделать ломает экспорт.
 * До монтирования на экране остаётся градиент, поэтому чёрной вспышки нет.
 */

import { useEffect, useState } from 'react';
import { Canvas } from '@react-three/fiber';
import { EffectComposer, Bloom } from '@react-three/postprocessing';
import * as THREE from 'three';

import City from './City';
import Ground from './Ground';
import Particles from './Particles';
import Avatar from './Avatar';
import CameraRig from './CameraRig';
import { useScrollProgress } from '@/lib/useScrollProgress';
import { scene as sceneConfig } from '@/config/site';

export default function Scene() {
  const [mounted, setMounted] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);

  const progress = useScrollProgress();

  useEffect(() => {
    setIsMobile(window.matchMedia('(max-width: 768px)').matches);
    setReducedMotion(window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    setMounted(true);
  }, []);

  const buildings = isMobile ? sceneConfig.buildings.mobile : sceneConfig.buildings.desktop;
  const particles = isMobile ? sceneConfig.particles.mobile : sceneConfig.particles.desktop;

  return (
    <div className="scene-root" aria-hidden="true">
      {mounted && (
        <Canvas
          // Матрицы камеры полностью ведёт CameraRig
          camera={{ fov: 58, near: 0.5, far: 460, position: [0, 40, 62] }}
          // Ограничиваем плотность пикселей: на Retina без этого рендер
          // считает вчетверо больше точек и телефон греется
          dpr={[1, isMobile ? 1.5 : 2]}
          gl={{
            antialias: !isMobile,
            powerPreference: 'high-performance',
            alpha: false,
          }}
          onCreated={({ gl, scene }) => {
            gl.setClearColor('#05070D', 1);
            gl.toneMapping = THREE.ACESFilmicToneMapping;
            gl.toneMappingExposure = 1.05;
            scene.fog = new THREE.FogExp2('#05070D', 0.0075);
          }}
        >
          <CameraRig progress={progress} />

          <Ground />
          <City count={buildings} />
          <Particles count={particles} />
          <Avatar count={isMobile ? 1100 : 2400} position={[0, 0, -130]} scale={10} />

          {/* Bloom — самый дорогой эффект, на телефонах и при отключённой
              анимации его нет: там важнее стабильные кадры */}
          {!isMobile && !reducedMotion && (
            <EffectComposer>
              <Bloom
                intensity={sceneConfig.bloom.intensity}
                luminanceThreshold={sceneConfig.bloom.threshold}
                luminanceSmoothing={0.28}
                mipmapBlur
              />
            </EffectComposer>
          )}
        </Canvas>
      )}

      <div className="scene-vignette" />
    </div>
  );
}
