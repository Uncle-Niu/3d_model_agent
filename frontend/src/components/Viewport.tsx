/**
 * 3D Viewport component — renders glTF models using React Three Fiber.
 */

import { Canvas } from '@react-three/fiber';
import { OrbitControls, Environment, Grid, Center, useGLTF } from '@react-three/drei';
import { Suspense, useEffect, useRef } from 'react';
import * as THREE from 'three';
import { useViewportStore } from '../stores';

function Model({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  const ref = useRef<THREE.Group>(null);

  useEffect(() => {
    // Apply default material improvements
    scene.traverse((child) => {
      if ((child as THREE.Mesh).isMesh) {
        const mesh = child as THREE.Mesh;
        if (mesh.material instanceof THREE.MeshStandardMaterial) {
          mesh.material.roughness = 0.4;
          mesh.material.metalness = 0.1;
        }
      }
    });
  }, [scene]);

  return (
    <Center>
      <primitive ref={ref} object={scene} />
    </Center>
  );
}

function LoadingIndicator() {
  return (
    <mesh>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color="#4a90d9" wireframe />
    </mesh>
  );
}

function EmptyState() {
  return (
    <mesh position={[0, 0.5, 0]}>
      <boxGeometry args={[2, 2, 2]} />
      <meshStandardMaterial
        color="#2a2a3a"
        transparent
        opacity={0.15}
        wireframe
      />
    </mesh>
  );
}

export default function Viewport() {
  const { glbUrl, isLoading } = useViewportStore();

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <Canvas
        camera={{ position: [80, 60, 80], fov: 45, near: 0.1, far: 10000 }}
        style={{ background: '#0a0a12' }}
      >
        {/* Lighting */}
        <ambientLight intensity={0.4} />
        <directionalLight position={[10, 15, 10]} intensity={1.0} castShadow />
        <directionalLight position={[-10, 10, -5]} intensity={0.3} />
        <Environment preset="studio" />

        {/* Grid */}
        <Grid
          position={[0, -0.01, 0]}
          args={[300, 300]}
          cellSize={10}
          cellThickness={0.5}
          cellColor="#1a1a2e"
          sectionSize={50}
          sectionThickness={1}
          sectionColor="#2a2a4e"
          fadeDistance={400}
          infiniteGrid
        />

        {/* Model or empty state */}
        <Suspense fallback={<LoadingIndicator />}>
          {glbUrl ? (
            <Model url={glbUrl} />
          ) : (
            <EmptyState />
          )}
        </Suspense>

        {/* Controls */}
        <OrbitControls
          makeDefault
          enableDamping
          dampingFactor={0.1}
          minDistance={5}
          maxDistance={500}
        />
      </Canvas>

      {/* Viewport overlay info */}
      {isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-loading">Loading model...</div>
        </div>
      )}

      {!glbUrl && !isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-empty">
            Send a message to generate a 3D model
          </div>
        </div>
      )}
    </div>
  );
}
