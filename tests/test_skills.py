from io import BytesIO
from pathlib import Path
import zipfile

from mghands_gateway.skills import SkillManager


def _zip(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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


def test_skill_upload_accepts_root_skill_md_and_detects_dependencies(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    archive = _zip(
        {
            'SKILL.md': '---\ntriggers: [demo]\n---\nUse it.',
            'requirements.txt': 'demo-package==1.0\n',
        }
    )

    record = SkillManager(tmp_path / 'shared').upload_zip(
        'uploaded-skill',
        'prj_1',
        workspace,
        archive,
        'skill.zip',
    )
    specs = SkillManager(tmp_path / 'shared').build_skill_specs(workspace, [record])

    assert (workspace / '.mghands' / 'skills' / 'uploaded-skill' / 'SKILL.md').exists()
    assert record.metadata.source_type == 'uploaded'
    assert record.metadata.source_name == 'skill.zip'
    assert record.metadata.requires_dependencies is True
    assert record.metadata.dependencies == ['demo-package==1.0']
    assert specs[0].triggers == ['demo']


def test_skill_upload_normalizes_single_top_level_directory(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    archive = _zip(
        {
            'ignored-root/SKILL.md': 'Use normalized root.',
            'ignored-root/scripts/tool.py': 'print("ok")',
        }
    )

    SkillManager(tmp_path / 'shared').upload_zip('normalized', 'prj_1', workspace, archive)

    target = workspace / '.mghands' / 'skills' / 'normalized'
    assert (target / 'SKILL.md').read_text(encoding='utf-8') == 'Use normalized root.'
    assert (target / 'scripts' / 'tool.py').exists()
    assert not (target / 'ignored-root').exists()


def test_skill_upload_overwrites_and_removes_stale_files(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    manager = SkillManager(tmp_path / 'shared')
    manager.upload_zip('replace-me', 'prj_1', workspace, _zip({'SKILL.md': 'old', 'old.txt': 'stale'}))

    record = manager.upload_zip('replace-me', 'prj_1', workspace, _zip({'SKILL.md': 'new'}))

    target = workspace / '.mghands' / 'skills' / 'replace-me'
    assert (target / 'SKILL.md').read_text(encoding='utf-8') == 'new'
    assert not (target / 'old.txt').exists()
    assert record.metadata.source_type == 'uploaded'


def test_skill_upload_rejects_invalid_zip_without_changing_existing_skill(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    manager = SkillManager(tmp_path / 'shared')
    manager.upload_zip('safe', 'prj_1', workspace, _zip({'SKILL.md': 'old'}))

    try:
        manager.upload_zip('safe', 'prj_1', workspace, b'not a zip')
    except ValueError as exc:
        assert 'valid zip' in str(exc)
    else:
        raise AssertionError('expected ValueError')

    assert (workspace / '.mghands' / 'skills' / 'safe' / 'SKILL.md').read_text(encoding='utf-8') == 'old'


def test_skill_upload_rejects_unsafe_zip_paths(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    manager = SkillManager(tmp_path / 'shared')

    for unsafe_name in ['../SKILL.md', 'root/../SKILL.md', '/SKILL.md', 'C:/temp/SKILL.md', 'root//SKILL.md']:
        try:
            manager.upload_zip('unsafe', 'prj_1', workspace, _zip({unsafe_name: 'bad'}))
        except ValueError as exc:
            assert 'unsafe paths' in str(exc)
        else:
            raise AssertionError(f'expected ValueError for {unsafe_name}')


def test_skill_upload_rejects_zip_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('SKILL.md', 'valid')
        info = zipfile.ZipInfo('link')
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, 'SKILL.md')

    try:
        SkillManager(tmp_path / 'shared').upload_zip('unsafe', 'prj_1', workspace, buffer.getvalue())
    except ValueError as exc:
        assert 'special files' in str(exc)
    else:
        raise AssertionError('expected ValueError')
