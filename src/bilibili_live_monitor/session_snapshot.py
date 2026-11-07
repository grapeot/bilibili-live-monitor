"""共用单场快照组装与文件写出；阶段、取数和发布由调用者决定。"""
from .content_mode import apply_content_mode
from .live_share import atomic_json, make_snapshot, render_snapshot
from .time_utils import iso


def build_snapshot(source, config, *, cutoff, phase, health, annotations, approved_songs):
    snapshot = make_snapshot(
        source, start=config['start_utc'], now=iso(cutoff), label=config['label'],
        phase=phase, health=health, annotations=annotations, approved_songs=approved_songs,
        followup_overrides=config.get('followup_overrides', {}),
        followup_rules=config.get('followup_rules'))
    apply_content_mode(snapshot, config)
    return snapshot


def write_page(snapshot, destination, *, navigation=None, temporary_suffix='.tmp'):
    page = destination / 'index.html'
    temporary = page.with_suffix(temporary_suffix)
    temporary.write_text(render_snapshot(snapshot, navigation=navigation))
    temporary.replace(page)


def write_snapshot(snapshot, destination, *, navigation=None, temporary_suffix='.tmp'):
    destination.mkdir(exist_ok=True)
    write_page(snapshot, destination, navigation=navigation, temporary_suffix=temporary_suffix)
    atomic_json(destination / 'snapshot.json', snapshot)
