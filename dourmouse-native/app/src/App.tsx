// dourmouse-native — items 1 + 7 of the native rewrite checklist.
//
// What is REAL here: a genuine @xyflow/react infinite pan/zoom/drag
// canvas (React Flow), rendering the REAL, live agent-swarm topology —
// dourmouse/webui.py's build_link_topology() (/api/links, deterministic
// node/edge structure of the whole real subagent roster) overlaid with
// dourmouse/webui.py's ActivityTracker.snapshot() (/api/activity, real
// live per-agent idle/computing/auth status, polled). Both are real Tauri
// IPC round-trips to the EXISTING Python backend (dourmouse.webui,
// unchanged — see src-tauri/src/lib.rs's own architecture comment).
//
// NOT a LangGraph orchestrator swap (item 7's checklist description
// mentions LangGraph, but see the architecture-decision comment in
// src-tauri/src/lib.rs and NATIVE_REWRITE_ROADMAP.md for why this
// VISUALIZES dispatch.py's real, unchanged orchestration instead of
// replacing it).
//
// What is NOT yet built, stated plainly rather than silently implied:
// - Skia GPU rendering. React Flow's default renderer is DOM/SVG-based
//   (fast, but not the Skia/WebGPU raster path the checklist names).
// - Live particle effects streaming per-tool-call along edges (the
//   checklist's own phrasing) — this polls a snapshot on an interval,
//   not a true real-time SSE event stream; a real future upgrade once
//   this shell has its own SSE client.
// - Items 2, 4, 8, 9 (semantic gravity clustering, Excalidraw scratchpad,
//   GUI automation, git time-travel).
import { useCallback, useEffect, useRef, useState } from "react";
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
import {
  topologyToNodes,
  topologyToEdges,
  statusColor,
  type Topology,
  type Activity,
  type AgentNodeData,
} from "./agentGraph";

// The backend Dourmouse already runs — same default port webui.py uses
// (dourmouse/config.py's DOURMOUSE_PORT default). Not hardcoded deeper
// than this one constant so a real settings panel can override it later.
const DOURMOUSE_BASE_URL = "http://127.0.0.1:8765";
const TOPOLOGY_POLL_MS = 15000; // the roster rarely changes mid-session
const ACTIVITY_POLL_MS = 2000; // live status wants a snappier refresh

function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<AgentNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [status, setStatus] = useState<string>("checking backend…");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  // Real topology fetched once (then re-polled slowly); kept in a ref so
  // the fast activity-poll effect can rebuild node styling from it without
  // re-fetching /api/links every 2 seconds.
  const topologyRef = useRef<Topology | null>(null);

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

  // Real topology: the deterministic node/edge structure of the whole
  // subagent roster. Fetched on mount and re-polled slowly (the roster
  // itself doesn't change during a normal session, but a restarted
  // backend with a different registry should still be reflected).
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const raw = await invoke<string>("fetch_agent_topology", { baseUrl: DOURMOUSE_BASE_URL });
        if (cancelled || raw.startsWith("ERROR:")) return;
        const topology = JSON.parse(raw) as Topology;
        topologyRef.current = topology;
        setNodes(topologyToNodes(topology, null));
        setEdges(topologyToEdges(topology));
      } catch {
        // Honest no-op: a failed/unparseable topology fetch leaves
        // whatever was last rendered in place rather than clearing the
        // canvas on a transient hiccup. The backend-status pill above
        // already reports connectivity problems.
      }
    }
    poll();
    const id = setInterval(poll, TOPOLOGY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [setNodes, setEdges]);

  // Real live activity: per-agent status (idle/computing/auth) + last
  // tool call, overlaid onto the topology already on screen -- never
  // refetches/rebuilds the topology itself, just updates each node's
  // data + visual status ring in place.
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const topology = topologyRef.current;
      if (!topology) return; // nothing to overlay onto yet
      try {
        const raw = await invoke<string>("fetch_agent_activity", { baseUrl: DOURMOUSE_BASE_URL });
        if (cancelled || raw.startsWith("ERROR:")) return;
        const activity = JSON.parse(raw) as Activity;
        setNodes(topologyToNodes(topology, activity));
      } catch {
        // Same honest no-op as the topology poll above.
      }
    }
    poll();
    const id = setInterval(poll, ACTIVITY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [setNodes]);

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
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => statusColor((n.data as AgentNodeData)?.status ?? "idle")}
        />
      </ReactFlow>
    </div>
  );
}

export default App;
