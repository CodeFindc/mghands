from pathlib import Path

from mghands_gateway.skills import SkillManager


def test_skill_catalog_detects_requirements_without_installing(tmp_path: Path) -> None:
    shared = tmp_path / 'shared'
    skill = shared / 'ppt-master'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\ndescription: PPT skill\ntriggers: [ppt, slides]\n---\nUse scripts.', encoding='utf-8')
    (skill / 'requirements.txt').write_text('python-pptx==1.0.2\n# ignored\n', encoding='utf-8')

    catalog = SkillManager(shared).catalog()

    assert len(catalog) == 1
    item = catalog[0]
    assert item.name == 'ppt-master'
    assert item.valid is True
    assert item.metadata.requires_dependencies is True
    assert item.metadata.dependency_status == 'not_managed_by_gateway'
    assert item.metadata.dependencies == ['python-pptx==1.0.2']


def test_skill_install_copies_snapshot_and_builds_workspace_skill_spec(tmp_path: Path) -> None:
    shared = tmp_path / 'shared'
    skill = shared / 'ppt-master'
    scripts = skill / 'scripts'
    scripts.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('Use ${SKILL_DIR}/scripts/tool.py', encoding='utf-8')
    (scripts / 'tool.py').write_text('print("ok")', encoding='utf-8')
    workspace = tmp_path / 'workspace'

    manager = SkillManager(shared)
    record = manager.install('ppt-master', 'prj_1', workspace)
    specs = manager.build_skill_specs(workspace, [record])

    assert (workspace / '.mghands' / 'skills' / 'ppt-master' / 'scripts' / 'tool.py').exists()
    assert record.metadata.requires_dependencies is False
    assert len(specs) == 1
    assert specs[0].content.startswith('SKILL_DIR=/workspace/.mghands/skills/ppt-master')
    assert 'Use ${SKILL_DIR}/scripts/tool.py' in specs[0].content
