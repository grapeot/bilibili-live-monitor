import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '.local')))
    cdp_url: str = field(default_factory=lambda: os.getenv('CDP_URL', 'http://127.0.0.1:9222'))
    asr_python: str = field(default_factory=lambda: os.getenv('ASR_PYTHON', '.venvs/asr/bin/python'))
    asr_model: str = field(default_factory=lambda: os.getenv('ASR_MODEL', 'Qwen/Qwen3-ASR-1.7B'))
    chunk_seconds: int = field(default_factory=lambda: int(os.getenv('CHUNK_SECONDS', '30')))
    max_rooms: int = field(default_factory=lambda: int(os.getenv('MAX_ROOMS', '10')))
    queue_size: int = field(default_factory=lambda: int(os.getenv('QUEUE_SIZE', '20')))
    silence_dbfs: float = field(default_factory=lambda: float(os.getenv('SILENCE_DBFS', '-55')))
    vad_enabled: bool = field(default_factory=lambda: os.getenv('VAD_ENABLED', '1').lower() in ('1', 'true', 'yes'))
    vad_model_path: str = field(default_factory=lambda: os.getenv('VAD_MODEL_PATH', '.local/models/silero_vad.onnx'))
    vad_threshold: float = field(default_factory=lambda: float(os.getenv('VAD_THRESHOLD', '0.5')))
    vad_min_speech_seconds: float = field(default_factory=lambda: float(os.getenv('VAD_MIN_SPEECH_SECONDS', '0.3')))
    root_path: str = field(default_factory=lambda: os.getenv('APP_ROOT_PATH', '').rstrip('/'))
    max_reconnects: int = field(default_factory=lambda: int(os.getenv('MAX_RECONNECTS', '5')))
    retry_delay: float = field(default_factory=lambda: float(os.getenv('RETRY_DELAY', '2')))
    asr_start_timeout: float = 30
    asr_infer_timeout: float = 30

    def __post_init__(self):
        if not 5 <= self.chunk_seconds <= 60 or self.max_rooms < 1 or self.queue_size < 1:
            raise ValueError('配置越界：切片5～60秒，房间与队列上限至少为1')
        self.data_dir = self.data_dir.resolve()
        if self.max_reconnects < 1 or self.retry_delay < 0:
            raise ValueError('连续重连次数至少为1，退避时间不得为负')
        if not 0 < self.vad_threshold < 1 or not 0 <= self.vad_min_speech_seconds <= 5:
            raise ValueError('VAD概率须在0和1之间，最短语音须在0～5秒之间')


def load_settings():
    load_dotenv()
    return Settings()
