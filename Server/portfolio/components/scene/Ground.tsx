'use client';

/**
 * Земля под городом.
 *
 * Без неё здания висят в пустоте и сцена читается как набор кубов.
 * Сетка рисуется шейдером и гаснет вдаль вместе с туманом — получается
 * линия горизонта, на которую опирается весь силуэт города.
 */

import { useMemo } from 'react';
import * as THREE from 'three';

const vertexShader = /* glsl */ `
  varying vec2  vWorld;
  varying float vFogDepth;

  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorld = world.xz;

    vec4 viewPos = viewMatrix * world;
    vFogDepth = -viewPos.z;

    gl_Position = projectionMatrix * viewPos;
  }
`;

const fragmentShader = /* glsl */ `
  precision highp float;

  uniform vec3  uLine;
  uniform vec3  uFogColor;
  uniform float uFogDensity;

  varying vec2  vWorld;
  varying float vFogDepth;

  /** Линия толщиной в один экранный пиксель независимо от расстояния */
  float gridLine(vec2 coord, float step) {
    vec2 g = abs(fract(coord / step - 0.5) - 0.5) / fwidth(coord / step);
    return 1.0 - min(min(g.x, g.y), 1.0);
  }

  void main() {
    float fine  = gridLine(vWorld, 8.0)  * 0.30;
    float major = gridLine(vWorld, 40.0) * 0.55;

    vec3 color = vec3(0.008, 0.011, 0.020) + uLine * (fine + major);

    float fogFactor = 1.0 - exp(-uFogDensity * uFogDensity * vFogDepth * vFogDepth);
    color = mix(color, uFogColor, clamp(fogFactor, 0.0, 1.0));

    gl_FragColor = vec4(color, 1.0);
  }
`;

export default function Ground() {
  const uniforms = useMemo(
    () => ({
      uLine: { value: new THREE.Color('#2F7BFF') },
      uFogColor: { value: new THREE.Color('#05070D') },
      uFogDensity: { value: 0.0075 },
    }),
    [],
  );

  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.05, -110]}>
      <planeGeometry args={[900, 900]} />
      <shaderMaterial
        vertexShader={vertexShader}
        fragmentShader={fragmentShader}
        uniforms={uniforms}
      />
    </mesh>
  );
}
