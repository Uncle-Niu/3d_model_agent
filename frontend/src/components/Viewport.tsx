/**
 * 3D Viewport component — renders glTF models using React Three Fiber.
 *
 * Features:
 * - GLB model rendering with orbit controls
 * - Wireframe mode toggle
 * - Bounding box overlay
 * - Download menu (STL / STEP / GLB)
 */

import { Canvas } from '@react-three/fiber';
import { OrbitControls, Environment, Grid, Center, useGLTF, Box } from '@react-three/drei';
import { Suspense, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { useViewportStore } from '../stores';
import { api } from '../api';

// ---------------------------------------------------------------------------
// Model renderer with wireframe support
// ---------------------------------------------------------------------------

function Model({ url, wireframe }: { url: string; wireframe: boolean }) {
  const { scene } = useGLTF(url);
  const ref = useRef<THREE.Group>(null);

  useEffect(() => {
    scene.traverse((child) => {
      if ((child as THREE.Mesh).isMesh) {
        const mesh = child as THREE.Mesh;
        if (Array.isArray(mesh.material)) {
          mesh.material.forEach((mat) => {
            if (mat instanceof THREE.MeshStandardMaterial) {
              mat.roughness = 0.4;
              mat.metalness = 0.1;
              mat.wireframe = wireframe;
            }
          });
        } else if (mesh.material instanceof THREE.MeshStandardMaterial) {
          mesh.material.roughness = 0.4;
          mesh.material.metalness = 0.1;
          mesh.material.wireframe = wireframe;
        }
      }
    });
  }, [scene, wireframe]);

  return (
    <Center>
      <primitive ref={ref} object={scene} />
    </Center>
  );
}

// ---------------------------------------------------------------------------
// Bounding box wireframe overlay
// ---------------------------------------------------------------------------

function BoundingBoxOverlay({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  const [bbox, setBbox] = useState<THREE.Box3 | null>(null);

  useEffect(() => {
    const box = new THREE.Box3().setFromObject(scene);
    setBbox(box);
  }, [scene]);

  if (!bbox) return null;

  const size = new THREE.Vector3();
  bbox.getSize(size);
  const center = new THREE.Vector3();
  bbox.getCenter(center);

  return (
    <Box
      args={[size.x, size.y, size.z]}
      position={[center.x, center.y, center.z]}
    >
      <meshBasicMaterial wireframe color="#00aaff" transparent opacity={0.4} />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Loading / Empty states
// ---------------------------------------------------------------------------

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
      <meshStandardMaterial color="#2a2a3a" transparent opacity={0.15} wireframe />
    </mesh>
  );
}

// ---------------------------------------------------------------------------
// Main Viewport
// ---------------------------------------------------------------------------

export default function Viewport() {
  const { glbUrl, isLoading, currentModelId, currentProjectId } = useViewportStore();
  const [showDownloadMenu, setShowDownloadMenu] = useState(false);
  const [downloadingFormat, setDownloadingFormat] = useState<string | null>(null);
  const [wireframe, setWireframe] = useState(false);
  const [showBbox, setShowBbox] = useState(false);
  const downloadMenuRef = useRef<HTMLDivElement>(null);

  // Close download menu when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (downloadMenuRef.current && !downloadMenuRef.current.contains(event.target as Node)) {
        setShowDownloadMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Reset view modes when model changes
  useEffect(() => {
    setWireframe(false);
    setShowBbox(false);
  }, [glbUrl]);

  async function handleDownload(format: 'stl' | 'step' | 'glb') {
    if (!currentModelId || !currentProjectId) {
      alert('Model information not available');
      return;
    }
    try {
      setDownloadingFormat(format);
      await api.downloadFile(
        `/api/projects/${currentProjectId}/models/${currentModelId}/${format}`,
        `model-${currentModelId}.${format}`,
      );
      setShowDownloadMenu(false);
    } catch (err) {
      alert(`Failed to download ${format.toUpperCase()}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setDownloadingFormat(null);
    }
  }

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
            <>
              <Model url={glbUrl} wireframe={wireframe} />
              {showBbox && <BoundingBoxOverlay url={glbUrl} />}
            </>
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

      {/* Loading overlay */}
      {isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-loading">Loading model...</div>
        </div>
      )}

      {!glbUrl && !isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-empty">Send a message to generate a 3D model</div>
        </div>
      )}

      {/* Viewport toolbar — top-right buttons */}
      {glbUrl && !isLoading && (
        <div className="viewport-toolbar">
          <button
            className={`viewport-tool-btn ${wireframe ? 'active' : ''}`}
            onClick={() => setWireframe((v) => !v)}
            title="Toggle wireframe mode"
          >
            ◻ Wire
          </button>
          <button
            className={`viewport-tool-btn ${showBbox ? 'active' : ''}`}
            onClick={() => setShowBbox((v) => !v)}
            title="Toggle bounding box"
          >
            ⬜ BBox
          </button>
        </div>
      )}

      {/* Download button */}
      {glbUrl && !isLoading && (
        <div className="viewport-download-btn-container" ref={downloadMenuRef}>
          <button
            className="viewport-download-btn"
            onClick={() => setShowDownloadMenu(!showDownloadMenu)}
            disabled={downloadingFormat !== null}
            title="Download 3D model file"
          >
            {downloadingFormat ? (
              <>
                <span className="download-spinner" />
                Downloading...
              </>
            ) : (
              <>⬇️ Download</>
            )}
          </button>

          {showDownloadMenu && (
            <div className="viewport-download-menu">
              <button
                className="viewport-download-option"
                onClick={() => handleDownload('stl')}
                disabled={downloadingFormat !== null}
              >
                <span className="option-icon">📦</span>
                <span className="option-text">
                  <div className="option-title">STL File</div>
                  <div className="option-desc">Best for 3D printing</div>
                </span>
              </button>
              <button
                className="viewport-download-option"
                onClick={() => handleDownload('step')}
                disabled={downloadingFormat !== null}
              >
                <span className="option-icon">🔧</span>
                <span className="option-text">
                  <div className="option-title">STEP File</div>
                  <div className="option-desc">For CAD software</div>
                </span>
              </button>
              <button
                className="viewport-download-option"
                onClick={() => handleDownload('glb')}
                disabled={downloadingFormat !== null}
              >
                <span className="option-icon">🌐</span>
                <span className="option-text">
                  <div className="option-title">GLB File</div>
                  <div className="option-desc">For web/viewing</div>
                </span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
