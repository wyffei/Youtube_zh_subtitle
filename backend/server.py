"""
本地后端服务 —— 配合浏览器插件使用，只监听 127.0.0.1，不对外网暴露。

启动方式：
    pip install -r requirements.txt
    python server.py

看电影前启动一下，看完 Ctrl+C 关掉就行，不需要一直挂着，也不需要买云服务器。
"""

import json
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import static_ffmpeg
import torch
import tqdm
from flask import Flask, jsonify, request
from flask_cors import CORS
from deep_translator import GoogleTranslator

app = Flask(__name__)
CORS(app)  # 允许插件跨域访问本地服务

# yt-dlp 转码、whisper 读取音频都要调用 ffmpeg/ffprobe。用 static_ffmpeg 提供的独立二进制，
# 只把它的目录加进当前进程的 PATH（不写入系统/用户环境变量），不依赖系统是否装过 ffmpeg
static_ffmpeg.add_paths()

# 新版 torch 在 GPU 上默认用 flash/memory-efficient 的 SDPA 后端，遇到某些静音片段
# 会给 whisper 老代码算出 0 长度张量导致 reshape 报错，强制退回朴素实现规避这个问题
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)

_whisper_model = None  # 懒加载，第一次真正用到才加载模型

# 语音识别结果按视频 ID 缓存到本地文件，避免刷新页面/重复打开同一视频时
# 重新跑一遍耗时的 Whisper 转录（下载音频+识别本身不便宜，结果不会变，值得存下来）
CACHE_DIR = Path(__file__).parent / ".cache" / "transcripts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_video_id(url):
    match = re.search(r"[?&]v=([^&]+)", url)
    return match.group(1) if match else None


# /transcribe 跑一遍长视频动辄十几分钟，改成"提交任务 + 轮询进度"，
# 避免插件那边一直挂着一个长 HTTP 请求（另外也方便中途看到百分比）
_jobs = {}  # job_id -> {"status": "running"|"done"|"error", "progress": 0, "segments": [...], "error": "..."}
_jobs_lock = threading.Lock()
_progress_local = threading.local()  # 每个转录任务在自己的线程里跑，用它记录当前 job_id


class _ProgressTqdm(tqdm.tqdm):
    """whisper 内部用 tqdm 按帧数汇报解码进度，这里拦下来写进对应 job 的状态里，
    不需要动 whisper 自己的代码"""

    def update(self, n=1):
        self.n = getattr(self, "n", 0) + n
        job_id = getattr(_progress_local, "job_id", None)
        if job_id and self.total:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    job["progress"] = min(99, round(self.n / self.total * 100))


tqdm.tqdm = _ProgressTqdm


_whisper_lock = threading.Lock()  # 每个 /transcribe/start 请求都在自己的线程里跑，
# 而 whisper 的模型实例不是设计成能被多个线程同时调用 transcribe() 的，
# 用一把全局锁把"同时开两个识别任务"的情况串行化，避免并发访问搞坏内部状态


def get_whisper_model(size="small"):
    global _whisper_model
    if _whisper_model is None:
        import whisper
        print(f"加载 Whisper 模型（{size}），第一次会比较慢，请耐心等待...")
        _whisper_model = whisper.load_model(size)
    return _whisper_model


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _run_transcribe_job(job_id, url):
    _progress_local.job_id = job_id  # 让 _ProgressTqdm 知道这个线程里的进度该记到哪个 job
    try:
        video_id = get_video_id(url)
        cache_path = CACHE_DIR / f"{video_id}.json" if video_id else None
        if cache_path and cache_path.exists():
            print(f"[缓存命中] {video_id} 之前识别过，直接用缓存结果")
            segments = json.loads(cache_path.read_text(encoding="utf-8"))["segments"]
        else:
            with tempfile.TemporaryDirectory() as tmp:
                audio_path = Path(tmp) / "audio.mp3"
                subprocess.run(
                    ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(audio_path), url],
                    check=True,
                )
                with _whisper_lock:
                    model = get_whisper_model()
                    result = model.transcribe(str(audio_path))
                segments = [
                    {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
                    for s in result["segments"]
                ]
            if cache_path:
                cache_path.write_text(
                    json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8"
                )

        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "progress": 100, "segments": segments}
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "progress": 0, "error": str(e)}


@app.route("/transcribe/start")
def transcribe_start():
    """视频没有任何字幕时，后台开始下载音频 + Whisper 识别，立刻返回 job_id，
    识别本身可能要跑很久，交给轮询 /transcribe/status 去看进度"""
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": 0}
    threading.Thread(target=_run_transcribe_job, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/transcribe/status")
def transcribe_status():
    job_id = request.args.get("job_id")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


def _looks_like_translate_error_page(text):
    """deep_translator 底层是刮 Google 翻译网页版，遇到限流/报错时不一定抛异常，
    而是把错误页面上刮到的文字当成"翻译结果"原样返回，这里识别一下这种情况"""
    if not text:
        return True
    lowered = text.lower()
    return "that's an error" in lowered or "server error" in lowered


def _translate_with_retry(translator, text, retries=3, delay=0.5):
    for attempt in range(retries):
        try:
            result = translator.translate(text)
        except Exception:
            result = None
        if result and not _looks_like_translate_error_page(result):
            return result
        time.sleep(delay)
    return text  # 重试几次还是不行，退回原文，至少不是错误页乱码


@app.route("/translate", methods=["POST"])
def translate():
    """把一批 {start, end, text} 翻译成中文，时间戳原样返回"""
    data = request.get_json()
    segments = data.get("segments", [])
    translator = GoogleTranslator(source="auto", target="zh-CN")

    translated = []
    for seg in segments:
        zh_text = _translate_with_retry(translator, seg["text"])
        translated.append({**seg, "text": zh_text})

    return jsonify({"segments": translated})


if __name__ == "__main__":
    # 只监听本机，不对局域网/公网开放；threaded=True 是为了让轮询 /transcribe/status
    # 的请求不会被同时进行的其他请求（比如翻译）卡住
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
