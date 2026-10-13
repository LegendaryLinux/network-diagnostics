import asyncio
import contextlib
import copy
import getpass
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import warnings

from netdiag.__main__ import main
from netdiag.collector import collect
from netdiag.config import load_config
from netdiag.storage import Reader


class ClientKeyTests(unittest.TestCase):
    def setUp(self):
        self.config = {'remote':{'host':'example.test','port':48123}}
        self.key = 'synthetic-test-key-not-a-secret-123456'

    def invoke(self, *, environment=None, prompt=None, config=None, argv=None):
        output = io.StringIO()
        with patch.dict(os.environ,environment or {},clear=True),patch('sys.argv',argv or ['netdiag','collect']),patch('netdiag.config.load_config',return_value=config or self.config),patch('netdiag.collector.collect',new_callable=AsyncMock) as run,patch('getpass.getpass',**(prompt or {'return_value':self.key})) as ask,contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            result = main()
            environment_after = dict(os.environ)
        self.assertNotIn(self.key,output.getvalue())
        return result,run,ask,output.getvalue(),environment_after

    def test_hidden_prompt_passes_memory_key_without_environment_mutation(self):
        for environment in ({},{'NETDIAG_KEY':''}):
            original = copy.deepcopy(self.config)
            result,run,ask,_,after = self.invoke(environment=environment)
            self.assertEqual(result,0)
            ask.assert_called_once_with('Diagnostic shared key: ')
            run.assert_awaited_once_with(self.config,serve_dashboard=True,key=self.key.encode())
            self.assertEqual(after,environment)
            self.assertEqual(self.config,original)

    def test_nonempty_environment_takes_precedence(self):
        result,run,ask,_,after = self.invoke(environment={'NETDIAG_KEY':self.key})
        self.assertEqual(result,0)
        ask.assert_not_called()
        self.assertEqual(run.call_args.kwargs['key'],self.key.encode())
        self.assertEqual(after['NETDIAG_KEY'],self.key)

    def test_short_keys_fail_before_collection_and_utf8_bytes_count(self):
        for prompt,environment in (({'return_value':'short'},{}),({'return_value':'é'*15},{}),(None,{'NETDIAG_KEY':'short'})):
            result,run,_,output,_ = self.invoke(prompt=prompt,environment=environment)
            self.assertEqual(result,1)
            run.assert_not_called()
            self.assertIn('at least 32',output)
        result,run,_,_,_ = self.invoke(prompt={'return_value':'é'*16})
        self.assertEqual(result,0)
        self.assertEqual(len(run.call_args.kwargs['key']),32)

    def test_cancellation_stops_before_collection(self):
        for interruption in (EOFError,KeyboardInterrupt):
            result,run,_,output,_ = self.invoke(prompt={'side_effect':interruption})
            self.assertEqual(result,1)
            run.assert_not_called()
            self.assertIn('cancelled',output)

    def test_echo_fallback_is_aborted_before_read(self):
        fallback_read = MagicMock()
        def unavailable_terminal(prompt):
            warnings.warn('Password input may be echoed.',getpass.GetPassWarning)
            fallback_read()
            return self.key
        result,run,_,output,_ = self.invoke(prompt={'side_effect':unavailable_terminal})
        self.assertEqual(result,1)
        run.assert_not_called()
        fallback_read.assert_not_called()
        self.assertIn('usable terminal',output)
        self.assertIn('NETDIAG_KEY',output)

    def test_local_collection_and_dashboard_do_not_prompt(self):
        result,run,ask,_,_ = self.invoke(config={'remote':None})
        self.assertEqual(result,0)
        ask.assert_not_called()
        self.assertIsNone(run.call_args.kwargs['key'])
        with patch('netdiag.storage.Reader') as reader,patch('netdiag.dashboard.create_server') as create_server:
            result,run,ask,_,_ = self.invoke(argv=['netdiag','dashboard'])
        self.assertEqual(result,0)
        ask.assert_not_called()
        run.assert_not_called()
        reader.assert_called_once()
        create_server.return_value.serve_forever.assert_called_once()

    def test_prompt_key_does_not_enter_dataset_and_invalid_key_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config.update(db=str(Path(directory)/'dataset'/'test.sqlite3'),gateway=None,services=[],remote={'host':'example.test','port':48123})
            with self.assertRaises(ValueError):
                asyncio.run(collect(config,serve_dashboard=False,key=b'short'))
            self.assertFalse(Path(config['db']).exists())
            async def stopped_collection():
                stop = asyncio.Event()
                stop.set()
                await collect(config,serve_dashboard=False,stop=stop,key=self.key.encode())
            output = io.StringIO()
            with patch('netdiag.protocol.load_key') as load_key,contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
                asyncio.run(stopped_collection())
            load_key.assert_not_called()
            self.assertNotIn(self.key,output.getvalue())
            self.assertNotIn(self.key.encode(),Path(config['db']).read_bytes())
            self.assertEqual(Reader(config['db']).metadata()['sessions'][0]['config'],config)


if __name__=='__main__':
    unittest.main()
