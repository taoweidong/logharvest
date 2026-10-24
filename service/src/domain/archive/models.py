from dataclasses import dataclass


@dataclass(frozen=True)
class LineRecord:
    line_no: int
    text: str
    member_path: str
    file_name: str
    truncated: bool = False
