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

import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { OrbitControls, Environment, Grid, Center, useGLTF, Box, ContactShadows, Line, Billboard, Text } from '@react-three/drei';
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

interface DimensionReadout {
  x: number;
  y: number;
  z: number;
  edges: StraightEdgeMeasurement[];
}

interface StraightEdgeMeasurement {
  id: string;
  length: number;
  start: THREE.Vector3;
  end: THREE.Vector3;
}

interface ModelProps {
  url: string;
  wireframe: boolean;
  showDimensions: boolean;
  onDimensionsChange: (dimensions: DimensionReadout | null) => void;
  selectedMeshName: string | null;
  onMeshClick: (name: string | null, point: THREE.Vector3) => void;
  partsVisibility: Record<string, boolean>;
  explodedFactor: number;
  onSceneReady?: (scene: THREE.Object3D) => void;
}

function Model({ url, wireframe, showDimensions, onDimensionsChange, selectedMeshName, onMeshClick, partsVisibility, explodedFactor, onSceneReady }: ModelProps) {
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
    <>
      <Center>
        <primitive
          ref={groupRef}
          object={scene}
          onClick={handleClick}
          onPointerMissed={handleMissedClick}
        />
      </Center>
      <DimensionOverlay target={scene} enabled={showDimensions} onDimensionsChange={onDimensionsChange} />
    </>
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
// Dimension measurement overlay
// ---------------------------------------------------------------------------

interface DimensionBounds {
  min: THREE.Vector3;
  max: THREE.Vector3;
  size: THREE.Vector3;
  edges: StraightEdgeMeasurement[];
}

function getVisibleMeshBounds(root: THREE.Object3D): THREE.Box3 | null {
  const bounds = new THREE.Box3().makeEmpty();
  const meshBounds = new THREE.Box3();

  root.updateWorldMatrix(true, true);
  root.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh || !mesh.visible || !mesh.geometry) return;

    const geometry = mesh.geometry as THREE.BufferGeometry;
    if (!geometry.boundingBox) geometry.computeBoundingBox();
    if (!geometry.boundingBox) return;

    meshBounds.copy(geometry.boundingBox).applyMatrix4(mesh.matrixWorld);
    bounds.union(meshBounds);
  });

  return bounds.isEmpty() ? null : bounds;
}

function getVisibleStraightEdges(root: THREE.Object3D) {
  const edges: Array<Omit<StraightEdgeMeasurement, 'id'>> = [];
  const start = new THREE.Vector3();
  const end = new THREE.Vector3();

  root.updateWorldMatrix(true, true);
  root.traverse((child) => {
    const mesh = child as THREE.Mesh;
    if (!mesh.isMesh || !mesh.visible || !mesh.geometry) return;

    const edgeGeometry = new THREE.EdgesGeometry(mesh.geometry as THREE.BufferGeometry, 12);
    const positions = edgeGeometry.getAttribute('position');
    for (let i = 0; i < positions.count; i += 2) {
      start.fromBufferAttribute(positions, i).applyMatrix4(mesh.matrixWorld);
      end.fromBufferAttribute(positions, i + 1).applyMatrix4(mesh.matrixWorld);
      const length = start.distanceTo(end);
      if (length > 0.01) {
        edges.push({
          length,
          start: start.clone(),
          end: end.clone(),
        });
      }
    }
    edgeGeometry.dispose();
  });

  const distinct = edges
    .sort((a, b) => b - a)
    .reduce<StraightEdgeMeasurement[]>((acc, edge) => {
      const tolerance = Math.max(0.1, edge.length * 0.002);
      if (!acc.some((existing) => Math.abs(existing.length - edge.length) <= tolerance)) {
        acc.push({
          ...edge,
          id: `E${acc.length + 1}`,
        });
      }
      return acc;
    }, []);

  return distinct.slice(0, 5);
}

function boundsChanged(a: DimensionBounds | null, b: THREE.Box3, epsilon = 0.001) {
  if (!a) return true;
  return (
    Math.abs(a.min.x - b.min.x) > epsilon ||
    Math.abs(a.min.y - b.min.y) > epsilon ||
    Math.abs(a.min.z - b.min.z) > epsilon ||
    Math.abs(a.max.x - b.max.x) > epsilon ||
    Math.abs(a.max.y - b.max.y) > epsilon ||
    Math.abs(a.max.z - b.max.z) > epsilon
  );
}

