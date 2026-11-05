"""整场文件式证据：提供原文入口，不替agent预选点歌上下文。"""
import hashlib
import json
from collections import ChainMap
from pathlib import Path

from .live_share import atomic_json, epoch


def source_id(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def check_room(value, room):
    if value.get('room_id') is not None and str(value['room_id']) != str(room):
        raise ValueError('整场证据包含其他房间')


def transcript_records(room, session):
    """优先使用离线校正稿（无校正时回退离线原稿），不混入在线ASR；歌词只作识别线索，不认证声源。"""
    records = {}
    for segment in session['source']['segments']:
        check_room(segment, room)
        if not segment.get('is_final') or not segment.get('text'):
            continue
        corrected = segment.get('corrected_text')
        layer = 'corrected' if corrected else 'original'
        text = corrected or segment['text']
        ident = source_id([room, session['id'], segment['seq'], text])
        records[ident] = {'id': ident, 'speaker': 'streamer', 'kind': 'transcript',
            'session_id': session['id'], 'at': segment['start_utc'],
            'end': segment['end_utc'], 'text': text, 'transcription': layer,
            'source_seq': segment['seq'], **({'time_mapping': segment['time_mapping']}
                                           if segment.get('time_mapping') else {})}
    return records


def evidence_map(person, material):
    """本人证据覆盖共享弹幕标签，不能把其他账号当成本人。"""
    return ChainMap({e['id']: e for e in person['evidence']}, material.get('source_evidence', {}))


def freeze_sources(directory, material, *, verify_only=False):
    """每场共享一份原文，input只给文件指针；不拼装LLM context window。"""
    directory = Path(directory).resolve()
    root = directory / 'sources'
    if not verify_only:
        root.mkdir(exist_ok=True)
    files = []

    def write(name, data):
        path = root / name
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
        if path.exists() and path.read_text() != payload:
            raise ValueError('冻结整场证据文件已变化：' + name)
        if not path.exists() and verify_only:
            raise ValueError('冻结整场证据文件缺失：' + name)
        if not path.exists():
            path.write_text(payload)
        item = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        files.append(item)
        return item

    sessions = []
    grouped = {}
    for record in material['source_evidence'].values():
        grouped.setdefault(record['session_id'], []).append(record)
    for index, session in enumerate(material['sessions']):
        records = grouped.get(session['id'], [])
        records.sort(key=lambda e: (epoch(e['at']), e['id']))
        transcripts = [e for e in records if e['kind'] == 'transcript']
        events = [e for e in records if e['kind'] != 'transcript']
        item = {**session,
            'transcripts': write(f'{index:03d}.transcripts.jsonl', ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in transcripts)),
            'events': write(f'{index:03d}.events.jsonl', ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events)),
            'documents': []}
        for number, document in enumerate(material.get('source_documents', {}).get(session['id'], [])):
            saved = write(f'{index:03d}.document-{number:03d}.json', document['content'])
            item['documents'].append({**saved, 'kind': document['kind'], 'name': document['name']})
        sessions.append(item)
    manifest = {'schema_version': 1, 'scope': material['scope'], 'room_id': material['room_id'],
        'sessions': sessions, 'files': files,
        'usage': ('按需读整场文件。优先用歌曲时间线与歌词查明歌曲是否出现；不能据分类标签独断现场演唱。以transcripts/events的id引用原文。'
                  if material.get('analysis_contract') else
                  '按需读整场文件。歌曲表仅定位，歌词仅识别曲目；不能据分类标签独断现场演唱。以transcripts/events的id引用原文。')}
    path = root / 'manifest.json'
    if not path.exists() and verify_only:
        raise ValueError('冻结整场来源清单缺失')
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError('冻结整场来源清单已变化')
    if not verify_only:
        atomic_json(path, manifest)
    return path


def verify_sources(directory, material):
    if 'source_evidence' not in material:
        return
    # 从已冻结material重建预期字节，不能通过同时篡改清单与文件绕过校验。
    freeze_sources(directory, material, verify_only=True)


def attach_session_files(session, workspace):
    """将本场现有歌曲表/歌词/在线原稿冻结入材料；不读取配置或凭据。"""
    workspace = Path(workspace)
    config_path = workspace / 'config.json'
    if config_path.is_file():
        binding = json.loads(config_path.read_text())
        check_room(binding, session['room_id'])
        if binding.get('start_utc') and epoch(binding['start_utc']) != epoch(session['start']):
            raise ValueError('整场证据来源属于其他场次')
    docs = []
    for name, kind in [('public/snapshot.json', 'song_activity'),
                       ('verified_songs.json', 'song_registry')]:
        path = workspace / name
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                check_room(data, session['room_id'])
            if kind == 'song_activity':
                if data.get('session_start') and epoch(data['session_start']) != epoch(session['start']):
                    raise ValueError('歌曲表属于其他场次')
                data = {key: data.get(key) for key in ('activity', 'activity_summary', 'session_start', 'updated_at')}
            docs.append({'kind': kind, 'name': name, 'content': data})
    for path in sorted((workspace / 'song_sources').glob('*.json')):
        docs.append({'kind': 'song_identity_and_lyrics', 'name': path.name,
                     'content': json.loads(path.read_text())})
    session['documents'] = docs
    return session
