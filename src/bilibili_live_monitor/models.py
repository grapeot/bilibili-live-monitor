"""面向调用者的响应契约，供 OpenAPI 导出。"""
from typing import Literal

from pydantic import BaseModel, Field

SegmentStatus = Literal['queued', 'transcribing', 'done', 'empty', 'silent', 'truncated', 'error', 'cancelled']


class SegmentResponse(BaseModel):
    room_id: str = Field(description='实际房间号', examples=['123456'])
    seq: int = Field(ge=1, description='该房间跨会话递增的音频段序号')
    start_utc: str = Field(description='本段采集开始时间，带时区ISO8601')
    end_utc: str = Field(description='本段音频结束时间，带时区ISO8601')
    duration_seconds: float = Field(ge=0, description='解码音频时长，秒')
    wall_seconds: float | None = Field(default=None, description='本段实际墙钟采集时长，旧记录可能为空')
    coverage_ratio: float | None = Field(default=None, description='音频时长除以墙钟时长；旧记录可能为空')
    media_advanced: float | None = Field(default=None, description='播放器实际前进的媒体秒数，旧记录可能为空')
    missing_samples: int | None = Field(default=None, description='音频线程没有输入的样本数，旧记录可能为空')
    dropped_samples: int | None = Field(default=None, description='页面缓冲过载丢失样本数，旧记录可能为空')
    pauses: int | None = Field(default=None, description='本段捕获到的播放器暂停事件次数')
    track_muted_ms: float | None = Field(default=None, description='源音轨mute累计毫秒，不是播放器静音状态')
    muted: bool = Field(description='播放器是否静音')
    paused: bool = Field(description='播放器是否暂停，正常采集时为false')
    track_live: bool = Field(description='本段提交时音轨是否有效')
    rms_dbfs: float = Field(description='解码后音频均方根电平，dBFS；不是语音置信度')
    queue_wait_ms: int = Field(ge=0, description='排队等待毫秒，不含模型首次启动')
    infer_ms: int = Field(ge=0, description='模型推理毫秒，不含首次加载')
    status: SegmentStatus = Field(description='处理状态；empty表示模型未输出文本')
    text: str = Field(description='模型原始文字，未做语义编辑')
    error: str | None = Field(description='错误原因，正常时为null')


class RoomResponse(BaseModel):
    record_mode: Literal['none', 'audio', 'video'] = 'audio'
    asr_enabled: bool = False
    preview_enabled: bool = False
    recording_status: str = 'disabled'
    recorded_seconds: float = 0
    recording_files: int = 0
    recording_bytes: int = 0
    room_id: str = Field(description='解析重定向后的实际房间号', examples=['123456'])
    url: str = Field(description='实际房间HTTPS链接')
    title: str = Field(description='直播页面标题')
    cover_url: str = Field(description='静态封面或头像URL，缺失时为空字符串')
    chunk_seconds: int = Field(ge=5, le=60, description='目标切片时长，秒')
    status: Literal['capturing', 'reconnecting', 'stopped', 'error'] = Field(description='房间采集生命周期状态')
    error: str | None = Field(description='采集错误，正常时为null')
    muted: bool = Field(description='是否要求播放器静音')
    cycle_notice: bool = Field(description='是否存在周期重置的潜在采集间隙')
    updated_at: str = Field(description='状态更新时间，UTC ISO8601')


class GiftDetails(BaseModel):
    event_type: Literal['SEND_GIFT', 'SEND_GIFT_V2', 'GUARD_BUY', 'SUPER_CHAT_MESSAGE']
    event_id: str | None = None
    time_source: Literal['event', 'received', 'unknown'] = 'unknown'
    event_at_utc: str | None = Field(default=None, description='协议事件时间；缺失时at_utc仅用接收时钟显示，不伪造事件时间')
    dedup_basis: Literal['event_id', 'event_fields', 'payload_hash']
    sender_uid: str | None = Field(default=None, description='协议提供的UID，可能缺失或脱敏；不是保证稳定的观众身份')
    gift_id: str | None = None
    gift_name: str
    num: int = Field(ge=1)
    coin_type: str | None = None
    price: int | None = Field(default=None, ge=0, description='平台原始price字段，不擅自换算人民币或当作净收入')
    total_coin: int | None = Field(default=None, ge=0, description='SEND_GIFT原始总币数，金币/银币需分开')
    batch_index: int | None = Field(default=None, ge=0, description='V2多结果批次中的索引，不丢弃盲盒每个结果')
    blind_gift: dict | None = Field(default=None, description='原始盲盒信息；结果礼物标价不代表实际付费金额')
    guard_level: int | None = None
    guard_price_gold: int | None = Field(default=None, ge=0, description='GUARD_BUY.price原值，单位金瓜子；不与礼物单价混用，不乘num')
    price_yuan: int | None = Field(default=None, ge=0, description='仅醒目留言的协议人民币元金额')


class TimelineItem(BaseModel):
    id: int
    room_id: str
    kind: Literal['transcript', 'danmaku', 'gift']
    at_utc: str
    received_utc: str
    text: str = ''
    author: str | None = None
    source_key: str | None = None
    gift: GiftDetails | None = None
    seq: int | None = None
    status: SegmentStatus | None = None
    error: str | None = None


class RoomState(RoomResponse):
    segments: list[SegmentResponse] = Field(description='该房间最近20条切片，按序号升序')
    items: list[TimelineItem] = Field(default_factory=list, description='最近80条弹幕、礼物与转写，按事件时间升序')


class Inflight(BaseModel):
    room_id: str = Field(description='正在推理的房间号')
    seq: int = Field(description='正在推理的切片序号')


class StateResponse(BaseModel):
    asr_enabled: bool = False
    record_mode: Literal['none', 'audio', 'video'] = 'audio'
    preview_enabled: bool = False
    rooms: list[RoomState] = Field(description='历史和当前监控房间')
    queue_depth: int = Field(ge=0, description='等待识别的段数，不包含正在推理的段')
    queue_capacity: int = Field(ge=1, description='等待队列容量上限')
    asr_ready: bool = Field(description='ASR常驻进程是否已加载模型')
    overload_count: int = Field(ge=0, description='本次服务启动以来丢弃的过载段数')
    inflight: Inflight | None = Field(description='正在推理的段；空闲时为null')
    cursor: int = Field(ge=0, description='此快照对应的最新持久事件ID')
    active_count: int = Field(ge=0, description='占用名额的任务数，包含采集中和重连中')
    pending_count: int = Field(ge=0, description='正在添加的房间数，同样占用名额')
    asr_restarts: int = Field(ge=0, description='模型传输失败后的自动重启次数')
    asr_retries: int = Field(ge=0, description='同一音频段自动重试次数，包含识别错误与进程错误')
    asr_error: str | None = Field(description='最近的模型启动或通信错误；成功启动后清空')


class HealthResponse(BaseModel):
    asr_enabled: bool = False
    ok: bool = Field(description='HTTP服务是否可响应，不代表所有直播均正常')
    asr_ready: bool = Field(description='ASR模型是否已就绪')
    queue_depth: int = Field(ge=0, description='等待识别段数')


class CandidateResponse(BaseModel):
    url: str = Field(description='页面发现的直播间链接，不保证正在播放')
    title: str = Field(description='页面链接的文字')
