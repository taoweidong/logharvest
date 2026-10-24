from decimal import Decimal
from typing import Annotated, Literal

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
    timeout_ms: int = Field(default=50, gt=0)


MatchSpec = Annotated[KeywordMatch | RegexMatch, Field(discriminator="type")]


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

    @model_validator(mode="after")
    def validate_threshold_requires_regex(self) -> "Rule":
        if self.threshold is not None and isinstance(self.match, KeywordMatch):
            raise ValueError(
                "threshold requires a regex match with named groups; "
                "keyword matches produce no fields"
            )
        return self


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
