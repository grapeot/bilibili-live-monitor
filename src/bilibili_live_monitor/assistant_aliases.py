"""可回查的称呼关系：原文关联、依赖版本和待复核状态。"""
import json
import re


def alias_view(store, record, seen=None):
    result = dict(record)
    seen = set() if seen is None else set(seen)
    key = record['alias'].casefold()
    valid = record.get('status') == 'confirmed' and bool(record.get('relation_evidence'))
    if key in seen:
        valid = False
    seen.add(key)
    for ref in record.get('evidence', []):
        current = store.evidence(ref['id'])
        if not current or current['version'] != ref['version']:
            valid = False
    for dependency in record.get('dependencies', []):
        row = store.db.execute('SELECT data FROM aliases WHERE alias_key=?', (dependency['alias_key'],)).fetchone()
        parent = json.loads(row[0]) if row else None
        if (not parent or parent['version'] != dependency['version'] or parent['alias'].casefold() in seen
                or alias_view(store, parent, seen)['status'] != 'confirmed'):
            valid = False
    if not valid:
        result['status'] = 'pending_review' if record.get('status') != 'unconfirmed' else 'unconfirmed'
    return result


def aliases(store, uid=None, *, include_candidates=False):
    rows = store.db.execute('SELECT data FROM aliases' + (' WHERE uid=?' if uid else ''), (uid,) if uid else ())
    result = [alias_view(store, json.loads(r[0])) for r in rows]
    if include_candidates:
        rows = store.db.execute('SELECT data FROM alias_candidates' + (' WHERE uid=?' if uid else ''),
                                (uid,) if uid else ())
        result += [json.loads(r[0]) for r in rows]
    return sorted(result, key=lambda a: (a['alias'].casefold(), a['uid'], a.get('status', '')))


def trusted_names(store, uid):
    return {a['alias'] for a in aliases(store, uid) if a['status'] == 'confirmed'}


def relationship(store, uid, alias, identities, relations, quote=''):
    """只接受明确自称或已知称呼与新称呼的直接肯定关联；歧义不升级为事实。"""
    names = {e['data'].get('author') for e in identities if e['data'].get('sender_uid') == uid
             and e['data'].get('author')}
    parents = [a for a in aliases(store, uid) if a['status'] == 'confirmed' and a['alias'] != alias]
    other_names = {r[0] for r in store.db.execute("SELECT DISTINCT json_extract(data,'$.author') FROM evidence "
                   "WHERE json_extract(data,'$.sender_uid') != ?", (uid,)) if r[0]}
    names.difference_update(other_names)
    names.update(a['alias'] for a in parents)

    def compact(value):
        # 仅允许已使用的修订标签；不能任意截去主语、宾语或逗号后的尾文。
        value = re.sub(r'^\s*修订后\s*[:：]\s*', '', value)
        return re.sub(r'[\s，,：:]', '', value).rstrip('。！？!?；;.').casefold()

    def links(left, right, text):
        left, right = re.escape(compact(left)), re.escape(compact(right))
        return bool(re.fullmatch(left + r'(?:以后)?(?:叫你|叫作|叫|也叫|又名|昵称是|就是|是|iscalled|isalsoknownas|is)' + right, text)
                    or re.fullmatch(right + r'(?:就是|是|也叫|又名|is)' + left, text))

    def self_name(text):
        return bool(re.fullmatch(
            r'(?:大家好|你好)?(?:我叫|我是|叫我|以后叫我|我的昵称是|mynameis|callme|iam|i.m)'
            + re.escape(compact(alias)), text))

    for ev in relations:
        text = ev['data'].get('text', '')
        if quote and quote not in text:
            continue
        for fragment in re.finditer(r'([^。！？!?；;\n]+)([。！？!?；;\n]|$)', text):
            sentence = fragment[1]
            if fragment[2] in ('?', '？'):
                continue
            if (quote and quote.rstrip('。！？!?；;\n') not in sentence) or alias not in sentence:
                continue
            normalized = compact(sentence)
            if re.search(r'不是|并非|不叫|别叫|不要叫|是否|可能|好像|也许|大概|请问|吗$|isnot|isn.t|\bnot\b', normalized):
                continue
            if any(links(other, alias, normalized) for other in other_names):
                continue
            excerpt = quote or sentence.strip()
            if len(excerpt) > 600:
                continue  # 请提供短关系摘录，避免单条关系撑破工作索引。
            proof_text = compact(excerpt)
            # 摘录与完整原句必须各自证明同一关系，截取一个看似正确的前缀不能授权。
            self_claim = ev['data'].get('sender_uid') == uid and self_name(normalized) and self_name(proof_text)
            linked = next((name for name in sorted(names) if len(name) >= 2
                           and links(name, alias, normalized) and links(name, alias, proof_text)), None)
            if self_claim or linked:
                dependencies = [{'alias_key': a['alias'].casefold(), 'version': a['version']}
                                for a in parents if a['alias'] == linked]
                return {'quote': excerpt, 'relation_kind': 'self_claim' if self_claim else 'named_relation',
                        'dependencies': dependencies, 'relation_id': ev['id']}
    return None
