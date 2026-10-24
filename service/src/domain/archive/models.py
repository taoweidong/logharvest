from dataclasses import dataclass


class ArchiveFormatError(ValueError):
    """Raised when a file's content does not match its declared format."""


@dataclass(frozen=True)
class LineRecord:
    line_no: int
    text: str
    member_path: str
    file_name: str
    truncated: bool = False
