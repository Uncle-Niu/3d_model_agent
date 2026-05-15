/**
 * 3D Viewport component — renders glTF models using React Three Fiber.
 *
 * Selection model:
 *   - Mesh names from the GLB are normalised to `userData.cadName` (assembly-level).
 *   - On scene load we clone every material so highlight color changes don't
 *     leak across meshes that originally shared a material instance, and so
 *     they don't mutate the useGLTF cache. Original colors and original local
 *     positions are stashed in userData for restore.
 */

import { Canvas, useThree } from '@react-three/fiber';
import { OrbitControls, Environment, Grid, Center, useGLTF, Box, ContactShadows } from '@react-three/drei';
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

type CameraAction =
  | { kind: 'preset'; preset: CameraPreset; nonce: number }
  | { kind: 'fit'; nonce: number };

function CameraController({ action, fitTargetRef }: {
  action: CameraAction | null;
  fitTargetRef: React.MutableRefObject<THREE.Object3D | null>;
}) {
  const { camera, controls } = useThree() as any;
  useEffect(() => {
    if (!action) return;
    if (action.kind === 'preset') {
      const target = CAMERA_PRESETS[action.preset].clone();
      camera.position.copy(target);
      camera.lookAt(0, 0, 0);
      if (controls && controls.target) {
        controls.target.set(0, 0, 0);
        controls.update();
      }
    } else if (action.kind === 'fit') {
      const target = fitTargetRef.current;
      if (!target) return;
      const box = new THREE.Box3().setFromObject(target);
      if (box.isEmpty()) return;
      const size = new THREE.Vector3();
      box.getSize(size);
      const center = new THREE.Vector3();
      box.getCenter(center);
      const maxDim = Math.max(size.x, size.y, size.z) || 1;
      const persp = camera as THREE.PerspectiveCamera;
      const fov = (persp.fov * Math.PI) / 180;
      const distance = (maxDim / 2) / Math.tan(fov / 2) * 1.8;
      const dir = new THREE.Vector3(1, 0.75, 1).normalize();
      camera.position.copy(center.clone().add(dir.multiplyScalar(distance)));
      camera.lookAt(center);
      if (controls && controls.target) {
        controls.target.copy(center);
        controls.update();
      }
    }
  }, [action, camera, controls, fitTargetRef]);
  return null;
}

// ---------------------------------------------------------------------------
// Model renderer with wireframe + selection highlight
// ---------------------------------------------------------------------------

const GENERIC_NAME = (n: string) =>
  !n ||
  n.toLowerCase().startsWith('mesh') ||
  n.toLowerCase().startsWith('buffer') ||
  n.toLowerCase().startsWith('object_') ||
  /^\d+$/.test(n);

const HIGHLIGHT_COLOR = '#ff5fc1';
const HIGHLIGHT_EMISSIVE = '#5a0a3a';

interface ModelProps {
  url: string;
  wireframe: boolean;
  selectedMeshName: string | null;
  onMeshClick: (name: string | null, point: THREE.Vector3) => void;
  partsVisibility: Record<string, boolean>;
  explodedFactor: number;
  onSceneReady?: (scene: THREE.Object3D) => void;
}

