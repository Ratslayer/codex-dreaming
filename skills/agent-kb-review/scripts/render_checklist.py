#!/usr/bin/env python3
"""Render the reusable live checklist. This command never reads or changes KB state.

Input JSON: schema_version=2, absolute kb_root, optional preview=true, and groups.
Each group: project, proposal_id, proposal_hash (64 hex), assessment_versions,
entries. Each entry: id, title, text (exact original), lesson_ids, lesson_versions,
rationale, optional applicability/review_note/requested_text, evidence list of
quote plus source label/session_id and source_hash (resolved to the preserved
source automatically), or an explicit absolute source_path inside the KB.
Only new/changed entries need be displayed; preserve original snapshot fields.
Merge entries also contain replaces (two or more exact original entry snapshots)
and merge_conditions explaining compatible applicability and preserved exceptions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

TOKEN = '__AGENT_KB_REVIEW_DATA__'


def render(data):
    data = json.loads(json.dumps(data))
    if not isinstance(data, dict) or data.get('schema_version') != 2 or not Path(data.get('kb_root', '')).is_absolute():
        raise ValueError('Expected schema_version 2 and absolute kb_root')
    if not isinstance(data.get('groups'), list) or not data['groups']:
        raise ValueError('At least one proposal group is required')
    seen = set()
    for group in data['groups']:
        if not isinstance(group.get('project'), str) or not isinstance(group.get('proposal_id'), str) or not re.fullmatch(r'[a-f0-9]{64}', group.get('proposal_hash', '')):
            raise ValueError('Group needs a project, proposal ID, and exact snapshot hash')
        if not isinstance(group.get('entries'), list):
            raise ValueError('Group entries must be a list')
        for entry in group['entries']:
            if not isinstance(entry.get('id'), str) or entry['id'] in seen or not isinstance(entry.get('text'), str) or not isinstance(entry.get('title'), str):
                raise ValueError('Entries need unique IDs, exact text, and concise titles')
            if not isinstance(entry.get('lesson_ids'), list) or not entry['lesson_ids'] or not isinstance(entry.get('lesson_versions'), dict):
                raise ValueError('Entry snapshot needs supporting lesson IDs and versions')
            if set(entry['lesson_ids']) != set(entry['lesson_versions']) or any(not re.fullmatch(r'[a-f0-9]{64}', v) for v in entry['lesson_versions'].values()):
                raise ValueError('Lesson versions must match the supporting IDs')
            if entry.get('requested_text') is not None and (not isinstance(entry['requested_text'], str) or not entry['requested_text'].strip()):
                raise ValueError('Requested wording must be nonempty text')
            if 'replaces' in entry:
                originals = entry['replaces']
                if not isinstance(originals, list) or len(originals) < 2 or not isinstance(entry.get('merge_conditions'), str) or not entry['merge_conditions'].strip():
                    raise ValueError('A merge needs originals and compatible conditions')
                ids, versions = set(), {}
                for original in originals:
                    if not isinstance(original, dict) or not isinstance(original.get('id'), str) or original['id'] in ids or original['id'] == entry['id'] or not isinstance(original.get('text'), str):
                        raise ValueError('Invalid or duplicate merge original')
                    if not isinstance(original.get('lesson_ids'), list) or not original['lesson_ids'] or not isinstance(original.get('lesson_versions'), dict) or set(original['lesson_ids']) != set(original['lesson_versions']):
                        raise ValueError('Original snapshots need lesson IDs and versions')
                    for lid, version in original['lesson_versions'].items():
                        if lid in versions and versions[lid] != version:
                            raise ValueError('Merge originals have conflicting lesson versions')
                        versions[lid] = version
                    ids.add(original['id'])
                if versions != entry['lesson_versions']:
                    raise ValueError('Merged evidence must retain every original lesson version')
            if not isinstance(entry.get('rationale'), str) or not entry['rationale'].strip() or not isinstance(entry.get('evidence'), list) or not entry['evidence']:
                raise ValueError('Foldout details need a rationale and supporting evidence')
            for source in entry['evidence']:
                if not isinstance(source.get('quote'), str) or not source['quote'].strip():
                    raise ValueError('Evidence needs the preserved quote')
                if source.get('source_hash') and not re.fullmatch(r'[a-f0-9]{64}', source['source_hash']):
                    raise ValueError('Invalid evidence source hash')
                if source.get('source_hash') and not source.get('source_path'):
                    source['source_path'] = str(Path(data['kb_root']) / '.aggregator/sources' / (source['source_hash'] + '.md'))
                if source.get('source_path'):
                    path = Path(source['source_path'])
                    if not path.is_absolute() or not path.resolve().is_relative_to(Path(data['kb_root']).resolve()) or any(ord(c) < 32 for c in str(path)):
                        raise ValueError('Evidence paths must stay inside the KB')
            seen.add(entry['id'])
    if not seen:
        raise ValueError('No proposed changes to display')
    result = dict(data)
    result['review_id'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    encoded = json.dumps(result, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    template = Path(__file__).resolve().parent.parent / 'assets/review-checklist.html'
    fragment = template.read_text().replace(TOKEN, encoded)
    if len(fragment.encode()) > 1_000_000:
        raise ValueError('Checklist exceeds the inline size limit; use a smaller batch')
    return fragment


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', required=True, type=Path, help='Reviewed proposal data JSON')
    parser.add_argument('--output', required=True, type=Path, help='Absolute .html path in the thread visualization directory')
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.suffix != '.html':
        parser.error('--output must be an absolute .html path')
    try:
        fragment = render(json.loads(args.input.read_text()))
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(fragment)
    print(args.output)


if __name__ == '__main__':
    main()
