import tempfile
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path
from token_meter import TokenMeter


class TokenMeterTests(unittest.TestCase):
    def test_retry_and_unknown_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = TokenMeter(Path(directory) / 'usage.json')
            meter.begin()
            meter.record({'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150})
            meter.begin()
            meter.record({'prompt_tokens': 90, 'completion_tokens': 10})
            meter.begin()
            meter.record(None)
            result = meter.snapshot()
            self.assertEqual(result['total_tokens'], 250)
            self.assertEqual(result['requests'], 3)
            self.assertEqual(result['missing_usage_requests'], 1)
            self.assertTrue(meter.path.exists())

    def test_server_restart_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.json'
            old = TokenMeter(path)
            request = old.begin('scene', 'deepseek-v4.1-flash', provider='apiz')
            old.record({'prompt_tokens': 100, 'completion_tokens': 20}, request)
            new = TokenMeter(path).snapshot()
            self.assertEqual(new['requests'], 0)
            self.assertEqual(new['lifetime']['requests'], 1)
            self.assertEqual(new['lifetime']['total_tokens'], 120)
            self.assertEqual(new['lifetime']['by_provider']['apiz']['input_tokens'], 100)
            self.assertEqual(new['lifetime']['by_model']['deepseek-v4.1-flash']['output_tokens'], 20)
            self.assertNotEqual(new['session_id'], old.session_id)

    def test_usage_is_idempotent_and_optional_details_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = TokenMeter(Path(directory) / 'usage.json')
            request = meter.begin('scene', 'grok-4.7', provider='apiz')
            usage = dict(prompt_tokens=100, completion_tokens=50,
                         prompt_tokens_details={'cached_tokens': 80},
                         completion_tokens_details={'reasoning_tokens': 20},
                         prompt='private prompt', response='private response', api_key='secret-key')
            meter.record(usage, request)
            meter.record(usage, request)
            meter.record(usage, 9999)
            result = meter.snapshot()
            self.assertEqual(result['total_tokens'], 150)
            self.assertEqual(result['cached_input_tokens'], 80)
            self.assertEqual(result['reasoning_output_tokens'], 20)
            self.assertEqual(result['missing_usage_requests'], 0)
            journal = meter.journal_path.read_text(encoding='utf-8')
            for secret in ('private prompt', 'private response', 'secret-key'):
                self.assertNotIn(secret, journal)
            events = [json.loads(line) for line in journal.splitlines()]
            self.assertEqual([e['event'] for e in events], ['begin', 'usage'])
            self.assertEqual(events[0]['request_id'], events[1]['request_id'])

    def test_timeout_unknown_usage_survives_restart_and_does_not_become_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.json'
            meter = TokenMeter(path)
            request = meter.begin('scene', 'gemini-3.8-flash', provider='gemini')
            meter.finish(request, 180.5, 'timeout', 'APITimeoutError', 'read')
            meter.record(None, request)
            result = TokenMeter(path).snapshot()['lifetime']
            self.assertEqual(result['requests'], 1)
            self.assertEqual(result['missing_usage_requests'], 1)
            row = json.loads(meter.journal_path.read_text().splitlines()[1])
            self.assertEqual(row['status'], 'timeout')
            self.assertEqual(row['timeout_phase'], 'read')

    def test_torn_line_and_duplicate_events_do_not_destroy_future_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.json'
            meter = TokenMeter(path)
            request = meter.begin()
            meter.record({'prompt_tokens': 10, 'completion_tokens': 5}, request)
            content = meter.journal_path.read_text()
            with meter.journal_path.open('a', encoding='utf-8') as stream:
                stream.write(content + '{"partial":')
            new = TokenMeter(path)
            self.assertEqual(new.snapshot()['lifetime']['total_tokens'], 15)
            self.assertEqual(new.snapshot()['journal']['skipped_lines'], 1)
            request = new.begin()
            new.record({'prompt_tokens': 20, 'completion_tokens': 5}, request)
            restarted = TokenMeter(path).snapshot()
            self.assertEqual(restarted['lifetime']['total_tokens'], 40)
            self.assertEqual(restarted['lifetime']['requests'], 2)
            self.assertEqual(restarted['journal']['skipped_lines'], 1)

    def test_disk_failure_does_not_fail_request_and_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = TokenMeter(Path(directory) / 'usage.json', Path(directory) / 'missing' / 'usage.jsonl')
            request = meter.begin()
            meter.record({'prompt_tokens': 1, 'completion_tokens': 2}, request)
            self.assertEqual(meter.snapshot()['total_tokens'], 3)
            self.assertFalse(meter.snapshot()['journal']['persistence_ok'])

    def test_concurrent_requests_and_old_pending_request_outside_recent_list(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = TokenMeter(Path(directory) / 'usage.json')
            first = meter.begin('scene', 'gpt-6-luna', provider='apiz')
            def run(index):
                request = meter.begin('tracker', 'gpt-6-luna', provider='apiz')
                meter.finish(request, 0.1, 'response_received')
                meter.record({'prompt_tokens': 2, 'completion_tokens': 1}, request)
            with patch('token_meter.os.fsync'), ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(run, range(105)))
            meter.finish(first, 0.2, 'response_received')
            meter.record({'prompt_tokens': 5, 'completion_tokens': 5}, first)
            result = TokenMeter(meter.path).snapshot()['lifetime']
            self.assertEqual(result['requests'], 106)
            self.assertEqual(result['total_tokens'], 325)
            self.assertEqual(result['missing_usage_requests'], 0)
            self.assertEqual(len(meter.snapshot()['recent_requests']), 100)

    def test_invalid_usage_and_snapshot_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = TokenMeter(Path(directory) / 'usage.json')
            request = meter.begin()
            for usage in ({'prompt_tokens': True, 'completion_tokens': 1},
                          {'prompt_tokens': -1, 'completion_tokens': 1}):
                meter.record(usage, request)
            snapshot = meter.snapshot()
            snapshot['lifetime']['requests'] = 900
            self.assertEqual(meter.snapshot()['lifetime']['requests'], 1)
            self.assertEqual(meter.snapshot()['missing_usage_requests'], 1)


if __name__ == '__main__':
    unittest.main()
