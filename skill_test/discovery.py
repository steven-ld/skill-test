"""
Skill 自动发现与组合 — 扫描仓库中的 SKILL.md 文件，支持多 Skill 合并。

功能：
- 自动扫描 .claude/skills/ 和 .cursor/skills/ 目录
- 支持 Skill 组合（多个 Skill 合并为一个）
- 预设 Skill 配置（从模板快速创建）
- Skill 元数据提取（name, description）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .log import get_logger
from .models import SkillConfig

log = get_logger("discovery")

_SKILL_DIRS = [
    ".claude/skills",
    ".cursor/skills",
    ".claude/commands",
]

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---", re.DOTALL
)


def _parse_frontmatter(content: str) -> dict[str, str]:
    """从 SKILL.md 提取 YAML frontmatter 中的字段。"""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def discover_skills(
    repo_path: str | Path,
    *,
    include_refs: bool = True,
    max_ref_size: int = 50_000,
) -> list[SkillConfig]:
    """
    扫描仓库中的所有 Skill 定义。

    Args:
        repo_path:    仓库根路径
        include_refs: 是否包含 references 子目录中的文件
        max_ref_size: 单个 reference 文件最大字节数

    Returns:
        发现的 SkillConfig 列表
    """
    root = Path(repo_path).resolve()
    skills: list[SkillConfig] = []
    seen_names: set[str] = set()

    for skill_dir_rel in _SKILL_DIRS:
        skill_dir = root / skill_dir_rel
        if not skill_dir.is_dir():
            continue

        for entry in sorted(skill_dir.iterdir()):
            if not entry.is_dir():
                continue

            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue

            content = skill_md.read_text(encoding="utf-8", errors="replace")
            meta = _parse_frontmatter(content)
            name = meta.get("name", entry.name)

            if name in seen_names:
                continue
            seen_names.add(name)

            ref_files: list[str] = []
            if include_refs:
                ref_dir = entry / "references"
                if ref_dir.is_dir():
                    for ref in sorted(ref_dir.glob("*.md")):
                        if ref.stat().st_size <= max_ref_size:
                            ref_files.append(f"references/{ref.name}")

            skills.append(SkillConfig(
                name=name,
                skill_file=str(skill_md),
                ref_files=ref_files,
            ))

            log.info(
                "发现 Skill: %s (%s, %d refs)",
                name, skill_dir_rel, len(ref_files),
            )

    log.info("共发现 %d 个 Skill", len(skills))
    return skills


def compose_skills(
    skills: list[SkillConfig],
    *,
    name: str | None = None,
    mode: str = "merge",
) -> SkillConfig:
    """
    将多个 Skill 合并为一个复合 Skill。

    Args:
        skills: 要合并的 Skill 列表
        name:   合并后的名称
        mode:   合并模式
                - "merge": 合并所有 system_prompt 和 skill_file
                - "chain": 按顺序串联，每个 Skill 作为独立段落

    Returns:
        合并后的 SkillConfig
    """
    if not skills:
        return SkillConfig(name="empty")

    if len(skills) == 1:
        return skills[0]

    combined_name = name or " + ".join(s.name for s in skills)
    parts: list[str] = []
    all_refs: list[str] = []

    for i, skill in enumerate(skills, 1):
        section_parts: list[str] = []

        if skill.skill_file:
            path = Path(skill.skill_file)
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="replace")
                section_parts.append(content)

                for ref in skill.ref_files:
                    ref_path = path.parent / ref
                    if ref_path.exists():
                        ref_content = ref_path.read_text(encoding="utf-8", errors="replace")
                        section_parts.append(f"### {ref_path.name}\n\n{ref_content}")

        if skill.system_prompt:
            section_parts.append(skill.system_prompt)

        if section_parts:
            if mode == "chain":
                parts.append(
                    f"## Skill {i}: {skill.name}\n\n"
                    + "\n\n".join(section_parts)
                )
            else:
                parts.extend(section_parts)

    separator = "\n\n---\n\n" if mode == "chain" else "\n\n"

    return SkillConfig(
        name=combined_name,
        system_prompt=separator.join(parts),
    )


# ── 预设 Skill ────────────────────────────────────────────────────────────

_PRESETS: dict[str, SkillConfig] = {
    "write-expert": SkillConfig(
        name="write-expert",
        system_prompt=(
            "你是专业的代码编写专家。"
            "要求：高质量、结构清晰、有类型标注、有注释、有单元测试。"
            "直接实现代码，不要解释。"
        ),
    ),
    "review-expert": SkillConfig(
        name="review-expert",
        system_prompt=(
            "你是专业的代码审查专家。"
            "关注：代码质量、安全隐患、性能问题、最佳实践。"
            "给出具体改进建议和代码示例。"
        ),
    ),
    "tdd-expert": SkillConfig(
        name="tdd-expert",
        system_prompt=(
            "你是 TDD 专家。严格按照 Red-Green-Refactor 流程："
            "1. 先写失败的测试 2. 实现最小代码通过测试 3. 重构保持测试通过。"
            "每个步骤都要生成实际代码。"
        ),
    ),
    "refactor-expert": SkillConfig(
        name="refactor-expert",
        system_prompt=(
            "你是代码重构专家。"
            "关注：消除重复、提取方法、简化条件、降低耦合。"
            "保持行为不变，只改善结构。每次重构说明理由。"
        ),
    ),
}


def get_preset(name: str) -> Optional[SkillConfig]:
    """获取预设 Skill。"""
    return _PRESETS.get(name)


def list_presets() -> dict[str, SkillConfig]:
    """列出所有预设 Skill。"""
    return _PRESETS.copy()
