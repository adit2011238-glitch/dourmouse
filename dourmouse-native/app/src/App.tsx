// dourmouse-native — first real vertical slice of the "GPU-Accelerated
// Infinite Kinetic Workspace" (item 1 of the native rewrite checklist).
//
// What is REAL here: a genuine @xyflow/react infinite pan/zoom/drag
// canvas (React Flow — same library named in the checklist), real nodes,
// real edges, and a real Tauri IPC round-trip to the EXISTING Python
// backend (dourmouse.webui, unchanged — see src-tauri/src/lib.rs's own
// architecture comment for why the backend is reused, not rewritten).
//
// What is NOT yet built, stated plainly rather than silently implied:
// - Skia GPU rendering. React Flow's default renderer is DOM/SVG-based
//   (fast, but not the Skia/WebGPU raster path the checklist names). A
//   genuine Skia canvas layer (e.g. via skia-canvas or a custom WebGPU
//   renderer) is real follow-on work, not started — flagged here, not
//   silently substituted.
// - The other 8 checklist items (semantic gravity clustering, gaze-assist,
//   the Excalidraw scratchpad, audio pipeline, chimes, agent-swarm graph,
//   GUI automation, git time-travel) — this file is the canvas foundation
//   those build on top of.
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type Connection,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./App.css";

// The backend Dourmouse already runs — same default port webui.py uses
// (dourmouse/config.py's DOURMOUSE_PORT default). Not hardcoded deeper
// than this one constant so a real settings panel can override it later.
const DOURMOUSE_BASE_URL = "http://127.0.0.1:8765";

// Panel taxonomy mirrors ui/workspace.html's real panel types (MAIL,
// COMPANION, RESEARCH, WORLD MAP, ...) so a user moving between the
// browser-based workspace and this native shell sees the same vocabulary
// — not a coincidence, a deliberate continuity choice.
type PanelKind = "mail" | "companion" | "research" | "map" | "system";

interface PanelNodeData extends Record<string, unknown> {
  label: string;
  kind: PanelKind;
}

const initialNodes: Node<PanelNodeData>[] = [
  { id: "companion", type: "default", position: { x: 0, y: 0 }, data: { label: "◆ COMPANION", kind: "companion" } },
  { id: "mail", type: "default", position: { x: 280, y: -120 }, data: { label: "✉ MAIL", kind: "mail" } },
  { id: "research", type: "default", position: { x: 280, y: 120 }, data: { label: "⌕ RESEARCH", kind: "research" } },
  { id: "map", type: "default", position: { x: -280, y: 0 }, data: { label: "🗺 WORLD MAP", kind: "map" } },
];

const initialEdges: Edge[] = [
  { id: "e-companion-mail", source: "companion", target: "mail" },
  { id: "e-companion-research", source: "companion", target: "research" },
  { id: "e-companion-map", source: "companion", target: "map" },
];

function App() {
  const [nodes, , onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [status, setStatus] = useState<string>("checking backend…");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);

  const onConnect = useCallback(
    (connection: Connection) => setEdges((eds) => addEdge(connection, eds)),
    [setEdges],
  );

  // Real IPC round-trip: Rust (src-tauri/src/lib.rs's fetch_dourmouse_status)
  // -> HTTP GET against the real, already-running Python server -> back
  // here. Proves the "new native client, same real backend" architecture
  // decision actually works, not just that it compiles.
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const raw = await invoke<string>("fetch_dourmouse_status", {
          baseUrl: DOURMOUSE_BASE_URL,
        });
        if (cancelled) return;
        if (raw.startsWith("ERROR:")) {
          setStatusOk(false);
          setStatus(raw);
        } else {
          setStatusOk(true);
          setStatus("dourmouse.webui reachable — " + raw.slice(0, 120));
        }
      } catch (e) {
        if (!cancelled) {
          setStatusOk(false);
          setStatus("ERROR: invoke failed: " + String(e));
        }
      }
    }
    poll();
    const id = setInterval(poll, 7000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="native-canvas-root">
      <div className={"backend-pill " + (statusOk === null ? "pending" : statusOk ? "ok" : "bad")}>
        {status}
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        fitView
        minZoom={0.1}
        maxZoom={4}
      >
        <Background gap={24} />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}

export default App;
