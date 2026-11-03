"""有证据、绑定原稿指纹的私有转写校正；不改原文和公开快照。"""
import hashlib
import json
from pathlib import Path

from .live_share import fingerprint, followup_evidence
from .song_sources import source_matches


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def correction_context(workspace, source):
    workspace = Path(workspace)
    witnesses = {row['id']: row['text'] for row in followup_evidence(source)}
    catalog = []
    root = (workspace / 'song_sources').resolve()
    entries = _read(workspace / 'verified_songs.json', [])
    for entry in entries if isinstance(entries, list) else []:
        try:
            path = (root / entry['file']).resolve()
            index = entry['result_index']
            if path.parent != root or path.suffix != '.json' or type(index) is not int or index < 0:
                continue
            result = _read(path, {})['data']['results'][index]
            if not source_matches(entry['title'], result):
                continue
            ref = f"s:{entry['file']}:{index}"
            body = result.get('raw_content') or result['markdown']
            witnesses[ref] = result['title'] + '\n' + body
            catalog.append({'id': ref, 'title': entry['title'], 'url': result['url'], 'file': str(path)})
        except (KeyError, TypeError, IndexError, ValueError, OSError):
            continue
    document = _read(workspace / 'transcript_corrections.json', {})
    rows = document.get('corrections', []) if isinstance(document, dict) else []
    segments = {s['seq']: s for s in source['segments'] if s.get('is_final') and s.get('text')}
    active, rejected = {}, []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            rejected.append({'reason': 'invalid_entry'})
            continue
        seq = row.get('seq')
        segment = segments.get(seq) if type(seq) is int else None
        reason = None
        if segment is None or row.get('fingerprint') != fingerprint(segment):
            reason = 'stale_or_missing_source'
        elif (not isinstance(row.get('corrected_text'), str) or not row['corrected_text'].strip()
              or len(row['corrected_text']) > 20000 or row['corrected_text'] == segment['text']
              or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            reason = 'invalid_correction'
        else:
            refs = row.get('evidence')
            if not isinstance(refs, list) or not 1 <= len(refs) <= 6:
                reason = 'missing_evidence'
            else:
                for ref in refs:
                    if not isinstance(ref, dict):
                        reason = 'invalid_evidence'
                        break
                    ref_id, quote = ref.get('id'), ref.get('quote')
                    text = witnesses.get(ref_id) if isinstance(ref_id, str) else None
                    if (not isinstance(text, str) or not isinstance(quote, str) or not quote.strip()
                            or ''.join(quote.split()) not in ''.join(text.split())):
                        reason = 'unverified_evidence'
                        break
        if reason:
            rejected.append({'seq': seq, 'reason': reason})
            continue
        ident = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        active[seq] = {'corrected_text': row['corrected_text'], 'correction_id': ident,
                       'correction_reason': row['reason'], 'correction_evidence': row['evidence']}
    return active, {'sources': catalog, 'rejected': rejected}


def review_window(segment, corrections):
    return {'seq': segment['seq'], 'start': segment['start_utc'], 'end': segment['end_utc'],
            'text': segment['text'], 'fingerprint': fingerprint(segment),
            **corrections.get(segment['seq'], {})}


def correction_prompt(workspace):
    return f'''新查实的歌词、歌名或明确报幕，可顺手回溯本场旧ASR错字；不只复核最新窗口。
先读私有 {workspace / 'transcript_corrections.json'}（不存在按空文件），在完整上下文中定位受影响的窗口。
只做有原文依据的局部纠错，不补写漏听内容、不用完整歌词替代实际转写，不改写说话人的事实、立场或语气，不把语义推断改写成原话。
校正另存上述文件，原子合并并保留其他项及人工修订；禁止改数据库、原始媒体、离线ASR和annotations里的原文指纹。
格式为{{"corrections":[{{"seq":1,"fingerprint":"原稿指纹","corrected_text":"局部改字后的本窗口全文","reason":"简短纠错依据","evidence":[{{"id":"本场t:/m:证据ID或correction_context.sources的s:ID","quote":"对应证据中的原文短句"}}]}}]}}。
证据ID及quote必须实际存在；公开来源只用已登记song_sources，先登记来源再引用；本场弹幕只支持其明确给出的拼写，不能替代说话人未说过的内容。
text始终为原始ASR，corrected_text是独立校正版。两者一起核对；指纹变化的旧校正暂不采用，应对新原稿重新查证，不能按seq盲目迁移。
input.correction_context.rejected列出未采用项；校正影响旧判断时重新复核该窗口。在reviews中原样带回输入的correction_id（没有则省略），表示基于该校正版完成复核；不可伪造。
纠错只进入私有文件与后续AI输入，不能向公开快照增加原文、校正文、证据、人名关联或调查笔记。
'''
