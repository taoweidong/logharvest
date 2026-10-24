from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class KeywordMatch(BaseModel):
    type: Literal["keyword"] = "keyword"
    any: list[str] = Field(default_factory=list)
    all: list[str] = Field(default_factory=list)
    case_sensitive: bool = False

    @model_validator(mode="after")
    def validate_one_mode(self) -> "KeywordMatch":
        if bool(self.any) == bool(self.all):
            raise ValueError("exactly one of any/all must be provided")
        return self


class RegexMatch(BaseModel):
    type: Literal["regex"] = "regex"
    pattern: str
    flags: str = ""
    timeout_ms: int = 50


MatchSpec = Annotated[Union[KeywordMatch, RegexMatch], Field(discriminator="type")]


class Threshold(BaseModel):
    field: str
    op: Literal["gt", "gte", "lt", "lte", "eq"]
    value: Decimal


class Rule(BaseModel):
    id: str
    name: str = ""
    enabled: bool = True
    match: MatchSpec
    threshold: Threshold | None = None
    severity: str = "info"
    tags: list[str] = Field(default_factory=list)


class RuleSetConfig(BaseModel):
    name: str
    file_glob: str = "**/*"
    encoding: str = "utf-8"
    fallback_encoding: str = "gb18030"
    rules: list[Rule]

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "RuleSetConfig":
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self


class RuleMatch(BaseModel):
    rule_id: str
    severity: str
    tags: list[str]
    fields: dict[str, str | None]
    numeric_value: Decimal | None = None
    matched_text: str | None = None
