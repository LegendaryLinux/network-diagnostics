from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from netdiag.credentials import VERSION, load_saved_key, save_key
from netdiag.protocol import derive_key


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ,{'XDG_CONFIG_HOME':str(self.root)},clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.remote = {'host':'example.test','port':48123}
        self.key = bytes(range(32))

    def test_private_derived_only_roundtrip_and_endpoint_isolation(self):
        self.assertIsNone(load_saved_key(self.remote))
        self.assertFalse((self.root/'network-diagnostics').exists())
        passphrase = ' private ünicode passphrase '
        key = derive_key(passphrase)
        save_key(self.remote,key)
        directory = self.root/'network-diagnostics'
        file = next(directory.iterdir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
        self.assertEqual(file.stat().st_nlink,1)
        self.assertEqual(file.read_bytes(),VERSION+key)
        self.assertNotIn(passphrase.encode(),file.read_bytes())
        self.assertEqual(load_saved_key({'host':'EXAMPLE.TEST.','port':48123}),key)
        self.assertIsNone(load_saved_key({'host':'other.test','port':48123}))
        self.assertIsNone(load_saved_key({'host':'example.test','port':48124}))

    def test_fallback_home_is_isolated_and_relative_xdg_is_refused(self):
        with patch.dict(os.environ,{},clear=True),patch('pathlib.Path.home',return_value=self.root):
            save_key(self.remote,self.key)
            self.assertEqual(load_saved_key(self.remote),self.key)
            self.assertTrue((self.root/'.config/network-diagnostics').is_dir())
        with patch.dict(os.environ,{'XDG_CONFIG_HOME':'relative'}):
            with self.assertRaisesRegex(ValueError,'absolute'):
                save_key(self.remote,self.key)

    def test_invalid_version_length_and_replacement(self):
        save_key(self.remote,self.key)
        file = next((self.root/'network-diagnostics').iterdir())
        for invalid in (b'',VERSION+self.key[:-1],VERSION+self.key+b'x',b'wrong-version'+self.key):
            file.write_bytes(invalid)
            with self.assertRaisesRegex(ValueError,'invalid format'):
                load_saved_key(self.remote)
        save_key(self.remote,self.key)
        self.assertEqual(load_saved_key(self.remote),self.key)
        for invalid in ('plaintext',b'short',b'x'*33):
            with self.assertRaisesRegex(ValueError,'32-byte derived'):
                save_key(self.remote,invalid)

    def test_unsafe_directory_and_file_permissions_are_not_changed(self):
        save_key(self.remote,self.key)
        directory = self.root/'network-diagnostics'
        file = next(directory.iterdir())
        for path,mode,restore in ((directory,0o755,0o700),(file,0o644,0o600),(self.root,0o777,0o700)):
            path.chmod(mode)
            try:
                for operation in (lambda:load_saved_key(self.remote),lambda:save_key(self.remote,self.key)):
                    with self.assertRaises(ValueError):
                        operation()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),mode)
            finally:
                path.chmod(restore)
        with patch('netdiag.credentials.os.getuid',return_value=os.getuid()+1):
            with self.assertRaises(ValueError):
                load_saved_key(self.remote)

    def test_symlink_hardlink_and_fifo_refused_without_reading_or_overwriting(self):
        save_key(self.remote,self.key)
        file = next((self.root/'network-diagnostics').iterdir())
        original = file.read_bytes()
        other = self.root/'untouched'
        other.write_bytes(original)
        other.chmod(0o600)
        for kind in ('symlink','hardlink','fifo'):
            file.unlink()
            if kind=='symlink':
                file.symlink_to(other)
            elif kind=='hardlink':
                os.link(other,file)
            else:
                os.mkfifo(file,0o600)
            for operation in (lambda:load_saved_key(self.remote),lambda:save_key(self.remote,self.key)):
                with self.assertRaises(ValueError):
                    operation()
            self.assertEqual(other.read_bytes(),original)
        file.unlink()
        (self.root/'network-diagnostics').rmdir()
        (self.root/'network-diagnostics').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):
            save_key(self.remote,self.key)

    def test_atomic_failure_preserves_previous_key_and_cleans_temporary_file(self):
        save_key(self.remote,self.key)
        with patch('netdiag.credentials.os.replace',side_effect=OSError('synthetic disk error')):
            with self.assertRaisesRegex(ValueError,'Cannot safely save'):
                save_key(self.remote,b'x'*32)
        self.assertEqual(load_saved_key(self.remote),self.key)
        self.assertEqual(len(list((self.root/'network-diagnostics').iterdir())),1)

    def test_parallel_endpoint_saves_do_not_lose_other_keys(self):
        endpoints = [{'host':f'{index}.example.test','port':48123} for index in range(16)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda endpoint:save_key(endpoint,self.key),endpoints))
        for endpoint in endpoints:
            self.assertEqual(load_saved_key(endpoint),self.key)


if __name__=='__main__':
    unittest.main()
