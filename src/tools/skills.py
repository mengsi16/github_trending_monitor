"""Skills 加载工具

参考 learn-claude-code s05 的两层 Skill 注入模式：
- Layer 1: 启动时扫描 skills/*/SKILL.md，将 name/description 元信息注入 Prompt
- Layer 2: 模型调用 load_skill 时，按需加载完整 Skill 内容
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

_logger = logging.getLogger("skills_tools")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_SKILLS_DIR = _PROJECT_ROOT / "skills"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_MAX_SKILL_CONTENT_CHARS = 200000


class SkillLoader:
    """扫描并按需加载 SKILL.md。"""

    def __init__(self, skills_dir: Path | str | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else _DEFAULT_SKILLS_DIR
        self.skills: Dict[str, Dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """重新扫描 skills 目录。"""
        self.skills = {}
        if not self.skills_dir.exists():
            _logger.info("Skills directory does not exist: %s", self.skills_dir)
            return

        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception as e:
                _logger.warning("Failed to read skill file %s: %s", skill_file, e)
                continue

            meta, body = self._parse_frontmatter(text)
            name = str(meta.get("name") or skill_file.parent.name).strip()
            if not name:
                continue

            if name in self.skills:
                _logger.warning("Duplicate skill name '%s', keeping first: %s", name, self.skills[name].get("path"))
                continue

            self.skills[name] = {
                "name": name,
                "meta": meta,
                "body": body,
                "path": skill_file,
            }

    @staticmethod
    def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
        """解析 YAML frontmatter。"""
        text = text.lstrip("\ufeff")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}, text.strip()

        raw_meta, body = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(raw_meta) or {}
            if not isinstance(meta, dict):
                meta = {}
        except yaml.YAMLError as e:
            _logger.warning("Invalid skill frontmatter: %s", e)
            meta = {}
        return meta, body.strip()

    def list_skills(self) -> List[Dict[str, str]]:
        """返回所有 Skill 元信息。"""
        items = []
        for name, skill in sorted(self.skills.items()):
            meta = skill.get("meta", {})
            items.append({
                "name": name,
                "description": str(meta.get("description", "No description")),
                "path": str(skill.get("path", "")),
            })
        return items

    def get_descriptions(self) -> str:
        """生成适合注入 system prompt 的 Layer 1 元信息。"""
        if not self.skills:
            return "(no skills available)"

        lines = []
        for item in self.list_skills():
            lines.append(f"- {item['name']}: {item['description']}")
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """返回指定 Skill 的完整 body。"""
        skill_name = (name or "").strip()
        skill = self.skills.get(skill_name)
        if not skill:
            available = ", ".join(sorted(self.skills.keys())) or "(none)"
            return f"Error: Unknown skill '{skill_name}'. Available skills: {available}"

        meta = skill.get("meta", {})
        if meta.get("external-skill") is True:
            body = self._get_external_content(meta)
            return f"<skill name=\"{skill_name}\">\n{body}\n</skill>"

        body = str(skill.get("body", ""))
        if len(body) > _MAX_SKILL_CONTENT_CHARS:
            body = body[:_MAX_SKILL_CONTENT_CHARS] + "\n\n... [skill content truncated]"

        return f"<skill name=\"{skill_name}\">\n{body}\n</skill>"

    @staticmethod
    def _get_external_content(meta: Dict[str, Any]) -> str:
        env_name = str(meta.get("source-env") or "BRAIN_BASE_PATH").strip()
        source_path = str(meta.get("source-path") or "").strip()
        if not source_path:
            return "Error: external skill missing source-path"

        relative_path = Path(source_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return f"Error: unsafe external skill source-path: {source_path}"

        root_value = os.environ.get(env_name, "").strip()
        if not root_value:
            return f"Error: {env_name} is not set"

        root = Path(root_value)
        skill_file = root / relative_path
        if not skill_file.exists():
            return f"Error: external skill file not found: {skill_file}"

        text = skill_file.read_text(encoding="utf-8")
        _, body = SkillLoader._parse_frontmatter(text)
        if len(body) > _MAX_SKILL_CONTENT_CHARS:
            body = body[:_MAX_SKILL_CONTENT_CHARS] + "\n\n... [skill content truncated]"
        return body


_SKILL_LOADER = SkillLoader()


def get_skill_descriptions() -> str:
    """供 Agent Prompt 注入 Layer 1 Skill 元信息。"""
    return _SKILL_LOADER.get_descriptions()


def tool_load_skill(name: str) -> str:
    """按需加载指定 Skill 的完整内容。"""
    return _SKILL_LOADER.get_content(name)


def tool_list_skills() -> str:
    """列出当前已发现的 Skills。"""
    items = _SKILL_LOADER.list_skills()
    if not items:
        return "当前没有发现可用 Skills"

    lines = [f"发现 {len(items)} 个 Skills:"]
    for item in items:
        lines.append(f"- {item['name']}: {item['description']}")
    return "\n".join(lines)


SKILLS_TOOLS = [
    {
        "name": "load_skill",
        "description": (
            "按名称加载一个 Skill 的完整指令内容。"
            "当任务匹配某个 Skill 的描述时，先调用该工具读取完整 Skill，再按 Skill 指令执行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要加载的 Skill 名称"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_skills",
        "description": "列出当前已发现的 Skills 及其描述。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

SKILLS_HANDLERS = {
    "load_skill": tool_load_skill,
    "list_skills": tool_list_skills,
}
