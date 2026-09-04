import json
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, List
import networkx as nx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Foundry AIP Core")

# --- DATA MODELS ---
class Entity(BaseModel):
    id: str
    type: str
    status: str
    cost_per_hour_downtime: float = 0.0
    properties: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class Relationship(BaseModel):
    source_id: str
    target_id: str
    relation_type: str

class ActionPayload(BaseModel):
    action_type: str
    target_id: str
    parameters: Dict[str, Any] = Field(default_factory=dict)

class QueryPayload(BaseModel):
    prompt: str

# --- ENGINE STATE ---
graph = nx.DiGraph()
entities: Dict[str, Entity] = {}
audit_trail: List[Dict[str, Any]] = []

def seed_baseline():
    global graph, entities, audit_trail
    graph.clear()
    entities.clear()
    audit_trail.clear()

    initial_entities = [
        Entity(
            id="FLIGHT-802",
            type="Mission",
            status="CRITICAL_RISK",
            cost_per_hour_downtime=18500.0,
            properties={"Route": "JFK → LHR", "Passengers": 178, "Departure": "18:30 UTC", "Priority": "Tier 1"}
        ),
        Entity(
            id="TAIL-787",
            type="Airframe",
            status="GROUND_IMPACT",
            cost_per_hour_downtime=12000.0,
            properties={"Model": "Boeing 787-9", "Total Hours": 4210, "Location": "Hangar 2, JFK"}
        ),
        Entity(
            id="TURBINE-01",
            type="Subsystem",
            status="CRITICAL",
            cost_per_hour_downtime=0.0,
            properties={"Vibration": "8.8 mm/s (Max 4.0)", "Core Temp": "780°C (Limit 710°C)", "Telemetry": "Exceeded"}
        ),
        Entity(
            id="TURBINE-02",
            type="Subsystem",
            status="OPERATIONAL",
            cost_per_hour_downtime=0.0,
            properties={"Vibration": "1.1 mm/s", "Core Temp": "520°C", "Telemetry": "Nominal"}
        ),
        Entity(
            id="SPARE-TURB-99",
            type="InventoryAsset",
            status="READY",
            cost_per_hour_downtime=0.0,
            properties={"Location": "Depot Bay 4", "Condition": "Certified", "Lead Time": "35 min"}
        ),
        Entity(
            id="CREW-BRAVO",
            type="SupportUnit",
            status="ON_DUTY",
            cost_per_hour_downtime=0.0,
            properties={"Lead": "Chief Vance", "Size": "5 Technicians", "Shift": "Alpha"}
        )
    ]

    for ent in initial_entities:
        entities[ent.id] = ent
        graph.add_node(ent.id, **ent.model_dump())

    graph.add_edge("FLIGHT-802", "TAIL-787", relation="ASSIGNED_TO")
    graph.add_edge("TAIL-787", "TURBINE-01", relation="PORT_ENGINE")
    graph.add_edge("TAIL-787", "TURBINE-02", relation="STBD_ENGINE")
    graph.add_edge("CREW-BRAVO", "TAIL-787", relation="STAGED_AT")

    audit_trail.append({
        "id": "init-01",
        "action": "SYSTEM_BOOTSTRAP",
        "target": "FLIGHT-802",
        "prev": "SYSTEM_OFFLINE",
        "curr": "ACTIVE_MONITORING",
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    })

seed_baseline()

def compute_impact(target_id: str) -> Dict[str, Any]:
    upstream = list(nx.ancestors(graph, target_id))
    exposure = sum(entities[n].cost_per_hour_downtime for n in upstream if n in entities)
    return {"affected_nodes": upstream, "hourly_cost": exposure}

