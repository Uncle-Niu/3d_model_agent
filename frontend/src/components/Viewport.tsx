/**
 * 3D Viewport component — renders glTF models using React Three Fiber.
 *
 * Features:
 * - GLB model rendering with orbit controls
 * - Wireframe mode toggle
 * - Bounding box overlay
 * - Camera preset buttons (Iso, Front, Right, Top)
 * - Assembly-level mesh click → selection → WS selection message
 * - Visual highlight of selected mesh
 * - Visual highlight of selected mesh
 */

import { Canvas, useThree } from '@react-three/fiber';
import { OrbitControls, Environment, Grid, Center, useGLTF, Box } from '@react-three/drei';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { useViewportStore, useSelectionStore, useAssemblyStore } from '../stores';

// ---------------------------------------------------------------------------
// Camera preset helper — imperative camera moves
// ---------------------------------------------------------------------------

const CAMERA_PRESETS = {
  iso:   new THREE.Vector3(80, 60, 80),
  front: new THREE.Vector3(0, 0, 160),
  right: new THREE.Vector3(160, 0, 0),
  top:   new THREE.Vector3(0, 160, 0),
} as const;

type CameraPreset = keyof typeof CAMERA_PRESETS;

function CameraController({ preset }: { preset: CameraPreset | null }) {
  const { camera, controls } = useThree() as any;
  useEffect(() => {
    if (!preset) return;
    const target = CAMERA_PRESETS[preset].clone();
    camera.position.copy(target);
    camera.lookAt(0, 0, 0);
    if (controls && controls.target) {
      controls.target.set(0, 0, 0);
      controls.update();
    }
  }, [preset, camera, controls]);
  return null;
}

// ---------------------------------------------------------------------------
// Model renderer with wireframe + selection highlight
// ---------------------------------------------------------------------------

interface ModelProps {
  url: string;
  wireframe: boolean;
  selectedMeshName: string | null;
  onMeshClick: (name: string | null, point: THREE.Vector3) => void;
  partsVisibility: Record<string, boolean>;
  explodedFactor: number;
}

