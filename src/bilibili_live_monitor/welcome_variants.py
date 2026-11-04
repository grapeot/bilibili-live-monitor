"""按房间持久洗牌，信息与CTA作为一组，不临时生成外发文案。"""
import hashlib
import json
import random

from .welcome import text_length, welcome_greeting, welcome_messages

COPY_FIELDS = ('greeting', 'intro', 'welcome_variants', 'copy_values')


def validate_copy(config):
    greeting = config.get('greeting', '欢迎 {names} {suffix}')
    intro = config.get('intro')
    if not isinstance(greeting, str) or '{names}' not in greeting:
        raise ValueError('欢迎模板必须包含{names}')
    if not isinstance(intro, str) or not intro.strip():
        raise ValueError('介绍文案不能为空')
    try:
        greeting.format(names='访客', suffix='来到直播间～')
    except (KeyError, ValueError, IndexError, AttributeError) as error:
        raise ValueError('欢迎模板只支持{names}和{suffix}') from error
    variants = load_variants(config)
    if len(variants) > 20:
        raise ValueError('随机文案最多20组')
    if variants:
        for variant in variants:
            variant_messages(['访客'], 80, variant, greeting)
    else:
        welcome_messages(['访客'], 80, intro=intro, greeting_template=greeting)
    return variants


def load_variants(config):
    variants = config.get('welcome_variants', [])
    if not isinstance(variants, list):
        raise ValueError('welcome_variants必须是列表')
    values = config.get('copy_values', {})
    if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
        raise ValueError('copy_values必须是字符串映射')
    result, ids = [], set()
    for item in variants:
        if not isinstance(item, dict):
            raise ValueError('文案组必须是对象')
        key = item.get('id')
        if not isinstance(key, str) or not key.strip() or key in ids:
            raise ValueError('文案组id必须非空且唯一')
        messages = []
        for field in ('info', 'cta'):
            text = item.get(field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError('每组必须有非空info和cta')
            try:
                text = text.format_map(values).strip()
            except (KeyError, ValueError, IndexError, AttributeError) as error:
                raise ValueError('文案占位符缺少有效配置') from error
            if not text:
                raise ValueError('展开后的文案不能为空')
            messages.append(text)
        ids.add(key)
        result.append({'id': key, 'info': messages[0], 'cta': messages[1]})
    return result


def variant_messages(names, limit, variant, greeting_template):
    # 复用昵称裁剪，但信息和CTA各为独立消息，不按标点自动拆散。
    greeting = welcome_greeting(names, limit, greeting_template)
    texts = [greeting, variant['info'], variant['cta']]
    if any(not text or text_length(text) > limit for text in texts):
        raise ValueError('文案组超出页面字数限制')
    return texts


def choose_variant(variants, state, *, shuffle=None):
    """预留下一组；调用方对确定未发送的失败回滚，未知发送仍阻断。"""
    fingerprint = hashlib.sha256(json.dumps(variants, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    ids = [item['id'] for item in variants]
    if not ids:
        raise ValueError('文案池不能为空')
    previous = state.get('last')
    if state.get('fingerprint') != fingerprint:
        state.update(fingerprint=fingerprint, remaining=[])
    remaining = state.get('remaining', [])
    if not remaining:
        remaining = ids.copy()
        (shuffle or random.shuffle)(remaining)
        if len(remaining) > 1 and remaining[0] == previous:
            remaining[0], remaining[1] = remaining[1], remaining[0]
        state['remaining'] = remaining
    key = remaining.pop(0)
    state['last'] = key
    return next(item for item in variants if item['id'] == key)
