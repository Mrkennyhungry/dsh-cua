"""Reconcile bounded CLI output with full persisted DSH session evidence."""
import json
from pathlib import Path
import re
import sys

import zstandard


report_path = Path(sys.argv[1])
report = json.loads(report_path.read_text(encoding='utf-8'))
cwd = str(Path(__file__).resolve().parent.parent)
session_root = Path.home() / '.dsh' / 'sessions' / ('--' + re.sub(r'[^a-zA-Z0-9]+', '-', cwd) + '--')
# DSH replaces each path separator/punctuation with a dash, preserving letters.
for trial in report['trials']:
    sessions = trial.get('session_ids', [trial['session_id']])
    rows = []
    missing = []
    for session in sessions:
        path = session_root / session / 'session.v4.jsonl.zstd'
        if not path.exists():
            missing.append(str(path))
            continue
        with path.open('rb') as stream:
            raw = zstandard.ZstdDecompressor().stream_reader(stream).read()
        rows.extend(json.loads(line) for line in raw.splitlines() if line)
    if missing:
        trial['session_evidence'] = {'error': 'Persisted session was not found', 'paths': missing}
        continue
    calls, values, images, observations = [], [], [], {}
    headers, usages = [], []
    starts = sum(row['type'] == 'step/start' for row in rows)
    for row in rows:
        data = row.get('data', {})
        if row['type'] == 'assistant/message' and isinstance(data.get('usage'), dict):
            usages.append(data['usage'])
        if row['type'] == 'request/header':
            header = data['header']
            headers.append({'config': header.get('config'), 'tools': [tool.get('name') for tool in header.get('tools', [])]})
        if row['type'] == 'tool/call':
            calls.append({'call_id': data['callId'], 'name': data['name'], 'arguments': data['arguments']})
        if row['type'] != 'tool/result':
            continue
        message = data['message']
        for part in message.get('content', []):
            if part.get('type') == 'image':
                images.append(part['attachment'])
            if part.get('type') != 'text':
                continue
            try:
                value = json.loads(part['text'])
            except (ValueError, TypeError):
                continue
            values.append({'call_id': message.get('toolCallId'), 'value': value})
            observation = value.get('observation', {}) if isinstance(value, dict) else {}
            if observation.get('observation_id'):
                observations[observation['observation_id']] = observation
    backend = {}
    for observation in observations.values():
        for key, value in observation.get('trace', {}).items():
            if isinstance(value, (int, float)):
                backend[key] = round(backend.get(key, 0) + value, 3)
    trial['session_evidence'] = {'rows': len(rows), 'sessions': sessions, 'headers': headers, 'calls': calls, 'results': values,
                                 'attachments': images, 'unique_observations': len(observations),
                                 'backend_totals': backend,
                                 'text_bytes': sum(len(observation.get('text', '').encode('utf-8')) for observation in observations.values())}
    # The CLI intentionally bounds tool text. Use the persisted result for
    # failure and frame counts, including calls whose CLI preview was truncated.
    trial['failed_tools'] = len({result['call_id'] for result in values
                                 if isinstance(result['value'], dict) and (
                                     result['value'].get('success') is False
                                     or result['value'].get('error')
                                     or result['value'].get('effect') == 'refused')})
    trial['images'] = [frame for observation in observations.values() for frame in observation.get('frames', [])]
    trial['image_pixels'] = sum(frame['width'] * frame['height'] for frame in trial['images'])
    trial['usage_complete'] = bool(usages) and len(usages) == starts
    trial['usage_observed'] = {}
    for key in ('inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens'):
        observed = (sum(usage[key] for usage in usages)
                    if usages and all(isinstance(usage.get(key), (int, float)) for usage in usages) else None)
        trial['usage_observed'][key] = observed
        trial['usage'][key] = observed if trial['usage_complete'] else None
    provider_images = [image for request in trial.get('provider_requests', []) for image in request['images']]
    attachments = {image['attachmentId'].removeprefix('sha256:'): image for image in images}
    for image in provider_images:
        attachment = attachments.get(image['sha256'])
        if attachment and 'width' not in image:
            image.update({key: attachment[key] for key in ('width', 'height')})
    trial['provider_image_count'] = len(provider_images)
    trial['provider_image_pixels'] = sum(image.get('width', 0) * image.get('height', 0) for image in provider_images)
    trial['provider_unique_images'] = len({image['sha256'] for image in provider_images})
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'trials': len(report['trials']), 'reconciled': sum('rows' in trial['session_evidence'] for trial in report['trials'])}))
