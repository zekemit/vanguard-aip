from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone

class Entity(BaseModel):
    id: str
    type: str
    status: str
    properties: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Relationship(BaseModel):
    source_id: str
    target_id: str
    relation_type: str

class ActionRequest(BaseModel):
    action_name: str
    target_entity_id: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    executed_by: str = "operator"

class ActionLog(BaseModel):
    id: str
    action_name: str
    target_entity_id: str
    previous_status: str
    new_status: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
