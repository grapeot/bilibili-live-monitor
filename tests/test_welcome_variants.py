import json
import random

import pytest

from bilibili_live_monitor.welcome_variants import choose_variant, load_variants, variant_messages


def pool():
    return load_variants({'welcome_variants': [
        {'id': str(i), 'info': f'合成信息{i}。', 'cta': f'合成引导{i}哦。'} for i in range(5)]})


def test_shuffle_rounds_never_repeat_and_survive_serialization():
    variants, state, rng = pool(), {}, random.Random(123)
    draws = []
    for i in range(30):
        chosen = choose_variant(variants, state, shuffle=rng.shuffle)
        draws.append(chosen['id'])
        assert chosen['info'].endswith(chosen['id']+'。')
        state = json.loads(json.dumps(state))  # 同一状态可跨进程恢复。
    assert all(a != b for a, b in zip(draws, draws[1:]))
    assert all(len(set(draws[i:i+5])) == 5 for i in range(0, 30, 5))


def test_changed_copy_resets_bag_and_single_variant_works():
    variants, state = pool(), {}
    choose_variant(variants, state, shuffle=lambda xs: None)
    variants = [{'id': '0', 'info': '新信息', 'cta': '新引导'}]
    assert choose_variant(variants, state)['info'] == '新信息'
    assert choose_variant(variants, state)['id'] == '0'


def test_rooms_have_independent_bags():
    a, b = {}, {}
    choose_variant(pool(), a, shuffle=lambda xs: None)
    assert choose_variant(pool(), b, shuffle=lambda xs: None)['id'] == '0'


def test_templates_and_explicit_three_messages():
    variants = load_variants({'copy_values': {'hours': '上午十点、下午四点'},
        'welcome_variants': [{'id': 'hours', 'info': '每天{hours}开播哦。', 'cta': '点个关注哦。'}]})
    messages = variant_messages(['访客'], 80, variants[0], '欢迎{names}～')
    assert messages == ['欢迎访客～', '每天上午十点、下午四点开播哦。', '点个关注哦。']
    with pytest.raises(ValueError):
        variant_messages(['访客'], 10, variants[0], '欢迎{names}～')


@pytest.mark.parametrize('bad', [None, {}, [{'id': 'a', 'info': '', 'cta': 'x'}],
    [{'id': 'a', 'info': '{missing}', 'cta': 'x'}],
    [{'id': 'a', 'info': 'x', 'cta': 'y'}, {'id': 'a', 'info': 'x', 'cta': 'y'}]])
def test_invalid_pool_rejected(bad):
    with pytest.raises(ValueError):
        load_variants({'welcome_variants': bad})
