import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import build_v1_hydro_install_backup as hydro
import collect_v1_ordinary_oj_observation as ordinary


class PM2EntrypointTests(unittest.TestCase):
    def fixture(self, *, profile=True, store_mode=0o1775, owner=0,
                target_mode=0o555, link_owner=0, parent_mode=0o555):
        target = Path('/nix/store/abc-pm2/bin/pm2').absolute()
        requested = Path('/root/.nix-profile/bin/pm2').absolute() if profile else target

        def resolve(path, strict=False):
            if path == requested:
                return target
            # A real profile resolves through a separate Nix user environment.
            if str(path).startswith(str(Path('/root/.nix-profile').absolute())):
                return Path('/nix/store/profile/bin').absolute()
            return path

        def metadata(path, **_kwargs):
            path = Path(path)
            mode, uid = stat.S_IFDIR | 0o755, 0
            if path == target:
                mode, uid = stat.S_IFREG | target_mode, owner
            elif path == requested and profile:
                mode, uid = stat.S_IFLNK | 0o777, link_owner
            elif path == Path('/root/.nix-profile').absolute() and profile:
                mode, uid = stat.S_IFLNK | 0o777, link_owner
            elif path == Path('/nix/store').absolute():
                mode = stat.S_IFDIR | store_mode
            elif path == target.parent:
                mode = stat.S_IFDIR | parent_mode
            return SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=1)

        rows = [{'name': name, 'pid': i + 10, 'pm2_env': {
            'status': 'online', 'restart_time': 0,
        }} for i, name in enumerate(ordinary.PROCESS_NAMES)]
        raw = json.dumps(rows)
        return requested, resolve, metadata, raw

    def run_both(self, **options):
        requested, resolve, metadata, raw = self.fixture(**options)
        outputs = []
        with mock.patch.object(Path, 'resolve', resolve), \
                mock.patch('os.lstat', side_effect=metadata), \
                mock.patch.object(Path, 'stat', metadata), \
                mock.patch('os.access', return_value=True):
            for module, function in ((hydro, hydro.pm2_jlist), (ordinary, ordinary.pm2_rows)):
                result = SimpleNamespace(returncode=0, stdout=raw.encode() if module is hydro else raw)
                with mock.patch.object(module.subprocess, 'run', return_value=result) as run:
                    outputs.append(function(requested))
                    self.assertEqual(run.call_args.args[0][1:], ['jlist', '--silent'])
        return outputs

    def test_root_owned_nix_profile_works_in_both_collectors(self):
        self.assertEqual([len(rows) for rows in self.run_both()], [4, 4])

    def test_root_owned_canonical_nix_store_works_in_both_collectors(self):
        self.assertEqual([len(rows) for rows in self.run_both(profile=False)], [4, 4])

    def test_read_only_nix_store_also_works(self):
        self.assertEqual([len(rows) for rows in self.run_both(store_mode=0o555)], [4, 4])

    def test_relative_entrypoint_is_rejected_before_pm2_runs(self):
        with mock.patch('subprocess.run') as run:
            for function, error in ((hydro.pm2_jlist, hydro.CollectError),
                                    (ordinary.pm2_rows, ordinary.ObservationError)):
                with self.assertRaises(error):
                    function(Path('pm2'))
            run.assert_not_called()

    def test_untrusted_targets_are_rejected_before_pm2_runs(self):
        for options in ({'owner': 1000}, {'target_mode': 0o775},
                        {'link_owner': 1000}, {'store_mode': 0o775},
                        {'store_mode': 0o1777}, {'parent_mode': 0o775}):
            requested, resolve, metadata, _ = self.fixture(**options)
            with self.subTest(options=options), mock.patch.object(Path, 'resolve', resolve), \
                    mock.patch('os.lstat', side_effect=metadata), \
                    mock.patch.object(Path, 'stat', metadata), \
                    mock.patch('os.access', return_value=True), \
                    mock.patch('subprocess.run') as run:
                for function, error in ((hydro.pm2_jlist, hydro.CollectError),
                                        (ordinary.pm2_rows, ordinary.ObservationError)):
                    with self.assertRaises(error):
                        function(requested)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