def query_ollama(prompt: str) -> str:
    body = json.dumps({
        "model": "qwen2.5-coder:latest",
        "prompt": prompt,
        "stream": False
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            return res_data.get("response", "").strip()
    except Exception:
        return ""

# --- API ROUTES ---
@app.get("/api/ontology")
def get_ontology():
    nodes = []
    for nid in graph.nodes:
        ent = entities.get(nid)
        impact = compute_impact(nid) if ent and ("CRIT" in ent.status or "IMPACT" in ent.status) else {"affected_nodes": [], "hourly_cost": 0}
        nodes.append({
            "id": nid,
            "type": ent.type if ent else "Asset",
            "status": ent.status if ent else "NOMINAL",
            "properties": ent.properties if ent else {},
            "cost_downtime": ent.cost_per_hour_downtime if ent else 0.0,
            "impact": impact
        })
    edges = [{"from": u, "to": v, "label": d.get("relation", "")} for u, v, d in graph.edges(data=True)]
    return {"nodes": nodes, "edges": edges, "audit_trail": audit_trail}

@app.post("/api/reset")
def reset_system():
    seed_baseline()
    return {"status": "reset_complete"}

@app.post("/api/actions/execute")
def execute_action(act: ActionPayload):
    if act.target_id not in entities:
        raise HTTPException(status_code=404, detail="Entity missing")
    
    ent = entities[act.target_id]
    prev_status = ent.status

    if act.action_type == "HOT_SWAP_TURBINE":
        spare_id = act.parameters.get("spare_id", "SPARE-TURB-99")
        parent_id = act.parameters.get("parent_id", "TAIL-787")

        if graph.has_edge(parent_id, act.target_id):
            graph.remove_edge(parent_id, act.target_id)
        graph.add_edge(parent_id, spare_id, relation="PORT_ENGINE")

        ent.status = "QUARANTINED"
        if spare_id in entities:
            entities[spare_id].status = "MOUNTED_ACTIVE"
        if parent_id in entities:
            entities[parent_id].status = "AIRWORTHY"
        if "FLIGHT-802" in entities:
            entities["FLIGHT-802"].status = "ON_TIME"

    elif act.action_type == "UPDATE_STATUS":
        ent.status = act.parameters.get("new_status", "UPDATED")

    log_entry = {
        "id": str(uuid.uuid4())[:8],
        "action": act.action_type,
        "target": act.target_id,
        "prev": prev_status,
        "curr": ent.status,
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    }
    audit_trail.insert(0, log_entry)
    return {"status": "success", "log": log_entry}

@app.post("/api/aip/diagnose")
def diagnose_agent(query: QueryPayload):
    time_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    
    prompt_text = (
        f"You are Palantir AIP Operational Intelligence. System entities: {list(entities.keys())}.\n"
        f"TURBINE-01 vibration is 8.8mm/s, threatening FLIGHT-802 ($18,500/hr exposure). SPARE-TURB-99 is staged.\n"
        f"User query: '{query.prompt}'.\n"
        f"Give a professional 2-sentence operational mitigation plan."
    )
    
    response = query_ollama(prompt_text)
    if not response:
        response = (
            f"Vibration telemetry on TURBINE-01 exceeds safety envelope by 120%, jeopardizing FLIGHT-802 ($18,500/hr risk). "
            f"Recommend immediate automated swap with SPARE-TURB-99 to clear TAIL-787 for scheduled 18:30Z departure."
        )

    return {
        "analysis": response,
        "timestamp": time_str,
        "source": "Local Ollama (qwen2.5-coder)" if query_ollama("test") else "Deterministic Safety Policy"
    }

# --- HIGH-CONTRAST ENTERPRISE CONSOLE ---
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ontology OS | Enterprise Action Platform</title>
        <script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.2/dist/vis-network.min.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-base: #090c10;
                --bg-panel: #0d1117;
                --bg-surface: #161b22;
                --bg-card: #21262d;
                --border: #30363d;
                --border-subtle: #21262d;
                --text-main: #f0f6fc;
                --text-muted: #8b949e;
                --text-soft: #c9d1d9;
                --accent-blue: #388bfd;
                --accent-cyan: #58a6ff;
                --danger-bg: #ffebe9;
                --danger: #f85149;
                --success: #3fb950;
                --warning: #d29922;
            }

            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                background: var(--bg-base);
                color: var(--text-main);
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
                height: 100vh;
                display: flex;
                flex-direction: column;
                overflow: hidden;
                font-size: 14px;
                line-height: 1.5;
            }

            /* TOP BAR */
            header {
                height: 52px;
                background: var(--bg-panel);
                border-bottom: 1px solid var(--border);
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 0 20px;
            }
            .brand { display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 14px; letter-spacing: 0.5px; }
            .brand-badge {
                font-family: 'JetBrains Mono', monospace;
                font-size: 11px;
                background: #1f2a3c;
                color: var(--accent-cyan);
                border: 1px solid #388bfd44;
                padding: 3px 8px;
                border-radius: 4px;
                font-weight: 600;
            }

            .header-actions { display: flex; align-items: center; gap: 10px; }
            .btn {
                background: var(--bg-surface);
                border: 1px solid var(--border);
                color: var(--text-main);
                padding: 6px 12px;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 500;
                cursor: pointer;
                display: inline-flex;
                align-items: center;
                gap: 6px;
                transition: all 0.15s ease;
            }
            .btn:hover { background: var(--bg-card); border-color: #8b949e; }
            .btn-primary { background: #238636; border-color: #2ea043; color: #fff; font-weight: 600; }
            .btn-primary:hover { background: #2ea043; border-color: #3fb950; }
            .btn-danger { background: #da3633; border-color: #f85149; color: #fff; font-weight: 600; }
            .btn-danger:hover { background: #f85149; }

            /* MAIN LAYOUT */
            #workspace { display: flex; flex: 1; height: calc(100vh - 52px); }

            /* SIDEBAR / AUDIT */
            #sidebar-left {
                width: 320px;
                background: var(--bg-panel);
                border-right: 1px solid var(--border);
                display: flex;
                flex-direction: column;
            }
            .panel-header {
                padding: 12px 16px;
                font-size: 12px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.8px;
                color: var(--text-muted);
                border-bottom: 1px solid var(--border);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            #audit-stream { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
            .audit-card {
                background: var(--bg-surface);
                border: 1px solid var(--border);
                border-left: 4px solid var(--accent-blue);
                padding: 10px 12px;
                border-radius: 6px;
            }
            .audit-card.CRITICAL { border-left-color: var(--danger); }
            .audit-card.RESOLVED { border-left-color: var(--success); }
            .audit-meta { display: flex; justify-content: space-between; font-size: 12px; font-weight: 600; margin-bottom: 4px; }
            .audit-time { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-muted); }

            /* CENTER CANVAS */
            #canvas-wrapper {
                flex: 1;
                position: relative;
                background-color: #0b0e14;
                background-image: radial-gradient(circle, #21262d 1.5px, transparent 1.5px);
                background-size: 24px 24px;
            }
            #network { width: 100%; height: 100%; }

            .canvas-hud {
                position: absolute;
                top: 16px;
                left: 20px;
                display: flex;
                gap: 12px;
                pointer-events: none;
            }
            .hud-pill {
                background: rgba(13, 17, 23, 0.9);
                border: 1px solid var(--border);
                backdrop-filter: blur(8px);
                padding: 6px 14px;
                border-radius: 6px;
                font-size: 12px;
                font-family: 'JetBrains Mono', monospace;
                color: var(--text-soft);
            }
            .hud-pill strong { color: var(--accent-cyan); }

            /* RIGHT INSPECTOR & AIP */
            #sidebar-right {
                width: 380px;
                background: var(--bg-panel);
                border-left: 1px solid var(--border);
                display: flex;
                flex-direction: column;
                overflow-y: auto;
            }
            .sidebar-section { padding: 16px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 12px; }

            /* AIP REASONING CARD */
            .aip-card {
                background: linear-gradient(180deg, #161f30 0%, #101520 100%);
                border: 1px solid #2f456e;
                border-radius: 8px;
                padding: 14px;
            }
            .aip-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
            .aip-badge { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 700; color: var(--accent-cyan); }
            .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent-cyan); box-shadow: 0 0 8px var(--accent-cyan); }
            .aip-body { font-size: 13px; color: var(--text-soft); line-height: 1.6; margin-bottom: 12px; }

            .input-row { display: flex; gap: 8px; }
            .input-field {
                flex: 1;
                background: var(--bg-base);
                border: 1px solid var(--border);
                padding: 8px 12px;
                border-radius: 6px;
                color: var(--text-main);
                font-size: 13px;
                outline: none;
            }
            .input-field:focus { border-color: var(--accent-blue); }

            /* INSPECTOR CARDS */
            .node-card {
                background: var(--bg-surface);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 14px;
            }
            .status-pill {
                display: inline-block;
                padding: 2px 8px;
                border-radius: 4px;
                font-size: 11px;
                font-family: 'JetBrains Mono', monospace;
                font-weight: 700;
            }
            .status-CRITICAL, .status-CRITICAL_RISK { background: rgba(248, 81, 73, 0.15); color: var(--danger); border: 1px solid rgba(248, 81, 73, 0.4); }
            .status-OPERATIONAL, .status-READY, .status-ON_TIME, .status-AIRWORTHY { background: rgba(63, 185, 80, 0.15); color: var(--success); border: 1px solid rgba(63, 185, 80, 0.4); }
            .status-GROUND_IMPACT { background: rgba(210, 153, 34, 0.15); color: var(--warning); border: 1px solid rgba(210, 153, 34, 0.4); }

            .prop-grid { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; font-size: 13px; }
            .prop-row { display: flex; justify-content: space-between; border-bottom: 1px solid var(--border-subtle); padding-bottom: 4px; }
            .prop-name { color: var(--text-muted); }
            .prop-val { font-family: 'JetBrains Mono', monospace; font-weight: 500; color: var(--text-main); }
        </style>
    </head>
    <body>
        <header>
            <div class="brand">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#58a6ff" stroke-width="2">
                    <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
                    <polyline points="2 17 12 22 22 17"></polyline>
                    <polyline points="2 12 12 17 22 12"></polyline>
                </svg>
                <span>FOUNDRY ONTOLOGY PLATFORM</span>
                <span class="brand-badge">AIP US-EAST</span>
            </div>
            <div class="header-actions">
                <button class="btn" onclick="triggerReset()">Reset Scenario</button>
                <button class="btn" onclick="exportData()">Export Graph JSON</button>
            </div>
        </header>

        <div id="workspace">
            <!-- AUDIT CHRONOLOGY -->
            <aside id="sidebar-left">
                <div class="panel-header">
                    <span>Audit Chronology</span>
                    <span style="font-family: 'JetBrains Mono'; font-size: 11px;">IMMUTABLE</span>
                </div>
                <div id="audit-stream"></div>
            </aside>

            <!-- MAIN GRAPH -->
            <main id="canvas-wrapper">
                <div class="canvas-hud">
                    <div class="hud-pill">TOPOLOGY: <strong id="hud-nodes">0 Nodes</strong></div>
                    <div class="hud-pill">RELATIONS: <strong id="hud-edges">0 Links</strong></div>
                </div>
                <div id="network"></div>
            </main>

            <!-- RIGHT CONTROL PANEL -->
            <aside id="sidebar-right">
                <!-- AIP CO-PILOT -->
                <div class="sidebar-section">
                    <div class="aip-card">
                        <div class="aip-header">
                            <span class="aip-badge">
                                <span class="pulse-dot"></span>
                                AIP DECISION ENGINE
                            </span>
                            <span id="aip-source" style="font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono';">Online</span>
                        </div>
                        <div id="aip-output" class="aip-body">
                            Analyzing operational dependencies across airframe and mission units...
                        </div>
                        <div class="input-row">
                            <input id="aip-input" class="input-field" placeholder="Ask AIP to evaluate operations..." value="Assess downstream delay penalties." />
                            <button id="aip-run-btn" class="btn btn-primary" onclick="requestAIP()">Ask</button>
                        </div>
                    </div>
                </div>

                <!-- SELECTED ENTITY SPEC -->
                <div class="sidebar-section">
                    <div class="panel-header" style="padding: 0 0 8px 0; border: none;">
                        <span>Entity Inspector</span>
                    </div>
                    <div id="inspector-content" class="node-card">
                        <div style="color: var(--text-muted); font-size: 13px;">Select any node on the canvas to inspect real-time properties and cascade exposure.</div>
                    </div>
                </div>

                <!-- GOVERNED ACTION DISPATCH -->
                <div class="sidebar-section">
                    <div class="panel-header" style="padding: 0 0 8px 0; border: none;">
                        <span>Governed Operational Orders</span>
                    </div>
                    <button class="btn btn-danger" style="width: 100%; justify-content: center; padding: 10px;" onclick="executeMitigation()">
                        DISPATCH: HOT-SWAP TURBINE-01
                    </button>
                    <p style="font-size: 12px; color: var(--text-muted); line-height: 1.4;">
                        Executes a deterministic state mutation: decouples degraded component, assigns reserve asset, and clears scheduled flights.
                    </p>
                </div>
            </aside>
        </div>

        <script>
            let network = null;
            let currentPayload = { nodes: [], edges: [], audit_trail: [] };

            async function syncState() {
                const res = await fetch('/api/ontology');
                currentPayload = await res.json();

                document.getElementById('hud-nodes').innerText = `${currentPayload.nodes.length} Nodes`;
                document.getElementById('hud-edges').innerText = `${currentPayload.edges.length} Links`;

                renderGraph(currentPayload.nodes, currentPayload.edges);
                renderAudit(currentPayload.audit_trail);
            }

            function renderGraph(nodesData, edgesData) {
                const container = document.getElementById('network');

                const nodes = new vis.DataSet(nodesData.map(n => {
                    let bg = '#161b22';
                    let border = '#30363d';
                    let textColor = '#f0f6fc';

                    if (n.status.includes('CRITICAL')) {
                        bg = '#2a1215'; border = '#f85149';
                    } else if (n.status.includes('OPERATIONAL') || n.status.includes('READY') || n.status.includes('ON_TIME') || n.status.includes('AIRWORTHY')) {
                        bg = '#0f2419'; border = '#3fb950';
                    } else if (n.status.includes('IMPACT')) {
                        bg = '#261c0c'; border = '#d29922';
                    }

                    return {
                        id: n.id,
                        label: `${n.id}\\n${n.type}`,
                        shape: 'box',
                        margin: 12,
                        color: {
                            background: bg,
                            border: border,
                            highlight: { background: bg, border: '#58a6ff' }
                        },
                        borderWidth: 2,
                        font: {
                            color: textColor,
                            face: 'JetBrains Mono',
                            size: 13,
                            bold: { color: textColor }
                        }
                    };
                }));

                const edges = new vis.DataSet(edgesData.map(e => ({
                    from: e.from,
                    to: e.to,
                    label: ` ${e.label} `,
                    arrows: 'to',
                    color: { color: '#30363d', highlight: '#58a6ff' },
                    font: {
                        color: '#8b949e',
                        face: 'JetBrains Mono',
                        size: 11,
                        background: '#0d1117'
                    },
                    smooth: { type: 'cubicBezier', roundness: 0.3 }
                })));

                network = new vis.Network(container, { nodes, edges }, {
                    physics: {
                        barnesHut: { gravitationalConstant: -2000, springLength: 160 }
                    }
                });

                network.on('click', (params) => {
                    if (params.nodes.length > 0) {
                        displayNodeDetails(params.nodes[0]);
                    }
                });
            }

            function displayNodeDetails(nodeId) {
                const node = currentPayload.nodes.find(n => n.id === nodeId);
                if (!node) return;

                let html = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <span style="font-family: 'JetBrains Mono'; font-weight: 700; font-size: 15px;">${node.id}</span>
                        <span class="status-pill status-${node.status}">${node.status}</span>
                    </div>
                    <div class="prop-grid">
                        <div class="prop-row">
                            <span class="prop-name">Classification:</span>
                            <span class="prop-val">${node.type}</span>
                        </div>
                        <div class="prop-row">
                            <span class="prop-name">Hourly Downtime Risk:</span>
                            <span class="prop-val" style="color: ${node.cost_downtime > 0 ? '#f85149' : '#8b949e'}; font-weight: bold;">
                                $${node.cost_downtime.toLocaleString()}/hr
                            </span>
                        </div>
                `;

                if (node.impact && node.impact.affected_nodes.length > 0) {
                    html += `
                        <div style="margin-top: 8px; padding: 8px; background: rgba(248, 81, 73, 0.1); border-left: 3px solid #f85149; border-radius: 4px;">
                            <strong style="color: #f85149; font-size: 12px;">CASCADE BLAST RADIUS:</strong>
                            <div style="color: #f0f6fc; font-size: 12px; margin-top: 2px;">
                                Impairs ${node.impact.affected_nodes.join(', ')} ($${node.impact.hourly_cost.toLocaleString()}/hr total risk)
                            </div>
                        </div>
                    `;
                }

                for (const [key, val] of Object.entries(node.properties)) {
                    html += `
                        <div class="prop-row">
                            <span class="prop-name">${key}:</span>
                            <span class="prop-val">${val}</span>
                        </div>
                    `;
                }

                html += `</div>`;
                document.getElementById('inspector-content').innerHTML = html;
            }

            function renderAudit(logs) {
                const stream = document.getElementById('audit-stream');
                stream.innerHTML = logs.map(l => {
                    let badgeClass = '';
                    if (l.action.includes('BOOTSTRAP')) badgeClass = 'RESOLVED';
                    if (l.curr && l.curr.includes('QUARANTINED')) badgeClass = 'CRITICAL';
                    if (l.action.includes('SWAP')) badgeClass = 'RESOLVED';

                    return `
                        <div class="audit-card ${badgeClass}">
                            <div class="audit-meta">
                                <span style="color: #f0f6fc;">${l.action}</span>
                                <span class="audit-time">${l.time}</span>
                            </div>
                            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 2px;">Target Asset: <strong style="color: #58a6ff;">${l.target}</strong></div>
                            <div style="font-size: 11px; font-family: 'JetBrains Mono'; color: #8b949e;">${l.prev} → <span style="color: #f0f6fc; font-weight: 600;">${l.curr}</span></div>
                        </div>
                    `;
                }).join('');
            }

            async function requestAIP() {
                const prompt = document.getElementById('aip-input').value.trim();
                const output = document.getElementById('aip-output');
                const btn = document.getElementById('aip-run-btn');

                btn.disabled = true;
                btn.innerText = 'Evaluating...';
                output.innerText = 'Transmitting graph state to inference engine...';

                try {
                    const res = await fetch('/api/aip/diagnose', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ prompt: prompt })
                    });
                    const data = await res.json();
                    output.innerText = data.analysis;
                    document.getElementById('aip-source').innerText = data.source;
                } catch (e) {
                    output.innerText = 'Error querying intelligence provider: ' + e.message;
                } finally {
                    btn.disabled = false;
                    btn.innerText = 'Ask';
                }
            }

            async function executeMitigation() {
                await fetch('/api/actions/execute', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        action_type: 'HOT_SWAP_TURBINE',
                        target_id: 'TURBINE-01',
                        parameters: { 'spare_id': 'SPARE-TURB-99', 'parent_id': 'TAIL-787' }
                    })
                });
                await syncState();
                displayNodeDetails('TAIL-787');
            }

            async function triggerReset() {
                await fetch('/api/reset', { method: 'POST' });
                await syncState();
                requestAIP();
                document.getElementById('inspector-content').innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">Scenario reset. Select a node to inspect.</div>';
            }

            function exportData() {
                const blob = new Blob([JSON.stringify(currentPayload, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `foundry-state-${Date.now()}.json`;
                a.click();
            }

            syncState();
            requestAIP();
        </script>
    </body>
    </html>
    """