function Model({ url, wireframe, selectedMeshName, onMeshClick, partsVisibility, explodedFactor, onSceneReady }: ModelProps) {
  const { scene: cachedScene } = useGLTF(url);

  // Clone the scene + materials once per url. This isolates us from the useGLTF
  // cache (so re-mounting doesn't see stale magenta) and ensures meshes that
  // share materials in the source GLB get independent material instances.
  const scene = useMemo(() => {
    const cloned = cachedScene.clone(true);
    cloned.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;
      mesh.material = Array.isArray(mesh.material)
        ? mesh.material.map((m) => m.clone())
        : mesh.material?.clone();
      mesh.castShadow = true;
      mesh.receiveShadow = true;
    });
    return cloned;
  }, [cachedScene]);

  const groupRef = useRef<THREE.Group>(null);

  // Assign cadName + cache original color, emissive, position once per mesh.
  useEffect(() => {
    scene.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;

      if (!mesh.userData.cadName) {
        let name = mesh.name;
        if (GENERIC_NAME(name)) name = '';
        let p = mesh.parent;
        while (!name && p && p !== scene) {
          if (!GENERIC_NAME(p.name)) name = p.name;
          p = p.parent;
        }
        mesh.userData.cadName = name || `mesh_${mesh.uuid.slice(0, 6)}`;
      }

      if (mesh.userData.origPos === undefined) {
        mesh.userData.origPos = mesh.position.clone();
      }

      if (mesh.userData.partCenter === undefined) {
        const box = new THREE.Box3().setFromObject(mesh);
        const center = new THREE.Vector3();
        box.getCenter(center);
        mesh.userData.partCenter = center;
      }

      if (mesh.userData.origColors === undefined) {
        const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
        mesh.userData.origColors = mats.map((m) =>
          m instanceof THREE.MeshStandardMaterial ? m.color.clone() : null
        );
        mesh.userData.origEmissive = mats.map((m) =>
          m instanceof THREE.MeshStandardMaterial ? m.emissive.clone() : null
        );
        mesh.userData.origEmissiveIntensity = mats.map((m) =>
          m instanceof THREE.MeshStandardMaterial ? m.emissiveIntensity : 0
        );
      }
    });
    onSceneReady?.(scene);
  }, [scene, onSceneReady]);

  // Apply selection / wireframe / visibility / explode every render.
  useEffect(() => {
    const selName = selectedMeshName?.toLowerCase().trim() || null;

    scene.traverse((child) => {
      const mesh = child as THREE.Mesh;
      if (!mesh.isMesh) return;

      const cadName: string = mesh.userData.cadName ?? '';
      const cadNameLc = cadName.toLowerCase().trim();
      const isSelected = !!selName && cadNameLc === selName;

      // Visibility
      mesh.visible = partsVisibility[cadName] !== false;

      // Exploded view from cached part center (relative to original position).
      const origPos = mesh.userData.origPos as THREE.Vector3 | undefined;
      const partCenter = mesh.userData.partCenter as THREE.Vector3 | undefined;
      if (explodedFactor > 0 && partCenter && origPos) {
        mesh.position
          .copy(origPos)
          .add(partCenter.clone().multiplyScalar(explodedFactor));
      } else if (origPos) {
        mesh.position.copy(origPos);
      }

      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      const origColors = (mesh.userData.origColors ?? []) as Array<THREE.Color | null>;
      const origEmissive = (mesh.userData.origEmissive ?? []) as Array<THREE.Color | null>;
      const origEmissiveI = (mesh.userData.origEmissiveIntensity ?? []) as Array<number>;

      mats.forEach((mat, i) => {
        if (!(mat instanceof THREE.MeshStandardMaterial)) return;
        mat.wireframe = wireframe;
        mat.roughness = 0.55;
        mat.metalness = 0.2;

        if (isSelected) {
          mat.color.set(HIGHLIGHT_COLOR);
          mat.emissive.set(HIGHLIGHT_EMISSIVE);
          mat.emissiveIntensity = 0.9;
        } else {
          const oc = origColors[i];
          if (oc) {
            // Lift pure white slightly so it's not blown out under the env map.
            if (oc.r > 0.95 && oc.g > 0.95 && oc.b > 0.95) {
              mat.color.setRGB(0.82, 0.83, 0.88);
            } else {
              mat.color.copy(oc);
            }
          }
          if (origEmissive[i]) mat.emissive.copy(origEmissive[i] as THREE.Color);
          mat.emissiveIntensity = origEmissiveI[i] ?? 0;
        }
        mat.needsUpdate = true;
      });
    });
  }, [scene, wireframe, selectedMeshName, partsVisibility, explodedFactor]);

  function handleClick(e: any) {
    e.stopPropagation();
    let obj: THREE.Object3D | null = e.object;
    // Walk up to find the named mesh node (in case click hits a sub-primitive).
    while (obj && !(obj as THREE.Mesh).isMesh) obj = obj.parent;
    const mesh = obj as THREE.Mesh | null;
    const cadName = mesh?.userData.cadName ?? null;
    onMeshClick(cadName, e.point);
  }

  function handleMissedClick() {
    onMeshClick(null, new THREE.Vector3());
  }

  return (
    <Center>
      <primitive
        ref={groupRef}
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
  const { glbUrl, isLoading, currentModelId } = useViewportStore();
  const { partsVisibility, explodedFactor } = useAssemblyStore();
  const [wireframe, setWireframe] = useState(false);
  const [showBbox, setShowBbox] = useState(false);
  const [cameraAction, setCameraAction] = useState<CameraAction | null>(null);
  const { selectedFeatureName, setSelection } = useSelectionStore();
  const sceneRef = useRef<THREE.Object3D | null>(null);

  // Reset view modes when model changes
  useEffect(() => {
    setWireframe(false);
    setShowBbox(false);
    setSelection(null);
    setCameraAction(null);
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

  const triggerPreset = (preset: CameraPreset) => {
    setCameraAction({ kind: 'preset', preset, nonce: Date.now() });
  };
  const triggerFit = () => {
    setCameraAction({ kind: 'fit', nonce: Date.now() });
  };

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <Canvas
        camera={{ position: [80, 60, 80], fov: 45, near: 0.1, far: 10000 }}
        style={{ background: '#0a0a12' }}
      >
        {/* Lighting */}
        <ambientLight intensity={0.2} />
        <hemisphereLight intensity={0.5} groundColor="#000000" />
        <directionalLight position={[20, 30, 20]} intensity={1.5} castShadow shadow-mapSize={[1024, 1024]} />
        <directionalLight position={[-15, 10, -10]} intensity={0.5} />
        <Environment preset="city" />

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
                onSceneReady={(s) => { sceneRef.current = s; }}
              />
              {showBbox && <BoundingBoxOverlay url={glbUrl} />}
              <ContactShadows
                position={[0, -0.01, 0]}
                opacity={0.6}
                scale={150}
                blur={2.5}
                far={20}
              />
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

        <CameraController action={cameraAction} fitTargetRef={sceneRef} />
      </Canvas>

      {/* Loading overlay */}
      {isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-loading">Loading model...</div>
        </div>
      )}

      {!glbUrl && !isLoading && (
        <div className="viewport-overlay">
          <div className="viewport-empty">
            {currentModelId
              ? 'This iteration didn’t produce viewable geometry. Open the Source panel to inspect the code.'
              : 'Send a message to generate a 3D model'}
          </div>
        </div>
      )}

      {/* Selection indicator */}
      {selectedFeatureName && (
        <div className="viewport-selection-indicator" title="Click empty space to deselect">
          <span className="viewport-selection-icon" aria-hidden="true" />
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
          {(['iso', 'front', 'right', 'top'] as CameraPreset[]).map((preset) => (
            <button
              key={preset}
              className="btn btn-ghost btn-sm viewport-preset-btn"
              onClick={() => triggerPreset(preset)}
              title={`${preset[0].toUpperCase() + preset.slice(1)} view`}
            >
              {preset}
            </button>
          ))}

          <button
            className="btn btn-ghost btn-sm"
            onClick={triggerFit}
            title="Fit model to view"
          >
            Fit
          </button>

          <div className="viewport-toolbar-divider" />

          <button
            className={`btn btn-ghost btn-sm ${wireframe ? 'is-active' : ''}`}
            onClick={() => setWireframe((v) => !v)}
            title="Toggle wireframe"
          >
            Wire
          </button>
          <button
            className={`btn btn-ghost btn-sm ${showBbox ? 'is-active' : ''}`}
            onClick={() => setShowBbox((v) => !v)}
            title="Toggle bounding box"
          >
            BBox
          </button>
        </div>
      )}
    </div>
  );
}
