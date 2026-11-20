"""Append-only provider-reported usage. No prompts, responses or credentials.

One server process owns the journal; threads share one lock. Replay streams the
journal once at startup, never on each UI poll. Session counters stay compatible.
"""
import copy
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _totals():
    return dict(requests=0, input_tokens=0, output_tokens=0, total_tokens=0,
                cached_input_tokens=0, reasoning_output_tokens=0, missing_usage_requests=0)


def _summary():
    return dict(**_totals(), by_purpose={}, by_model={}, by_provider={}, by_day={})


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class TokenMeter:
    def __init__(self, path, journal_path=None):
        self.path = Path(path)
        self.journal_path = Path(journal_path) if journal_path else self.path.with_suffix('.jsonl')
        self.lock = threading.Lock()
        self.session_id = uuid4().hex
        self.data = dict(started_at=datetime.now(timezone.utc).isoformat(),
                         **_summary(), recent_requests=[])
        self.lifetime = _summary()
        self.journal_status = dict(format='jsonl', schema_version=1, skipped_lines=0,
                                   persistence_ok=True, accounting_timezone='UTC')
        self._rows = {}
        self._replay()

    def _apply(self, target, event):
        if event['event'] == 'finish':
            return
        groups = [target]
        for field, name in (('by_purpose', 'purpose'), ('by_model', 'model'),
                            ('by_provider', 'provider'), ('by_day', 'day')):
            groups.append(target[field].setdefault(event[name], _totals()))
        for group in groups:
            if event['event'] == 'begin':
                group['requests'] += 1
                group['missing_usage_requests'] += 1
            elif event['event'] == 'usage':
                group['missing_usage_requests'] -= 1
                for name in ('input_tokens', 'output_tokens', 'total_tokens',
                             'cached_input_tokens', 'reasoning_output_tokens'):
                    group[name] += event[name]

    def _replay(self):
        seen = {}
        try:
            with self.journal_path.open('r', encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if (not isinstance(event, dict) or event.get('schema_version') != 1
                                or event.get('event') not in ('begin', 'usage', 'finish')
                                or not all(isinstance(event.get(k), str) for k in
                                           ('session_id', 'request_id', 'purpose', 'model', 'provider', 'day'))):
                            raise ValueError('Invalid journal event')
                        if event['event'] == 'usage' and not all(_integer(event.get(k)) for k in
                                ('input_tokens', 'output_tokens', 'total_tokens',
                                 'cached_input_tokens', 'reasoning_output_tokens')):
                            raise ValueError('Invalid usage')
                        identity = (event['session_id'], event['request_id'])
                        kinds = seen.setdefault(identity, set())
                        kind = event['event']
                        if kind in ('begin', 'usage'):
                            if kind in kinds:
                                continue
                            if kind == 'usage' and 'begin' not in kinds:
                                raise ValueError('Usage without begin')
                            kinds.add(kind)
                        self._apply(self.lifetime, event)
                    except (ValueError, TypeError):
                        self.journal_status['skipped_lines'] += 1
        except FileNotFoundError:
            pass
        except OSError:
            self.journal_status['persistence_ok'] = False
            print('[Warning] Cannot read token journal; lifetime statistics may be incomplete')

    def _event(self, kind, row, **extra):
        return dict(schema_version=1, event=kind, session_id=self.session_id,
                    request_id=f"{self.session_id}:{row['id']}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    **{k: row[k] for k in ('purpose', 'model', 'provider', 'day')}, **extra)

    def _append(self, event):
        try:
            # Preserve a torn final line, separating future valid records from it.
            payload = (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
            with self.journal_path.open('a+b') as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b'\n':
                        stream.write(b'\n')
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self.journal_status['persistence_ok'] = False
            print('[Warning] Cannot append token journal; in-memory accounting continues')

    def begin(self, purpose='unknown', model='', input_bytes=0, provider='unknown'):
        with self.lock:
            now = datetime.now(timezone.utc).isoformat()
            request_id = self.data['requests'] + 1
            row = dict(id=request_id, purpose=purpose, model=model, provider=provider,
                       started_at=now, day=now[:10], input_bytes=input_bytes,
                       estimated_input_tokens=(input_bytes+2)//3, usage_known=False)
            self._rows[request_id] = row
            self.data['recent_requests'].append(row)
            self.data['recent_requests'] = self.data['recent_requests'][-100:]
            # Retain pending requests independently of the UI's last 100 entries.
            keep = {r['id'] for r in self.data['recent_requests']}
            self._rows = {i: r for i, r in self._rows.items()
                          if i in keep or not r.get('status') or
                          (r['status'] == 'response_received' and not r['usage_known'])}
            event = self._event('begin', row, input_bytes=input_bytes)
            self._append(event)
            self._apply(self.data, event)
            self._apply(self.lifetime, event)
            self._save()
            return request_id

    def finish(self, request_id, elapsed_seconds, status, error_type=None, timeout_phase=None):
        """Transport metadata only; exception messages may contain private content."""
        with self.lock:
            row = self._rows.get(request_id)
            if row is None:
                return
            metadata = dict(status=status, elapsed_seconds=round(elapsed_seconds, 3))
            if error_type:
                metadata['error_type'] = error_type
            if timeout_phase:
                metadata['timeout_phase'] = timeout_phase
            row.update(metadata)
            self._append(self._event('finish', row, **metadata))
            self._save()

    def record(self, usage, request_id=None):
        def field(obj, name):
            return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        prompt, output, total = (field(usage, k) for k in
                                 ('prompt_tokens', 'completion_tokens', 'total_tokens'))
        if not _integer(prompt) or not _integer(output):
            return
        cached = field(field(usage, 'prompt_tokens_details'), 'cached_tokens')
        reasoning = field(field(usage, 'completion_tokens_details'), 'reasoning_tokens')
        with self.lock:
            if request_id is None:
                request_id = self.data['requests']
            row = self._rows.get(request_id)
            if row is None or row['usage_known']:
                return
            counts = dict(input_tokens=prompt, output_tokens=output,
                          total_tokens=total if _integer(total) else prompt+output,
                          cached_input_tokens=cached if _integer(cached) else 0,
                          reasoning_output_tokens=reasoning if _integer(reasoning) else 0)
            event = self._event('usage', row, **counts)
            self._append(event)
            self._apply(self.data, event)
            self._apply(self.lifetime, event)
            row.update(usage_known=True, **counts)
            self._save()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(self.data, session_id=self.session_id,
                                      lifetime=self.lifetime, journal=self.journal_status))

    def _save(self):
        # Accounting failure must not fail an otherwise valid story turn.
        try:
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
        except OSError:
            print('[Warning] Cannot save token session snapshot')