function formatDimension(value: number) {
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} mm`;
}

function GuideLine({ points, color = '#7bdcff', opacity = 0.86 }: {
  points: THREE.Vector3[];
  color?: string;
  opacity?: number;
}) {
  return (
    <Line
      points={points}
      color={color}
      lineWidth={1.5}
      transparent
      opacity={opacity}
      depthTest={false}
    />
  );
}

function AxisGuideLabel({ position, axis, color, size }: {
  position: THREE.Vector3;
  axis: 'X' | 'Y' | 'Z';
  color: string;
  size: number;
}) {
  return (
    <Billboard position={position}>
      <Text
        fontSize={size}
        color={color}
        anchorX="center"
        anchorY="middle"
        outlineWidth={size * 0.08}
        outlineColor="#080c14"
        depthTest={false}
        renderOrder={50}
      >
        {axis}
      </Text>
    </Billboard>
  );
}

function EdgeGuideLabel({ edge, color, size }: {
  edge: StraightEdgeMeasurement;
  color: string;
  size: number;
}) {
  const midpoint = edge.start.clone().lerp(edge.end, 0.5);
  const direction = edge.end.clone().sub(edge.start).normalize();
  const offset = new THREE.Vector3(-direction.z, 0.4, direction.x).normalize().multiplyScalar(size * 0.9);

  return (
    <Billboard position={midpoint.add(offset)}>
      <Text
        fontSize={size}
        color={color}
        anchorX="center"
        anchorY="middle"
        outlineWidth={size * 0.08}
        outlineColor="#080c14"
        depthTest={false}
        renderOrder={55}
      >
        {edge.id}
      </Text>
    </Billboard>
  );
}

function DimensionOverlay({ target, enabled, onDimensionsChange }: {
  target: THREE.Object3D;
  enabled: boolean;
  onDimensionsChange: (dimensions: DimensionReadout | null) => void;
}) {
  const [bounds, setBounds] = useState<DimensionBounds | null>(null);
  const boundsRef = useRef<DimensionBounds | null>(null);

  useEffect(() => {
    if (!enabled) {
      boundsRef.current = null;
      setBounds(null);
      onDimensionsChange(null);
    }
  }, [enabled, onDimensionsChange]);

  useFrame(() => {
    if (!enabled) return;

    const next = getVisibleMeshBounds(target);
    if (!next) {
      if (boundsRef.current) {
        boundsRef.current = null;
        setBounds(null);
        onDimensionsChange(null);
      }
      return;
    }

    if (!boundsChanged(boundsRef.current, next)) return;

    const size = new THREE.Vector3();
    next.getSize(size);
    const edges = getVisibleStraightEdges(target);
    const snapshot = {
      min: next.min.clone(),
      max: next.max.clone(),
      size,
      edges,
    };
    boundsRef.current = snapshot;
    setBounds(snapshot);
    onDimensionsChange({ x: size.x, y: size.y, z: size.z, edges });
  });

  if (!enabled || !bounds) return null;

  const { min, max, size, edges } = bounds;
  const maxDim = Math.max(size.x, size.y, size.z, 1);
  const pad = maxDim * 0.08 + 4;
  const tick = Math.min(Math.max(maxDim * 0.035, 2), 18);
  const labelSize = Math.min(Math.max(maxDim * 0.055, 4), 18);
  const edgeLabelSize = Math.min(Math.max(maxDim * 0.035, 3), 10);

  const xY = min.y - pad;
  const xZ = max.z + pad;
  const zX = max.x + pad;
  const zY = min.y - pad;
  const yX = max.x + pad;
  const yZ = max.z + pad;

  const v = (x: number, y: number, z: number) => new THREE.Vector3(x, y, z);

  const xStart = v(min.x, xY, xZ);
  const xEnd = v(max.x, xY, xZ);
  const zStart = v(zX, zY, min.z);
  const zEnd = v(zX, zY, max.z);
  const yStart = v(yX, min.y, yZ);
  const yEnd = v(yX, max.y, yZ);
  const guide = {
    x: '#7bdcff',
    y: '#9ee87d',
    z: '#ffd166',
    extension: '#8fb9c7',
    edge: '#f2f7ff',
  };

  return (
    <group>
      {edges.map((edge) => (
        <group key={`${edge.id}-${edge.length}`}>
          <GuideLine points={[edge.start, edge.end]} color={guide.edge} opacity={0.9} />
          <EdgeGuideLabel edge={edge} color={guide.edge} size={edgeLabelSize} />
        </group>
      ))}

      <GuideLine points={[xStart, xEnd]} color={guide.x} />
      <GuideLine points={[v(min.x, min.y, max.z), v(min.x, xY, max.z), v(min.x, xY, xZ)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(max.x, min.y, max.z), v(max.x, xY, max.z), v(max.x, xY, xZ)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(min.x, xY - tick * 0.25, xZ), v(min.x, xY + tick * 0.25, xZ)]} color={guide.x} />
      <GuideLine points={[v(max.x, xY - tick * 0.25, xZ), v(max.x, xY + tick * 0.25, xZ)]} color={guide.x} />
      <AxisGuideLabel position={v((min.x + max.x) / 2, xY - tick * 0.7, xZ)} axis="X" color={guide.x} size={labelSize} />

      <GuideLine points={[zStart, zEnd]} color={guide.z} />
      <GuideLine points={[v(max.x, min.y, min.z), v(zX, min.y, min.z), v(zX, zY, min.z)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(max.x, min.y, max.z), v(zX, min.y, max.z), v(zX, zY, max.z)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(zX - tick * 0.25, zY, min.z), v(zX + tick * 0.25, zY, min.z)]} color={guide.z} />
      <GuideLine points={[v(zX - tick * 0.25, zY, max.z), v(zX + tick * 0.25, zY, max.z)]} color={guide.z} />
      <AxisGuideLabel position={v(zX + tick * 0.7, zY, (min.z + max.z) / 2)} axis="Z" color={guide.z} size={labelSize} />

      <GuideLine points={[yStart, yEnd]} color={guide.y} />
      <GuideLine points={[v(max.x, min.y, max.z), v(yX, min.y, max.z), v(yX, min.y, yZ)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(max.x, max.y, max.z), v(yX, max.y, max.z), v(yX, max.y, yZ)]} color={guide.extension} opacity={0.65} />
      <GuideLine points={[v(yX - tick * 0.25, min.y, yZ), v(yX + tick * 0.25, min.y, yZ)]} color={guide.y} />
      <GuideLine points={[v(yX - tick * 0.25, max.y, yZ), v(yX + tick * 0.25, max.y, yZ)]} color={guide.y} />
      <AxisGuideLabel position={v(yX + tick * 0.7, (min.y + max.y) / 2, yZ)} axis="Y" color={guide.y} size={labelSize} />
    </group>
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
  const [showDimensions, setShowDimensions] = useState(true);
  const [dimensionReadout, setDimensionReadout] = useState<DimensionReadout | null>(null);
  const [cameraAction, setCameraAction] = useState<CameraAction | null>(null);
  const { selectedFeatureName, setSelection } = useSelectionStore();
  const sceneRef = useRef<THREE.Object3D | null>(null);

  // Reset view modes when model changes
  useEffect(() => {
    setWireframe(false);
    setShowBbox(false);
    setShowDimensions(true);
    setDimensionReadout(null);
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
                showDimensions={showDimensions}
                onDimensionsChange={setDimensionReadout}
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

      {glbUrl && !isLoading && showDimensions && dimensionReadout && (
        <div className="viewport-dim-readout" aria-label="Model dimensions">
          <div className="viewport-dim-readout-title">Bounds</div>
          <div className="viewport-dim-readout-row viewport-dim-readout-row--x">
            <span>X</span>
            <strong>{formatDimension(dimensionReadout.x)}</strong>
          </div>
          <div className="viewport-dim-readout-row viewport-dim-readout-row--y">
            <span>Y</span>
            <strong>{formatDimension(dimensionReadout.y)}</strong>
          </div>
          <div className="viewport-dim-readout-row viewport-dim-readout-row--z">
            <span>Z</span>
            <strong>{formatDimension(dimensionReadout.z)}</strong>
          </div>
          {dimensionReadout.edges.length > 0 && (
            <>
              <div className="viewport-dim-readout-title viewport-dim-readout-subtitle">
                Straight edges
              </div>
              {dimensionReadout.edges.map((edge) => (
                <div className="viewport-dim-readout-row viewport-dim-readout-row--edge" key={edge.id}>
                  <span>{edge.id}</span>
                  <strong>{formatDimension(edge.length)}</strong>
                </div>
              ))}
            </>
          )}
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
          <button
            className={`btn btn-ghost btn-sm ${showDimensions ? 'is-active' : ''}`}
            onClick={() => setShowDimensions((v) => !v)}
            title="Toggle model dimensions"
          >
            Dims
          </button>
        </div>
      )}
    </div>
  );
}
