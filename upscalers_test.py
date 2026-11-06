"""Offline component archive installation regressions"""

import hashlib
import io
import json
import lzma
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Union
from unittest.mock import patch

from . import upscalers


class ComponentArchiveTest(unittest.TestCase):
    """Keep all archive payloads and potential escape targets in a temporary tree"""

    def setUp(self) -> None:
        """Create an installed component and a local download cache"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prefix = self.root / 'pfx'
        self.path = 'drive_c/windows/system32/umu'
        self.component = self.prefix / self.path
        self.component.mkdir(parents=True)
        self.cache = self.root / 'cache'
        (self.cache / 'upscalers').mkdir(parents=True)
        self.archive = self.cache / 'upscalers/component.tar.xz'
        self.section = 'opti_files'
        self.installed = self.component / 'installed.dll'
        self.installed.write_bytes(b'installed version')
        self.tracking = self.root / 'upscaler_files'
        self.original_tracking = {
            self.section: {
                f'{self.path}/installed.dll': {
                    'version': 'old',
                    'md5_hash': self.checksum(b'installed version'),
                }
            },
            'other_files': {'untouched': {}},
        }
        self.tracking.write_text(json.dumps(self.original_tracking))
        self.item = {
            'file_description': 'test component',
            'version': 'new',
            'download_url': 'https://example.invalid/component.tar.xz',
            'md5_hash': {'new.dll': self.checksum(b'new')},
        }
        self.enter_patch(patch.object(upscalers.config.path, 'cache_dir', self.cache))
        # An accidental download must fail, keeping these tests strictly offline.
        self.enter_patch(patch.object(upscalers.urllib.request, 'urlopen',
                                     side_effect=AssertionError('Unexpected network access')))

    def enter_patch(self, patcher) -> None:  # noqa: ANN001
        """Use a patch with cleanup on the supported Python 3.9 runtime"""
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def checksum(data: bytes) -> str:
        """Return a manifest-compatible checksum"""
        return hashlib.md5(data).hexdigest().upper()

    def make_archive(self, entries: list[tuple[str, bytes, bytes, str]]) -> None:
        """Write local tar members; every possible escape stays in self.root"""
        with tarfile.open(self.archive, 'w:xz') as archive:
            for name, data, kind, link in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = link
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                archive.addfile(member, io.BytesIO(data))

    def install(self) -> bool:
        """Exercise the real installer against the local cache"""
        return getattr(upscalers, '__download_component_files')(
            str(self.root), str(self.prefix), self.path, self.item, self.section
        )

    def assert_preserved(self) -> None:
        """Verify a rejected update leaves files and tracking unchanged"""
        self.assertEqual(self.installed.read_bytes(), b'installed version')
        self.assertEqual(json.loads(self.tracking.read_text()), self.original_tracking)

    def test_path_traversal_is_rejected_before_any_install(self) -> None:
        """Reject traversal even after an otherwise valid archive member"""
        self.make_archive([
            ('new.dll', b'new', tarfile.REGTYPE, ''),
            ('../../../../../escaped.txt', b'escaped', tarfile.REGTYPE, ''),
        ])
        self.assertFalse(self.install())
        self.assertFalse((self.root / 'escaped.txt').exists())
        self.assertFalse((self.component / 'new.dll').exists())
        self.assert_preserved()

    def test_corrupt_archive_preserves_installation(self) -> None:
        """A corrupt cached archive must not delete installed files"""
        self.archive.write_bytes(b'not a tar archive')
        self.assertFalse(self.install())
        self.assert_preserved()

    def test_truncated_member_preserves_installation(self) -> None:
        """Do not replace installed files when compressed member data is incomplete"""
        self.make_archive([('new.dll', b'x' * 2048, tarfile.REGTYPE, '')])
        raw = lzma.decompress(self.archive.read_bytes())
        self.archive.write_bytes(lzma.compress(raw[:700]))
        self.assertFalse(self.install())
        self.assert_preserved()

    def test_absolute_archive_path_is_rejected(self) -> None:
        """Reject absolute members inside the test's temporary tree"""
        escaped = self.root / 'absolute.dll'
        self.make_archive([(str(escaped), b'escaped', tarfile.REGTYPE, '')])
        self.assertFalse(self.install())
        self.assertFalse(escaped.exists())
        self.assert_preserved()

    def test_unsafe_tracking_path_is_rejected(self) -> None:
        """A forged tracking path cannot delete a file outside the component"""
        escaped = self.root / 'keep.dll'
        escaped.write_bytes(b'keep')
        self.original_tracking[self.section]['../keep.dll'] = {}
        self.tracking.write_text(json.dumps(self.original_tracking))
        self.make_archive([('new.dll', b'new', tarfile.REGTYPE, '')])
        self.assertFalse(self.install())
        self.assertEqual(escaped.read_bytes(), b'keep')
        self.assert_preserved()

    def test_unsafe_manifest_path_is_rejected(self) -> None:
        """Manifest file names must stay inside the component"""
        self.make_archive([('new.dll', b'new', tarfile.REGTYPE, '')])
        self.item['md5_hash'] = {'../outside.dll': ''}
        self.assertFalse(self.install())
        self.assert_preserved()

    def test_missing_manifest_file_preserves_installation(self) -> None:
        """Do not install packages missing a required manifest member"""
        self.make_archive([('new.dll', b'new', tarfile.REGTYPE, '')])
        self.item['md5_hash'] = {'missing.dll': self.checksum(b'missing')}
        self.assertFalse(self.install())
        self.assert_preserved()

    def test_checksum_failure_preserves_installation(self) -> None:
        """Check every manifest digest before replacing installed files"""
        self.make_archive([('new.dll', b'bad', tarfile.REGTYPE, '')])
        self.item['md5_hash'] = {'new.dll': self.checksum(b'expected')}
        self.assertFalse(self.install())
        self.assert_preserved()

    def test_unsafe_links_and_special_members_are_rejected(self) -> None:
        """Reject escaping links, link chains, directory links and special files"""
        for kind, link in [
            (tarfile.SYMTYPE, str(self.root / 'outside')),
            (tarfile.SYMTYPE, '../../../../../outside'),
            (tarfile.LNKTYPE, str(self.root / 'outside')),
            (tarfile.LNKTYPE, '../outside'),
            (tarfile.SYMTYPE, 'alias.dll'),
            (tarfile.SYMTYPE, 'folder'),
            (tarfile.SYMTYPE, 'missing'),
            (tarfile.FIFOTYPE, ''),
            (tarfile.CHRTYPE, ''),
            (tarfile.BLKTYPE, ''),
        ]:
            with self.subTest(kind=kind, link=link):
                self.make_archive([
                    ('folder', b'', tarfile.DIRTYPE, ''),
                    ('alias.dll', b'', kind, link),
                ])
                self.assertFalse(self.install())
                self.assert_preserved()

    def test_duplicate_and_conflicting_members_are_rejected(self) -> None:
        """Reject duplicate names and file/directory or symlink/parent collisions"""
        for entries in [
            [('new.dll', b'a', tarfile.REGTYPE, ''),
             ('./new.dll', b'b', tarfile.REGTYPE, '')],
            [('folder', b'a', tarfile.REGTYPE, ''),
             ('folder/new.dll', b'b', tarfile.REGTYPE, '')],
            [('target.dll', b'a', tarfile.REGTYPE, ''),
             ('folder', b'', tarfile.SYMTYPE, 'target.dll'),
             ('folder/new.dll', b'b', tarfile.REGTYPE, '')],
        ]:
            with self.subTest(entries=entries):
                self.make_archive(entries)
                self.assertFalse(self.install())
                self.assert_preserved()

    def test_existing_symlink_destinations_cannot_escape(self) -> None:
        """Reject existing directory and file symlinks pointing outside the prefix"""
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'new.dll').write_bytes(b'keep')
        (self.component / 'folder').symlink_to(outside, target_is_directory=True)
        (self.component / 'alias.dll').symlink_to(outside / 'new.dll')
        for name in ('folder/new.dll', 'alias.dll'):
            with self.subTest(name=name):
                self.make_archive([(name, b'bad', tarfile.REGTYPE, '')])
                self.item['md5_hash'] = {name: self.checksum(b'bad')}
                self.assertFalse(self.install())
                self.assertEqual((outside / 'new.dll').read_bytes(), b'keep')
                self.assert_preserved()

    def test_paths_outside_component_but_inside_prefix_are_rejected(self) -> None:
        """Tracking must not delete other prefix files"""
        other = self.prefix / 'other.dll'
        other.write_bytes(b'keep')
        self.original_tracking[self.section]['other.dll'] = {}
        self.tracking.write_text(json.dumps(self.original_tracking))
        self.make_archive([('new.dll', b'new', tarfile.REGTYPE, '')])
        self.assertFalse(self.install())
        self.assertEqual(other.read_bytes(), b'keep')
        self.assert_preserved()

    def test_absolute_and_parent_manifest_paths_are_rejected(self) -> None:
        """Validate all manifest names even when their digest is empty"""
        self.make_archive([('new.dll', b'new', tarfile.REGTYPE, '')])
        for name in (str(self.root / 'outside.dll'), 'nested/../../outside', '.'):
            with self.subTest(name=name):
                self.item['md5_hash'] = {name: ''}
                self.assertFalse(self.install())
                self.assert_preserved()

    def test_valid_update_keeps_ini_backup_and_internal_aliases(self) -> None:
        """Install real package layouts while preserving user files and INI backups"""
        ini = self.component / 'settings.ini'
        ini.write_bytes(b'user settings')
        (self.component / 'settings.ini.old').write_bytes(b'older settings')
        (self.component / 'user.txt').write_bytes(b'keep')
        (self.component / 'untouched.ini').write_bytes(b'keep settings')
        self.original_tracking[self.section][f'{self.path}/untouched.ini'] = {}
        self.tracking.write_text(json.dumps(self.original_tracking))
        self.make_archive([
            ('.', b'', tarfile.DIRTYPE, ''),
            ('empty', b'', tarfile.DIRTYPE, ''),
            ('alias.dll', b'', tarfile.SYMTYPE, 'new.dll'),
            ('hard.dll', b'', tarfile.LNKTYPE, 'new.dll'),
            ('./new.dll', b'new version', tarfile.REGTYPE, ''),
            ('plugins', b'', tarfile.DIRTYPE, ''),
            ('plugins/plugin.dll', b'plugin', tarfile.REGTYPE, ''),
            ('settings.ini', b'default settings', tarfile.REGTYPE, ''),
        ])
        self.item['md5_hash'] = {
            name: self.checksum(b'new version')
            for name in ('./new.dll', 'alias.dll', 'hard.dll')
        }
        self.assertTrue(self.install())
        self.assertFalse(self.installed.exists())
        self.assertEqual(ini.read_bytes(), b'default settings')
        self.assertEqual((self.component / 'settings.ini.old').read_bytes(), b'user settings')
        self.assertEqual((self.component / 'user.txt').read_bytes(), b'keep')
        self.assertEqual((self.component / 'untouched.ini').read_bytes(), b'keep settings')
        self.assertEqual((self.component / 'plugins/plugin.dll').read_bytes(), b'plugin')
        self.assertTrue((self.component / 'empty').is_dir())
        for name in ('new.dll', 'alias.dll', 'hard.dll'):
            self.assertEqual((self.component / name).read_bytes(), b'new version')
        self.assertTrue((self.component / 'alias.dll').is_symlink())
        self.assertEqual(json.loads(self.tracking.read_text())['other_files'], {'untouched': {}})
        self.assertTrue(getattr(upscalers, '__check_component_files')(
            str(self.root), str(self.prefix), self.path, self.item, self.section
        ))
        self.assertEqual(list(self.prefix.glob('.upscaler-*')), [])

    def prepare_replacement(self) -> None:
        """Prepare an update that changes files and a previously backed up INI"""
        (self.component / 'settings.ini').write_bytes(b'user settings')
        (self.component / 'settings.ini.old').write_bytes(b'older settings')
        self.make_archive([
            ('installed.dll', b'replacement', tarfile.REGTYPE, ''),
            ('nested/new.dll', b'new', tarfile.REGTYPE, ''),
            ('settings.ini', b'default settings', tarfile.REGTYPE, ''),
        ])
        self.item['md5_hash'] = {'installed.dll': self.checksum(b'replacement')}

    def test_empty_manifest_cannot_remove_installed_files(self) -> None:
        """A package without declared component files is not an update"""
        self.make_archive([])
        self.item['md5_hash'] = {}
        self.assertFalse(self.install())
        self.assert_preserved()

    def assert_rolled_back(self) -> None:
        """Verify old DLLs, both INIs, tracking and directory state are restored"""
        self.assert_preserved()
        self.assertEqual((self.component / 'settings.ini').read_bytes(), b'user settings')
        self.assertEqual((self.component / 'settings.ini.old').read_bytes(), b'older settings')
        self.assertFalse((self.component / 'nested').exists())
        self.assertEqual(list(self.prefix.glob('.upscaler-*')), [])

    def test_replacement_failure_rolls_back(self) -> None:
        """Undo a partial install when moving a later staged file fails"""
        self.prepare_replacement()
        replace = Path.replace

        def fail_second_file(source: Path, target: Path) -> Path:
            if source.name == 'new.dll' and '.upscaler-stage-' in str(source):
                raise OSError('simulated replacement failure')
            return replace(source, target)

        with patch.object(Path, 'replace', fail_second_file):
            self.assertFalse(self.install())
        self.assert_rolled_back()

    def test_tracking_failure_rolls_back(self) -> None:
        """An atomic tracking-write failure must restore the previous install"""
        self.prepare_replacement()
        replace = Path.replace

        def fail_tracking_write(source: Path, target: Union[str, Path]) -> Path:
            if Path(target) == self.tracking:
                raise OSError('simulated tracking failure')
            return replace(source, target)

        with patch.object(Path, 'replace', fail_tracking_write):
            self.assertFalse(self.install())
        self.assert_rolled_back()

    def test_failed_rollback_retains_backups(self) -> None:
        """Never delete recoverable old DLLs if restoration itself fails"""
        self.prepare_replacement()
        replace = Path.replace

        def fail_install_and_restore(source: Path, target: Path) -> Path:
            if '.upscaler-backup-' in str(source) or source.name == 'new.dll':
                raise OSError('simulated filesystem failure')
            return replace(source, target)

        with patch.object(Path, 'replace', fail_install_and_restore):
            self.assertFalse(self.install())
        backups = list(self.prefix.glob('.upscaler-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            (backups[0] / self.path / 'installed.dll').read_bytes(), b'installed version'
        )
        self.assertEqual(json.loads(self.tracking.read_text()), self.original_tracking)

    def test_ini_backup_collision_is_rejected(self) -> None:
        """A package cannot replace the backup of an existing user config"""
        ini = self.component / 'settings.ini'
        ini.write_bytes(b'user settings')
        self.make_archive([
            ('settings.ini', b'new settings', tarfile.REGTYPE, ''),
            ('settings.ini.old', b'clobber', tarfile.REGTYPE, ''),
        ])
        self.item['md5_hash'] = {'settings.ini': self.checksum(b'new settings')}
        self.assertFalse(self.install())
        self.assertEqual(ini.read_bytes(), b'user settings')
        self.assert_preserved()

    def test_check_and_cleanup_reject_untrusted_tracking_paths(self) -> None:
        """Checks and cleanup enforce the same path boundary as installation"""
        for name in ('../outside.dll', str(self.root / 'outside.dll'), 'other.dll'):
            with self.subTest(name=name):
                self.original_tracking[self.section][name] = {}
                self.tracking.write_text(json.dumps(self.original_tracking))
                self.assertFalse(getattr(upscalers, '__check_component_files')(
                    str(self.root), str(self.prefix), self.path, self.item, self.section
                ))
                with patch.object(upscalers, '__get_items',
                                  return_value=((self.path, self.item), self.section)):
                    self.assertFalse(upscalers.clean_tracked_files(
                        'optiscaler', str(self.root), str(self.prefix)
                    ))
                self.assert_preserved()
                del self.original_tracking[self.section][name]


if __name__ == '__main__':
    unittest.main()
