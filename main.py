import json
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
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
    initial_entities = [
        Entity(id="FLIGHT-802", type="Mission", status="CRITICAL_RISK", cost_per_hour_downtime=18500.0, properties={"pax": 178, "route": "JFK -> LHR", "departure": "18:30Z"}),
        Entity(id="TAIL-787", type="Airframe", status="GROUND_IMPACT", cost_per_hour_downtime=12000.0, properties={"airframe": "B787-9", "flight_cycles": 4210, "hangar": "H-02"}),
        Entity(id="TURBINE-01", type="Component", status="CRITICAL", cost_per_hour_downtime=0.0, properties={"vibration_rms": "8.8 mm/s", "exhaust_temp": "780 C", "spec_limit": "710 C"}),
        Entity(id="TURBINE-02", type="Component", status="NOMINAL", cost_per_hour_downtime=0.0, properties={"vibration_rms": "1.1 mm/s", "exhaust_temp": "520 C", "spec_limit": "710 C"}),
        Entity(id="SPARE-TURB-99", type="Inventory", status="READY", cost_per_hour_downtime=0.0, properties={"location": "Logistics Depot A", "condition": "Overhauled", "lead_time_min": 35}),
        Entity(id="CREW-BRAVO", type="Personnel", status="STANDBY", cost_per_hour_downtime=0.0, properties={"lead": "Chief Vance", "technicians": 5, "shift": "First"})
    ]
    for ent in initial_entities:
        entities[ent.id] = ent
        graph.add_node(ent.id, **ent.model_dump())

    graph.add_edge("FLIGHT-802", "TAIL-787", relation="ASSIGNED_TO")
    graph.add_edge("TAIL-787", "TURBINE-01", relation="PROPULSION_PORT")
    graph.add_edge("TAIL-787", "TURBINE-02", relation="PROPULSION_STBD")
    graph.add_edge("CREW-BRAVO", "TAIL-787", relation="DISPATCHED_TO")

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
        with urllib.request.urlopen(req, timeout=10) as res:
            res_data = json.loads(res.read().decode("utf-8"))
            return res_data.get("response", "No analysis returned.")
    except Exception:
        return "[Local Ollama Inactive] Heuristic Fallback: Critical vibration detected on TURBINE-01. Recommend immediate replacement with SPARE-TURB-99 to avoid flight deferral penalties."

# --- API ROUTES ---
@app.get("/api/ontology")
def get_ontology():
    nodes = []
    for nid in graph.nodes:
        ent = entities.get(nid)
        impact = compute_impact(nid) if ent and "CRIT" in ent.status else {"affected_nodes": [], "hourly_cost": 0}
        nodes.append({
            "id": nid,
            "type": ent.type if ent else "Unknown",
            "status": ent.status if ent else "ACTIVE",
            "properties": ent.properties if ent else {},
            "cost_downtime": ent.cost_per_hour_downtime if ent else 0,
            "impact": impact
        })
    edges = [{"from": u, "to": v, "label": d.get("relation", "")} for u, v, d in graph.edges(data=True)]
    return {"nodes": nodes, "edges": edges, "audit_trail": audit_trail}

@app.post("/api/entities")
def create_entity(ent: Entity):
    entities[ent.id] = ent
    graph.add_node(ent.id, **ent.model_dump())
    audit_trail.insert(0, {"id": str(uuid.uuid4())[:8], "action": "ENTITY_CREATED", "target": ent.id, "time": datetime.now(timezone.utc).strftime("%H:%M:%S")})
    return ent

@app.post("/api/relationships")
def create_relationship(rel: Relationship):
    if rel.source_id not in entities or rel.target_id not in entities:
        raise HTTPException(status_code=400, detail="Entities must exist")
    graph.add_edge(rel.source_id, rel.target_id, relation=rel.relation_type)
    audit_trail.insert(0, {"id": str(uuid.uuid4())[:8], "action": "LINK_CREATED", "target": f"{rel.source_id}->{rel.target_id}", "time": datetime.now(timezone.utc).strftime("%H:%M:%S")})
    return {"status": "linked"}

