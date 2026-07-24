from __future__ import annotations

import re
from dataclasses import dataclass


SECRET_RE = re.compile(
    r"("
    r"token|password|passwd|secret|api[_-]?key|private[_ -]?key|"
    r"credentials?|auth\.json|\.env\b|ssh-rsa|BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY"
    r")",
    re.IGNORECASE,
)
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.-]*(?:token|password|passwd|secret|api[_-]?key)[A-Za-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redacted_lines: int = 0
    redacted_values: int = 0

    @property
    def changed(self) -> bool:
        return self.redacted_lines > 0 or self.redacted_values > 0


class RedactionService:
    def redact_text(self, text: str, max_chars: int | None = None) -> RedactionResult:
        kept: list[str] = []
        redacted_lines = 0
        redacted_values = 0
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if SECRET_RE.search(line):
                redacted_lines += 1
                masked, count = ASSIGNMENT_SECRET_RE.subn(
                    lambda match: f"{match.group('key')}{match.group('sep')}[REDACTED]",
                    line,
                )
                if count:
                    redacted_values += count
                    kept.append(masked)
                else:
                    kept.append("[REDACTED: potential secret line]")
                continue
            kept.append(line)

        result = "\n".join(kept).strip()
        if max_chars is not None and len(result) > max_chars:
            result = result[:max_chars].rstrip() + "\n..."
        return RedactionResult(
            text=result,
            redacted_lines=redacted_lines,
            redacted_values=redacted_values,
        )

    def contains_secret(self, text: str) -> bool:
        return bool(SECRET_RE.search(text))
