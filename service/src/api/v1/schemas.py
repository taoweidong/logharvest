from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.domain.rules.models import RuleSetConfig


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    read_only: bool = True
    copy_to_local: bool = True
    description: str = ""


class DataSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    type: str
    root_path: str
    read_only: bool
    copy_to_local: bool
    enabled: bool
    description: str
    created_at: datetime


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1)
    config: RuleSetConfig


class RuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    version: int
    created_at: datetime