@app.post("/api/actions/execute")
def execute_action(act: ActionPayload):
    if act.target_id not in entities:
        raise HTTPException(status_code=404, detail="Target not found")
    
    ent = entities[act.target_id]
    prev_status = ent.status

    if act.action_type == "HOT_SWAP_COMPONENT":
        spare_id = act.parameters.get("spare_id", "SPARE-TURB-99")
        parent_id = act.parameters.get("parent_id", "TAIL-787")
        if graph.has_edge(parent_id, act.target_id):
            graph.remove_edge(parent_id, act.target_id)
        graph.add_edge(parent_id, spare_id, relation="PROPULSION_PORT")
        ent.status = "QUARANTINED"
        entities[spare_id].status = "OPERATIONAL"
        entities[parent_id].status = "AIRWORTHY"
        entities["FLIGHT-802"].status = "ON_SCHEDULE"
    else:
        ent.status = act.parameters.get("new_status", "UPDATED")

    log_entry = {
        "id": str(uuid.uuid4())[:8],
        "action": act.action_type,
        "target": act.target_id,
        "prev": prev_status,
        "curr": ent.status,
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S")
    }
    audit_trail.insert(0, log_entry)
    return {"status": "success", "log": log_entry}

@app.post("/api/aip/diagnose")
def diagnose_agent(query: QueryPayload):
    full_prompt = (
        f"You are Palantir AIP Operational Intelligence. System State:\n"
        f"Entities: {list(entities.keys())}\n"
        f"Prompt from Operator: {query.prompt}\n"
        f"Provide a concise operational response (max 2 sentences) recommending deterministic actions."
    )
    analysis = query_ollama(full_prompt)
    return {"analysis": analysis}

