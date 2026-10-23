"""独立解释器运行，stdout 专用于 JSONL。"""
import contextlib
import json
import os
import sys
import time


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def main():
    with contextlib.redirect_stdout(sys.stderr):
        from mlx_qwen3_asr import load_model, transcribe
        model, _ = load_model(os.getenv('ASR_MODEL', 'Qwen/Qwen3-ASR-1.7B'))
    emit({'ready': True})
    for line in sys.stdin:
        request = json.loads(line)
        start = time.monotonic()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = transcribe(request['wav_path'], model=model, verbose=False)
            emit({'req_id': request['req_id'], 'text': result.text,
                  'infer_ms': round((time.monotonic() - start) * 1000),
                  'truncated': result.truncated, 'error': None})
        except Exception as error:
            emit({'req_id': request['req_id'], 'error': f'{type(error).__name__}: {error}'})


if __name__ == '__main__':
    main()
