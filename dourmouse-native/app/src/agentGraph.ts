// dourmouse-native — Vision OS checklist item 7 ("visual agent swarm
// graph"). Pure data-shaping functions, kept separate from App.tsx's
// rendering/wiring so they're directly unit-testable later even before
// a JS test runner is configured for this project (same "extract the
// pure function" discipline ui/workspace.html's computeGazeState/
// handPinchState already use, just in TypeScript here).
//
// NOT a LangGraph orchestrator swap — see src-tauri/src/lib.rs's own
// architecture comment. This only VISUALIZES the real, already-live
// topology (dourmouse/webui.py's build_link_topology(), served at
// /api/links) and live per-agent activity (ActivityTracker.snapshot(),
// served at /api/activity) — dispatch.py's own orchestration loop is
// completely untouched.

import type { Edge, Node } from "@xyflow/react";

export interface TopologyNode {
  name: string;
  domain: string;
  description: string;
  tool_count: number;
}

export interface TopologyEdge {
  source: string;
  target: string;
  kind: "delegate" | "memory" | "peer" | string;
}

export interface Topology {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

export type AgentStatus = "idle" | "computing" | "auth" | string;

export interface ActivityEntry {
  status: AgentStatus;
  last: { tool?: string; args?: string; result?: string; at?: string } | null;
  feed: unknown[];
}

export interface Activity {
  agents: Record<string, ActivityEntry>;
}

export interface AgentNodeData extends Record<string, unknown> {
  name: string;
  domain: string;
  description: string;
  toolCount: number;
  status: AgentStatus;
  lastTool: string;
}

/** Deterministic circular layout: `centerName` (if present in `names`)
 * sits at the origin, every other node is placed evenly around a circle
 * of `radius`. Pure — no DOM/window reference — so this can run in a
 * plain unit test with fabricated agent-name lists.
 */
export function computeCircleLayout(
  names: string[],
  centerName: string,
  radius = 340,
): Record<string, { x: number; y: number }> {
  const positions: Record<string, { x: number; y: number }> = {};
  const ring = names.filter((n) => n !== centerName);
  if (names.includes(centerName)) {
    positions[centerName] = { x: 0, y: 0 };
  }
  const n = ring.length;
  ring.forEach((name, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n) - Math.PI / 2;
    positions[name] = {
      x: Math.round(Math.cos(angle) * radius),
      y: Math.round(Math.sin(angle) * radius),
    };
  });
  return positions;
}

/** Color for a node's live status ring/glow. Pure lookup, defaults to a
 * calm neutral for any status this hasn't seen before (never throws on
 * an unrecognized value — the server's own real vocabulary today is
 * idle/computing/auth, but this must not crash the UI if that grows). */
export function statusColor(status: AgentStatus): string {
  switch (status) {
    case "computing":
      return "#f2b73d"; // amber — matches ui/workspace.html's --amber
    case "auth":
      return "#e0685c"; // bad/red — awaiting a real confirmation
    case "idle":
      return "#3a5068"; // dim — matches --blue-dim family
    default:
      return "#3a5068";
  }
}

/** Edge stroke styling by real relationship kind (delegate/memory/peer —
 * see build_link_topology's own docstring for what each means). Pure. */
export function edgeStyleFor(kind: string): { stroke: string; strokeDasharray?: string; strokeWidth: number } {
  switch (kind) {
    case "delegate":
      return { stroke: "#f2b73d", strokeWidth: 1.6 };
    case "memory":
      return { stroke: "#7ea9c9", strokeDasharray: "4 3", strokeWidth: 1.2 };
    case "peer":
      return { stroke: "#2a3a4c", strokeWidth: 0.8 };
    default:
      return { stroke: "#2a3a4c", strokeWidth: 0.8 };
  }
}

/** Build React Flow nodes from real topology, optionally overlaid with
 * real live activity (status/last-tool). `activity` is optional so the
 * graph still renders (idle-everywhere) from topology alone before the
 * first activity poll lands. */
export function topologyToNodes(topology: Topology, activity?: Activity | null): Node<AgentNodeData>[] {
  const names = topology.nodes.map((n) => n.name);
  const center = names.includes("orchestrator") ? "orchestrator" : names[0] ?? "";
  const positions = computeCircleLayout(names, center);
  return topology.nodes.map((n) => {
    const live = activity?.agents?.[n.name];
    const status = live?.status ?? "idle";
    const color = statusColor(status);
    return {
      id: n.name,
      type: "default",
      position: positions[n.name] ?? { x: 0, y: 0 },
      data: {
        name: n.name,
        domain: n.domain,
        description: n.description,
        toolCount: n.tool_count,
        status,
        lastTool: live?.last?.tool ?? "",
        label: n.name.toUpperCase() + (status === "computing" ? " ●" : ""),
      },
      // Real visual status, not just in the minimap: a computing/auth
      // node's border glows in its status color; idle nodes stay dim.
      style: {
        borderColor: color,
        borderWidth: status === "idle" ? 1 : 2,
        boxShadow: status === "computing" ? `0 0 12px ${color}` : "none",
        background: "#0d1622",
        color: "#dce8f2",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
      },
    };
  });
}

/** Build React Flow edges from real topology edges. Pure, no dedup logic
 * beyond what the server already guarantees (build_link_topology emits
 * each undirected peer pair once). */
export function topologyToEdges(topology: Topology): Edge[] {
  return topology.edges.map((e, i) => ({
    id: `e-${e.kind}-${e.source}-${e.target}-${i}`,
    source: e.source,
    target: e.target,
    style: edgeStyleFor(e.kind),
    animated: e.kind === "delegate",
  }));
}
