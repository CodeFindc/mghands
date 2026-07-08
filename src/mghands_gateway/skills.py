import hashlib
import shutil
from pathlib import Path

from mghands_gateway.models import (
    InstalledSkillMetadata,
    ProjectSkillRecord,
    SkillCatalogItem,
    SkillSpec,
    validate_safe_name,
)

MAX_REQUIREMENTS_BYTES = 64 * 1024


class SkillManager:
    def __init__(self, shared_root: Path, workspace_mount_path: str = '/workspace'):
        self.shared_root = shared_root
        self.workspace_mount_path = workspace_mount_path.rstrip('/') or '/workspace'

    def catalog(self) -> list[SkillCatalogItem]:
        root = self.shared_root
        if not root.exists():
            return []
        items: list[SkillCatalogItem] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            try:
                name = validate_safe_name(child.name, 'skill name')
                source = self._safe_source_dir(name)
                self._validate_skill_dir(source)
                items.append(
                    SkillCatalogItem(
                        name=name,
                        valid=True,
                        metadata=self._metadata(source),
                    )
                )
            except Exception as exc:
                items.append(
                    SkillCatalogItem(
                        name=child.name,
                        valid=False,
                        error=str(exc),
                    )
                )
        return items

    def install(self, skill_name: str, project_id: str, workspace_dir: Path) -> ProjectSkillRecord:
        source = self._safe_source_dir(skill_name)
        self._validate_skill_dir(source)
        target = self._target_dir(workspace_dir, skill_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f'.{target.name}.tmp')
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(source, tmp, symlinks=False)
        if target.exists():
            shutil.rmtree(target)
        tmp.replace(target)
        return ProjectSkillRecord(
            project_id=project_id,
            skill_name=skill_name,
            source_fingerprint=self._fingerprint(source),
            metadata=self._metadata(source),
        )

    def build_skill_specs(self, workspace_dir: Path, records: list[ProjectSkillRecord]) -> list[SkillSpec]:
        specs: list[SkillSpec] = []
        for record in records:
            target = self._target_dir(workspace_dir, record.skill_name)
            skill_md = target / 'SKILL.md'
            if not skill_md.exists():
                continue
            container_dir = f'{self.workspace_mount_path}/.mghands/skills/{record.skill_name}'
            content = f'SKILL_DIR={container_dir}\n\n{skill_md.read_text(encoding="utf-8")}'
            specs.append(
                SkillSpec(
                    name=record.skill_name,
                    content=content,
                    triggers=record.metadata.triggers,
                )
            )
        return specs

    def _safe_source_dir(self, skill_name: str) -> Path:
        name = validate_safe_name(skill_name, 'skill name')
        root = self.shared_root.resolve()
        path = (root / name).resolve()
        if root != path and root not in path.parents:
            raise ValueError('skill path escapes shared skills root')
        return path

    def _target_dir(self, workspace_dir: Path, skill_name: str) -> Path:
        name = validate_safe_name(skill_name, 'skill name')
        root = (workspace_dir / '.mghands' / 'skills').resolve()
        target = (root / name).resolve()
        if root != target and root not in target.parents:
            raise ValueError('skill path escapes project workspace')
        return target

    def _validate_skill_dir(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError('skill not found')
        if not (path / 'SKILL.md').is_file():
            raise ValueError('skill is missing SKILL.md')
        for item in path.rglob('*'):
            if item.is_symlink():
                raise ValueError('skill symlinks are not allowed')

    def _metadata(self, path: Path) -> InstalledSkillMetadata:
        skill_md = path / 'SKILL.md'
        content = skill_md.read_text(encoding='utf-8') if skill_md.exists() else ''
        dependencies = self._dependencies(path)
        return InstalledSkillMetadata(
            requires_dependencies=bool(dependencies),
            dependency_manifest='requirements.txt' if dependencies else None,
            dependency_status='not_managed_by_gateway' if dependencies else None,
            dependency_note='Dependencies must be preinstalled by an administrator in the sandbox image.'
            if dependencies
            else None,
            dependencies=dependencies,
            description=self._frontmatter_value(content, 'description'),
            triggers=self._frontmatter_list(content, 'triggers'),
        )

    def _dependencies(self, path: Path) -> list[str]:
        requirements = path / 'requirements.txt'
        if not requirements.exists() or not requirements.is_file():
            return []
        if requirements.stat().st_size > MAX_REQUIREMENTS_BYTES:
            return ['<requirements.txt too large to display>']
        lines = []
        for line in requirements.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                lines.append(stripped)
        return lines

    def _frontmatter_value(self, content: str, key: str) -> str | None:
        for line in self._frontmatter_lines(content):
            prefix = f'{key}:'
            if line.startswith(prefix):
                return line[len(prefix) :].strip().strip('"\'') or None
        return None

    def _frontmatter_list(self, content: str, key: str) -> list[str]:
        value = self._frontmatter_value(content, key)
        if not value:
            return []
        if value.startswith('[') and value.endswith(']'):
            value = value[1:-1]
        return [item.strip().strip('"\'') for item in value.split(',') if item.strip()]

    def _frontmatter_lines(self, content: str) -> list[str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != '---':
            return []
        result = []
        for line in lines[1:]:
            if line.strip() == '---':
                break
            result.append(line.strip())
        return result

    def _fingerprint(self, path: Path) -> str:
        digest = hashlib.sha256()
        for item in sorted(path.rglob('*')):
            if item.is_dir():
                continue
            relative = item.relative_to(path).as_posix()
            digest.update(relative.encode('utf-8'))
            digest.update(str(item.stat().st_size).encode('ascii'))
            digest.update(item.read_bytes())
        return digest.hexdigest()
