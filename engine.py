import uuid
import networkx as nx
from typing import Dict, Optional, List
from models import Entity, Relationship, ActionRequest, ActionLog

class OntologyEngine:
    def __init__(self):
        self.graph = nx.DiGraph()
        self.entities: Dict[str, Entity] = {}
        self.audit_log: List[ActionLog] = []

    def upsert_entity(self, entity: Entity):
        self.entities[entity.id] = entity
        self.graph.add_node(entity.id, **entity.model_dump())

    def add_relationship(self, rel: Relationship):
        if rel.source_id not in self.entities or rel.target_id not in self.entities:
            raise ValueError("Both source and target entities must exist.")
        self.graph.add_edge(rel.source_id, rel.target_id, relation=rel.relation_type)

    def execute_action(self, req: ActionRequest) -> ActionLog:
        if req.target_entity_id not in self.entities:
            raise KeyError(f"Entity '{req.target_entity_id}' not found.")

        entity = self.entities[req.target_entity_id]
        prev_status = entity.status

        if req.action_name == "ground_asset":
            entity.status = "GROUNDED"
            entity.properties["grounding_reason"] = req.parameters.get("reason", "Routine inspection")

        elif req.action_name == "replace_part":
            new_part_id = req.parameters.get("new_part_id")
            if not new_part_id or new_part_id not in self.entities:
                raise ValueError("Valid replacement part ID required.")

            edges_to_remove = [(u, v) for u, v in self.graph.out_edges(entity.id)]
            self.graph.remove_edges_from(edges_to_remove)
            self.graph.add_edge(entity.id, new_part_id, relation="HAS_INSTALLED_PART")
            entity.status = "OPERATIONAL"

        else:
            raise NotImplementedError(f"Action '{req.action_name}' is not permitted.")

        log_entry = ActionLog(
            id=str(uuid.uuid4())[:8],
            action_name=req.action_name,
            target_entity_id=entity.id,
            previous_status=prev_status,
            new_status=entity.status
        )
        self.audit_log.append(log_entry)
        return log_entry
