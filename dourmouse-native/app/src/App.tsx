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
// - Items 2, 4, 8, 9 (semantic gravity clustering, Excalidraw scratchpad,
//   GUI automation, git time-travel) — see NATIVE_REWRITE_ROADMAP.md.
//
// v13.6 update: live status is now a REAL push, not just a poll. A real
// SSE client (src-tauri/src/lib.rs's start_activity_stream) reads
// dourmouse.webui's existing GET /api/events hub and forwards real
// "agent_activity" events here via Tauri's event system — the
// ACTIVITY_POLL_MS interval below is kept only as a slow resync safety
// net (e.g. the very first paint, before the stream connects, or if it
// ever silently misses an event), not the primary update path anymore.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
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
// v13.6: real push (see the file-header comment) is now primary; this is
// a slow resync safety net only, not the main update cadence anymore.
const ACTIVITY_POLL_MS = 10000;

function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<AgentNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [status, setStatus] = useState<string>("checking backend…");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  // Real topology fetched once (then re-polled slowly); kept in a ref so
  // the activity poll/push effects can rebuild node styling from it
  // without re-fetching /api/links.
  const topologyRef = useRef<Topology | null>(null);
  // Real, running merge of the last-known status per agent — the resync
  // poll REPLACES this wholesale (it's a full snapshot); each pushed SSE
  // delta only overwrites the agents it actually names, preserving every
  // other agent's last-known state (including `feed`, which the push
  // payload never carries — see webui.py's ActivityTracker._broadcast_
  // changed, a deliberately compact delta, not a full snapshot).
  const activityRef = useRef<Activity>({ agents: {} });

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
  // data + visual status ring in place. Now a slow RESYNC (the real
  // primary path is the SSE push effect below) — a full snapshot fetch
  // wholesale-replaces activityRef so any delta the push stream ever
  // missed (a dropped connection during its 3s backoff, for instance)
  // self-heals within one resync interval.
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const topology = topologyRef.current;
      if (!topology) return; // nothing to overlay onto yet
      try {
        const raw = await invoke<string>("fetch_agent_activity", { baseUrl: DOURMOUSE_BASE_URL });
        if (cancelled || raw.startsWith("ERROR:")) return;
        const activity = JSON.parse(raw) as Activity;
        activityRef.current = activity;
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

  // v13.6 — the real push path (item 7's own flagged gap, now closed):
  // a real SSE client in Rust (start_activity_stream) forwards real
  // "agent_activity" events from dourmouse.webui's existing /api/events
  // hub. Each event only names the agents that actually changed since
  // the last one (see webui.py's ActivityTracker._broadcast_changed),
  // so this merges into activityRef rather than replacing it wholesale.
  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;

    async function wire() {
      try {
        await invoke<boolean>("start_activity_stream", { baseUrl: DOURMOUSE_BASE_URL });
      } catch {
        // Honest no-op: the resync poll above keeps working regardless.
        return;
      }
      if (cancelled) return;
      unlisten = await listen<{ agents: Record<string, { status: string; last: unknown }> }>(
        "agent_activity",
        (event) => {
          const topology = topologyRef.current;
          if (!topology) return; // nothing to overlay onto yet
          const delta = event.payload?.agents ?? {};
          for (const [name, entry] of Object.entries(delta)) {
            const prevFeed = activityRef.current.agents[name]?.feed ?? [];
            activityRef.current.agents[name] = {
              status: entry.status as Activity["agents"][string]["status"],
              last: entry.last as Activity["agents"][string]["last"],
              feed: prevFeed,
            };
          }
          setNodes(topologyToNodes(topology, activityRef.current));
        },
      );
    }
    wire();
    return () => {
      cancelled = true;
      unlisten?.();
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
