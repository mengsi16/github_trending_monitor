"""8层提示词构建 (s06)"""
from typing import List, Dict, Optional

def build_system_prompt(
    mode: str = "full",
    bootstrap: Dict[str, str] = None,
    skills_block: str = "",
    memory_context: str = "",
    agent_id: str = "main",
    channel: str = "cli",
) -> str:
    """
    构建系统提示词 (s06: 8层组装)
    """
    sections = []
    bootstrap = bootstrap or {}

    # Layer 1: Identity
    identity = bootstrap.get("IDENTITY.md", "").strip()
    sections.append(identity if identity else "You are a helpful AI assistant.")

    # Layer 2: Soul
    if mode == "full":
        soul = bootstrap.get("SOUL.md", "").strip()
        if soul:
            sections.append(f"## Personality\n\n{soul}")

    # Layer 3: Tools guidance
    tools_md = bootstrap.get("TOOLS.md", "").strip()
    if tools_md:
        sections.append(f"## Tool Usage Guidelines\n\n{tools_md}")

    # Layer 4: Skills
    if mode == "full" and skills_block:
        sections.append(skills_block)

    # Layer 5: Memory
    if mode == "full" and memory_context:
        sections.append(f"## Relevant Context\n\n{memory_context}")

    # Layer 6: Bootstrap
    for name, content in bootstrap.items():
        if name not in ("IDENTITY.md", "SOUL.md", "TOOLS.md") and content.strip():
            sections.append(f"## {name.replace('.md', '')}\n\n{content.strip()}")

    # Layer 7: Runtime context
    sections.append(f"## Runtime\n\nAgent ID: {agent_id}")

    # Layer 8: Channel hints
    if channel:
        hints = {
            "cli": "You are responding via command line.",
            "feishu": "You are responding via Feishu.",
            "email": "You are responding via email.",
        }
        if channel in hints:
            sections.append(f"## Channel\n\n{hints[channel]}")

    return "\n\n".join(sections)
