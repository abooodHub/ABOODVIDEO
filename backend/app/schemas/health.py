"""Health Check Pydantic Response Schemas."""
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field


class ServiceHealthResponse(BaseModel):
    service: str
    status: str = Field(..., description="'healthy', 'degraded', or 'unhealthy'")
    details: Dict[str, Any] = Field(default_factory=dict)


class OverallHealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    services: Dict[str, str]