function Model({ url, wireframe, selectedMeshName, onMeshClick, partsVisibility, explodedFactor }: ModelProps) {
  const { scene } = useGLTF(url);
  const ref = useRef<THREE.Group>(null);
  const originalColors = useRef<Map<string, THREE.Color>>(new Map());
  const partCenters = useRef<Map<string, THREE.Vector3>>(new Map());

  // Assign cadName from scene node names (for assembly-level selection)
  useEffect(() => {
    scene.traverse((child) => {
      if ((child as THREE.Mesh).isMesh) {
        const mesh = child as THREE.Mesh;
        // Preserve original color
        if (!originalColors.current.has(mesh.uuid)) {
          const mat = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
          if (mat instanceof THREE.MeshStandardMaterial) {
            originalColors.current.set(mesh.uuid, mat.color.clone());
          }
        }
        // Set cadName in userData for raycasting readout
        if (!mesh.userData.cadName) {
          mesh.userData.cadName = mesh.name || `mesh_${mesh.uuid.slice(0, 6)}`;
        }
        
        // Calculate part center for exploded view
        if (!partCenters.current.has(mesh.userData.cadName)) {
           const box = new THREE.Box3().setFromObject(mesh);
           const center = new THREE.Vector3();
           box.getCenter(center);
           partCenters.current.set(mesh.userData.cadName, center);
        }
      }
    });
  }, [scene]);

  // Apply material settings + selection highlight + visibility + exploded view
  useEffect(() => {
    scene.traverse((child) => {
      if ((child as THREE.Mesh).isMesh) {
        const mesh = child as THREE.Mesh;
        const cadName = mesh.userData.cadName;
        const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
        const isSelected = cadName === selectedMeshName;
        
        // Visibility
        mesh.visible = partsVisibility[cadName] !== false;

        // Exploded View
        if (explodedFactor > 0 && partCenters.current.has(cadName)) {
          const center = partCenters.current.get(cadName)!;
          // Move part away from origin based on its center vector
          mesh.position.copy(center).multiplyScalar(explodedFactor);
        } else {
          mesh.position.set(0, 0, 0);
        }

        mats.forEach((mat) => {
          if (mat instanceof THREE.MeshStandardMaterial) {
            mat.roughness = 0.4;
            mat.metalness = 0.1;
            mat.wireframe = wireframe;

            if (isSelected) {
              mat.color.set('#ff9020');
              mat.emissive.set('#331800');
            } else {
              const orig = originalColors.current.get(mesh.uuid);
              if (orig) mat.color.copy(orig);
              mat.emissive.set('#000000');
            }
            mat.needsUpdate = true;
          }
        });
      }
    });
  }, [scene, wireframe, selectedMeshName, partsVisibility, explodedFactor]);

  function handleClick(e: any) {
    e.stopPropagation();
    const mesh = e.object as THREE.Mesh;
    const cadName = mesh.userData.cadName ?? null;
    onMeshClick(cadName, e.point);
  }

  function handleMissedClick() {
    onMeshClick(null, new THREE.Vector3());
  }

  return (
    <Center>
      <primitive
        ref={ref}
        object={scene}
        onClick={handleClick}
        onPointerMissed={handleMissedClick}
      />
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

interface ViewportProps {
  /** Optional callback to expose selection events to the parent */
  onSelect?: (cadName: string | null, point: THREE.Vector3) => void;
  /** Optional ref to WS send function for selection messages */
  sendWsMessage?: ((msg: object) => void) | null;
}

export default function Viewport({ onSelect, sendWsMessage }: ViewportProps = {}) {
  const { glbUrl, isLoading } = useViewportStore();
  const { partsVisibility, explodedFactor } = useAssemblyStore();
  const [wireframe, setWireframe] = useState(false);
  const [showBbox, setShowBbox] = useState(false);
  const [cameraPreset, setCameraPreset] = useState<CameraPreset | null>(null);
  const { selectedFeatureName, setSelection } = useSelectionStore();

  // Reset view modes when model changes
  useEffect(() => {
    setWireframe(false);
    setShowBbox(false);
    setSelection(null);
    setCameraPreset(null);
  }, [glbUrl, setSelection]);

  const handleMeshClick = useCallback(
    (cadName: string | null, point: THREE.Vector3) => {
      setSelection(cadName, point);

      // Notify parent
      onSelect?.(cadName, point);

      // Send WS selection message if connected
      if (cadName && sendWsMessage) {
        sendWsMessage({
          type: 'selection',
          feature_name: cadName,
          point: [point.x, point.y, point.z],
        });
      }
    },
    [onSelect, sendWsMessage, setSelection]
  );

  function handlePreset(preset: CameraPreset) {
    setCameraPreset(preset);
    // Reset after one frame so repeated clicks to same preset still trigger
    setTimeout(() => setCameraPreset(null), 50);
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
              <Model
                url={glbUrl}
                wireframe={wireframe}
                selectedMeshName={selectedFeatureName}
                onMeshClick={handleMeshClick}
                partsVisibility={partsVisibility}
                explodedFactor={explodedFactor}
              />
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

        {/* Camera preset controller */}
        <CameraController preset={cameraPreset} />
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

      {/* Selection indicator */}
      {selectedFeatureName && (
        <div className="viewport-selection-indicator" title="Click elsewhere to deselect">
          <span className="viewport-selection-icon">◆</span>
          <span className="viewport-selection-name">{selectedFeatureName}</span>
          <button
            className="viewport-selection-clear"
            onClick={() => setSelection(null)}
            aria-label="Clear selection"
          >
            ✕
          </button>
        </div>
      )}

      {/* Viewport toolbar — top-right buttons */}
      {glbUrl && !isLoading && (
        <div className="viewport-toolbar">
          {/* View preset buttons */}
          <div className="viewport-view-presets">
            {(['iso', 'front', 'right', 'top'] as CameraPreset[]).map((preset) => (
              <button
                key={preset}
                className="viewport-tool-btn viewport-preset-btn"
                onClick={() => handlePreset(preset)}
                title={`${preset.charAt(0).toUpperCase() + preset.slice(1)} view`}
              >
                {preset === 'iso' ? '◈' : preset === 'front' ? '↕' : preset === 'right' ? '↔' : '⊕'}
                {' '}{preset}
              </button>
            ))}
          </div>

          <div className="viewport-toolbar-divider" />

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
    </div>
  );
}
