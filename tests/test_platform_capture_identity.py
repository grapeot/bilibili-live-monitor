from unittest.mock import AsyncMock

from test_platform import fixture

from bilibili_live_monitor.platform import Candidate


async def test_launch_preserves_canonical_candidate_when_page_redirects(tmp_path):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='123456')])
    candidate = next(iter(platform.candidates.values()))
    candidate.update(room_id='123456', live_status=1)
    platform.monitor.add = AsyncMock(return_value={'room_id': '654321', 'recorded_seconds': 0})
    await platform.launch(candidate)
    assert candidate['room_id'] == '123456'
    assert platform.active[candidate['url']] == '654321'
    assert candidate['last_error'] is None
    platform.store.close()