# --- PALANTIR ENTERPRISE UI ---
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Foundry AIP | Enterprise Ontology Engine</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.2/dist/vis-network.min.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <script>
            tailwind.config = {
                theme: {
                    extend: {
                        colors: {
                            slateRoot: '#090d16',
                            slatePanel: '#0f172a',
                            slateCard: '#1e293b',
                            palantirBlue: '#2563eb',
                            palantirCyan: '#38bdf8'
                        },
                        fontFamily: {
                            sans: ['Inter', 'sans-serif'],
                            mono: ['JetBrains Mono', 'monospace']
                        }
                    }
                }
            }
        </script>
        <style>
            .grid-canvas {
                background-color: #090d16;
                background-image: radial-gradient(circle, #1e293b 1px, transparent 1px);
                background-size: 20px 20px;
            }
        </style>
    </head>
    <body class="bg-slateRoot text-slate-200 font-sans h-screen flex flex-col overflow-hidden select-none">

        <!-- GLOBAL HEADER -->
        <header class="h-12 border-b border-slate-800 bg-slatePanel px-4 flex items-center justify-between z-10">
            <div class="flex items-center gap-3">
                <div class="w-4 h-4 bg-palantirCyan rounded-sm"></div>
                <span class="font-bold tracking-wider text-xs uppercase text-slate-100">Foundry // Object Explorer</span>
                <span class="text-xs px-2 py-0.5 rounded bg-blue-950 text-blue-400 border border-blue-800 font-mono">GOVERNED PRODUCTION</span>
            </div>
            <div class="flex items-center gap-3">
                <input id="search-input" oninput="handleSearch(this.value)" placeholder="Filter entities..." class="bg-slate-900 border border-slate-700 text-xs px-2.5 py-1 rounded w-48 font-mono focus:border-palantirBlue outline-none"/>
                <button onclick="openModal('entity-modal')" class="bg-blue-600 hover:bg-blue-500 text-white text-xs px-3 py-1 rounded font-medium transition">+ Add Entity</button>
                <button onclick="exportJSON()" class="bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs px-3 py-1 rounded font-medium border border-slate-700">Export State</button>
            </div>
        </header>

        <!-- MAIN LAYOUT -->
        <div class="flex-1 flex overflow-hidden">

            <!-- LEFT AUDIT STREAM -->
            <aside class="w-72 border-r border-slate-800 bg-slatePanel flex flex-col">
                <div class="px-4 py-2.5 border-b border-slate-800 text-[10px] uppercase font-bold tracking-wider text-slate-500">Live Transaction Ledger</div>
                <div id="audit-feed" class="flex-1 p-3 overflow-y-auto space-y-2 font-mono text-xs"></div>
            </aside>

            <!-- CENTER GRAPH CANVAS -->
            <main class="flex-1 relative grid-canvas">
                <div id="canvas-container" class="w-full h-full"></div>
                <div class="absolute top-3 left-4 flex gap-2 pointer-events-none">
                    <div class="bg-slatePanel/90 backdrop-blur border border-slate-800 px-3 py-1.5 rounded text-xs font-mono">
                        NODES: <span id="metric-nodes" class="text-palantirCyan font-bold">0</span> | EDGES: <span id="metric-edges" class="text-palantirCyan font-bold">0</span>
                    </div>
                </div>
            </main>

            <!-- RIGHT AIP REASONING & INSPECTOR -->
            <aside class="w-96 border-l border-slate-800 bg-slatePanel flex flex-col overflow-y-auto">
                
                <!-- AIP COPILOT -->
                <div class="p-4 border-b border-slate-800 bg-gradient-to-b from-blue-950/20 to-transparent">
                    <div class="flex items-center gap-2 mb-2">
                        <span class="w-2 h-2 rounded-full bg-palantirCyan animate-pulse"></span>
                        <span class="text-xs font-bold text-palantirCyan tracking-wider uppercase">AIP Strategic Copilot</span>
                    </div>
                    <div id="aip-reasoning-text" class="text-xs text-slate-300 leading-relaxed font-sans mb-3 min-h-[48px]">
                        Analyzing live graph telemetry...
                    </div>
                    <div class="flex gap-2">
                        <input id="agent-prompt" placeholder="Ask AIP to evaluate operations..." class="flex-1 bg-slate-900 border border-slate-700 text-xs px-2 py-1 rounded outline-none font-mono focus:border-palantirBlue"/>
                        <button onclick="runAIPQuery()" class="bg-palantirBlue hover:bg-blue-600 text-white text-xs px-2.5 py-1 rounded font-medium">Run</button>
                    </div>
                </div>

                <!-- INSPECTOR & ACTIONS -->
                <div class="p-4 flex-1 space-y-4">
                    <div class="text-[10px] uppercase font-bold tracking-wider text-slate-500">Selected Node Specification</div>
                    <div id="inspector-body" class="bg-slateCard border border-slate-800 rounded p-3 text-xs text-slate-400">
                        Click on any node in the graph to inspect properties and upstream cascade exposure.
                    </div>

                    <div class="text-[10px] uppercase font-bold tracking-wider text-slate-500">Governed Execution Handlers</div>
                    <div class="space-y-2">
                        <button onclick="executeMitigation()" class="w-full bg-rose-600 hover:bg-rose-500 text-white font-medium py-2 px-3 rounded text-xs transition">
                            EXECUTE: HOT-SWAP TURBINE-01
                        </button>
                    </div>
                </div>
            </aside>
        </div>

        <!-- MODAL: ADD ENTITY -->
        <div id="entity-modal" class="hidden fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
            <div class="bg-slatePanel border border-slate-700 rounded-lg w-96 p-5 space-y-4 text-xs">
                <div class="font-bold text-sm text-slate-100 uppercase tracking-wider">Deploy New Entity</div>
                <div class="space-y-2">
                    <label class="block text-slate-400 font-mono">Entity ID</label>
                    <input id="new-ent-id" placeholder="e.g. GENERATOR-04" class="w-full bg-slateCard border border-slate-700 p-2 rounded text-slate-200 outline-none font-mono"/>
                    
                    <label class="block text-slate-400 font-mono">Type</label>
                    <input id="new-ent-type" placeholder="e.g. Subsystem" class="w-full bg-slateCard border border-slate-700 p-2 rounded text-slate-200 outline-none font-mono"/>
                    
                    <label class="block text-slate-400 font-mono">Initial Status</label>
                    <select id="new-ent-status" class="w-full bg-slateCard border border-slate-700 p-2 rounded text-slate-200 outline-none font-mono">
                        <option value="OPERATIONAL">OPERATIONAL</option>
                        <option value="CRITICAL">CRITICAL</option>
                        <option value="STANDBY">STANDBY</option>
                    </select>
                </div>
                <div class="flex justify-end gap-2 pt-2">
                    <button onclick="closeModal('entity-modal')" class="px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300">Cancel</button>
                    <button onclick="submitEntity()" class="px-3 py-1.5 rounded bg-palantirBlue hover:bg-blue-600 text-white font-medium">Commit Node</button>
                </div>
            </div>
        </div>

        <script>
            let network = null;
            let rawData = { nodes: [], edges: [], audit_trail: [] };
            let nodesDataSet = null;
            let edgesDataSet = null;

            async function loadOntology() {
                const res = await fetch('/api/ontology');
                rawData = await res.json();

                document.getElementById('metric-nodes').innerText = rawData.nodes.length;
                document.getElementById('metric-edges').innerText = rawData.edges.length;

                const nodeItems = rawData.nodes.map(n => {
                    let bg = '#1e293b';
                    let border = '#334155';
                    if (n.status === 'CRITICAL' || n.status === 'CRITICAL_RISK') { bg = '#450a0a'; border = '#ef4444'; }
                    else if (n.status === 'OPERATIONAL' || n.status === 'READY' || n.status === 'ON_SCHEDULE') { bg = '#064e3b'; border = '#10b981'; }

                    return {
                        id: n.id,
                        label: `${n.id}\\n[${n.type}]`,
                        shape: 'box',
                        margin: 10,
                        color: { background: bg, border: border, highlight: { background: bg, border: '#38bdf8' } },
                        font: { color: '#f8fafc', face: 'JetBrains Mono', size: 11 }
                    };
                });

                const edgeItems = rawData.edges.map(e => ({
                    from: e.from, to: e.to, label: e.label, arrows: 'to',
                    color: { color: '#334155' }, font: { color: '#64748b', size: 9, face: 'JetBrains Mono' }
                }));

                nodesDataSet = new vis.DataSet(nodeItems);
                edgesDataSet = new vis.DataSet(edgeItems);

                const container = document.getElementById('canvas-container');
                network = new vis.Network(container, { nodes: nodesDataSet, edges: edgesDataSet }, {
                    physics: { barnesHut: { springLength: 150, gravitationalConstant: -1200 } }
                });

                network.on('click', (params) => {
                    if (params.nodes.length > 0) inspectNode(params.nodes[0]);
                });

                renderAudit(rawData.audit_trail);
            }

            function inspectNode(id) {
                const node = rawData.nodes.find(n => n.id === id);
                if (!node) return;

                let html = `
                    <div class="flex justify-between items-center pb-2 border-b border-slate-700 mb-2">
                        <span class="font-mono font-bold text-slate-100">${node.id}</span>
                        <span class="px-2 py-0.5 text-[10px] font-mono rounded bg-slate-800 text-slate-300 border border-slate-700">${node.status}</span>
                    </div>
                    <div class="space-y-1.5 font-mono">
                        <div class="flex justify-between"><span class="text-slate-500">Type:</span><span class="text-slate-300">${node.type}</span></div>
                        <div class="flex justify-between"><span class="text-slate-500">Hourly Risk:</span><span class="text-rose-400 font-bold">$${node.cost_downtime}/hr</span></div>
                `;

                if (node.impact && node.impact.affected_nodes.length > 0) {
                    html += `<div class="mt-2 pt-2 border-t border-slate-700 text-rose-400 font-bold">Blast Radius: ${node.impact.affected_nodes.join(', ')} ($${node.impact.hourly_cost}/hr)</div>`;
                }

                for (const [k, v] of Object.entries(node.properties)) {
                    html += `<div class="flex justify-between text-[11px]"><span class="text-slate-500">${k}:</span><span class="text-slate-300">${v}</span></div>`;
                }
                html += `</div>`;
                document.getElementById('inspector-body').innerHTML = html;
            }

            function renderAudit(logs) {
                const feed = document.getElementById('audit-feed');
                feed.innerHTML = logs.map(l => `
                    <div class="p-2 bg-slateCard border-l-2 border-palantirBlue rounded text-[11px]">
                        <div class="flex justify-between font-bold text-slate-200">
                            <span>${l.action}</span>
                            <span class="text-slate-500 text-[9px]">${l.time}</span>
                        </div>
                        <div class="text-slate-400 mt-1">Target: <span class="text-palantirCyan">${l.target}</span></div>
                    </div>
                `).join('');
            }

            async function runAIPQuery() {
                const prompt = document.getElementById('agent-prompt').value || "Evaluate asset risks";
                document.getElementById('aip-reasoning-text').innerText = "Querying Ollama reasoning engine...";
                const res = await fetch('/api/aip/diagnose', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ prompt })
                });
                const data = await res.json();
                document.getElementById('aip-reasoning-text').innerText = data.analysis;
            }

            async function executeMitigation() {
                await fetch('/api/actions/execute', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        action_type: "HOT_SWAP_COMPONENT",
                        target_id: "TURBINE-01",
                        parameters: { "spare_id": "SPARE-TURB-99", "parent_id": "TAIL-787" }
                    })
                });
                loadOntology();
            }

            async function submitEntity() {
                const id = document.getElementById('new-ent-id').value;
                const type = document.getElementById('new-ent-type').value;
                const status = document.getElementById('new-ent-status').value;
                if (!id || !type) return;

                await fetch('/api/entities', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ id, type, status, properties: {} })
                });
                closeModal('entity-modal');
                loadOntology();
            }

            function handleSearch(val) {
                if (!nodesDataSet) return;
                const matches = rawData.nodes.filter(n => n.id.toLowerCase().includes(val.toLowerCase()));
                if (matches.length > 0) {
                    network.focus(matches[0].id, { scale: 1.2, animation: true });
                }
            }

            function exportJSON() {
                const blob = new Blob([JSON.stringify(rawData, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `ontology-export-${Date.now()}.json`;
                a.click();
            }

            function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
            function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

            loadOntology();
            runAIPQuery();
        </script>
    </body>
    </html>
    """
