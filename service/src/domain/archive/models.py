from dataclasses import dataclass


class ArchiveFormatError(ValueError):
    """Raised when a file's content does not match its declared format."""


class ScanLimitError(Exception):
    """Raised when a directory scan exceeds its file cap or deadline."""

    def __init__(self, message: str, reason: str = "max_files") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class LineRecord:
    line_no: int
    text: str
    member_path: str
    file_name: str
    truncated: bool = False
