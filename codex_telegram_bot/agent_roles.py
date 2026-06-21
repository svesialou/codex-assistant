from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRole:
    name: str
    emoji: str
    description: str
    write_access: bool = False


AGENT_ROLES: dict[str, AgentRole] = {
    "PM": AgentRole(
        name="PM",
        emoji="🧠",
        description="Accepts tasks, chooses flow, and writes lifecycle events.",
    ),
    "Architect": AgentRole(
        name="Architect",
        emoji="🏗",
        description="Read-only planning and architecture review role.",
    ),
    "CodexDev": AgentRole(
        name="CodexDev",
        emoji="🛠",
        description="Main Codex execution flow; the only write-execution role.",
        write_access=True,
    ),
    "Reviewer": AgentRole(
        name="Reviewer",
        emoji="🔍",
        description="Post-run review label for future review flow.",
    ),
    "Safety": AgentRole(
        name="Safety",
        emoji="🔐",
        description="Git snapshots, warnings, protected paths, and safety events.",
    ),
    "Scribe": AgentRole(
        name="Scribe",
        emoji="📝",
        description="Future summaries and memory curation.",
    ),
    "System": AgentRole(
        name="System",
        emoji="⚙️",
        description="Runtime state and internal system events.",
    ),
}


def role_emoji(agent: str) -> str:
    role = AGENT_ROLES.get(agent)
    return role.emoji if role is not None else "•"
