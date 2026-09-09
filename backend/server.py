"""
本地后端服务 —— 配合浏览器插件使用，只监听 127.0.0.1，不对外网暴露。

启动方式：
    pip install -r requirements.txt
    python server.py

看电影前启动一下，看完 Ctrl+C 关掉就行，不需要一直挂着，也不需要买云服务器。
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import static_ffmpeg
import torch
import tqdm
from flask import Flask, jsonify, request
from flask_cors import CORS

# DeepSeek 官方 API，按量计费，新注册账号有一次性免费额度；Key 从环境变量读取，
# 不写进代码/配置文件，就不会被意外提交到 Git 里
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"

# 逐句调用改成并发发出去，而不是一句等完再等下一句，能把翻译时间压缩到接近
# 1/TRANSLATE_CONCURRENCY；数字调太大容易触发 DeepSeek 账号的限流（表现为更多重试/
# 偶尔退回原文），6 是比较保守够用的起点
TRANSLATE_CONCURRENCY = 6

# 用同一个 Session 复用底层 TCP/TLS 连接，而不是每次请求都重新握手一次；
# pool_maxsize 留够并发数用，避免并发请求抢连接池排队
_http_session = requests.Session()
_http_session.mount(
    "https://", requests.adapters.HTTPAdapter(pool_maxsize=TRANSLATE_CONCURRENCY)
)

# 识别现在按音频块推进，一块识别完就可能有好几个 /translate/start 同时在跑
# （每块各自一个翻译 job），如果每个 job 各开一个 ThreadPoolExecutor，实际并发数会
# 变成"块数 × TRANSLATE_CONCURRENCY"，失去限流的意义；改成全局唯一一个执行器，
# 所有翻译任务共用同一份并发额度，跟只有一个 job 时的限流效果一致
_translate_executor = ThreadPoolExecutor(max_workers=TRANSLATE_CONCURRENCY)

# 音频切块大小（秒）。识别改成一块一块地做，一块识别完就能立刻把这块交给翻译，
# 不用等整段视频识别完才开始翻第一句字幕；块切得越小，第一批字幕出现得越快，
# 但 whisper 调用次数变多、块边界处的识别准确率也会略打折扣。45 秒是个折中的经验值——
# 之前设成 120 秒时，几分钟的短视频整个音频还凑不够一块，切块等于没切
CHUNK_SECONDS = 45

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

# 翻译结果同样按视频 ID 缓存：DeepSeek 是按量计费的，同一个视频重复打开
# 不应该重新花钱翻一遍
TRANSLATE_CACHE_DIR = Path(__file__).parent / ".cache" / "translations"
TRANSLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


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
    不需要动 whisper 自己的代码。现在识别是按块跑的，这里汇报的是"当前块内部"的解码
    进度，要结合 chunk_index/total_chunks 才能算出整段视频的总体进度"""

    def update(self, n=1):
        self.n = getattr(self, "n", 0) + n
        job_id = getattr(_progress_local, "job_id", None)
        if job_id and self.total:
            chunk_index = getattr(_progress_local, "chunk_index", 0)
            total_chunks = getattr(_progress_local, "total_chunks", 1)
            overall_fraction = (chunk_index + self.n / self.total) / total_chunks
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    job["progress"] = min(99, round(overall_fraction * 100))


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


def _split_audio_into_chunks(audio_path, out_dir):
    """按 CHUNK_SECONDS 切块。-c copy 只是重新封装、不重新编码，切分本身几乎不耗时，
    不会在下载完的基础上再额外花多少时间"""
    pattern = str(out_dir / "chunk_%04d.mp3")
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-i", str(audio_path),
            "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
            "-c", "copy", "-reset_timestamps", "1",
            pattern,
        ],
        check=True,
    )
    return sorted(out_dir.glob("chunk_*.mp3"))


def _probe_duration(path):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(probe.stdout.strip())


def _run_transcribe_job(job_id, url):
    _progress_local.job_id = job_id  # 让 _ProgressTqdm 知道这个线程里的进度该记到哪个 job
    try:
        video_id = get_video_id(url)
        cache_path = CACHE_DIR / f"{video_id}.json" if video_id else None
        if cache_path and cache_path.exists():
            print(f"[缓存命中] {video_id} 之前识别过，直接用缓存结果")
            segments = json.loads(cache_path.read_text(encoding="utf-8"))["segments"]
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "progress": 100, "segments": segments}
            return

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            audio_path = tmp_path / "audio.mp3"
            t0 = time.time()
            # url 是浏览器地址栏的完整 href：如果这条视频是从播放列表里点进去看的，
            # 地址栏会带上 &list=...&index=... 参数，yt-dlp 看到 list 参数默认会把
            # 整个播放列表都下载下来（观察到过真实案例：想识别 1 条视频结果拉了
            # 一个 300+ 条的列表），--no-playlist 强制只处理链接直接指向的这一条
            subprocess.run(
                [
                    "yt-dlp", "--no-playlist", "-x", "--audio-format", "mp3",
                    "-o", str(audio_path), url,
                ],
                check=True,
            )
            print(f"[计时] 下载音频耗时 {time.time() - t0:.1f} 秒")

            chunk_paths = _split_audio_into_chunks(audio_path, tmp_path)
            total_chunks = len(chunk_paths) or 1
            _progress_local.total_chunks = total_chunks

            # 一块一块识别：识别完一块就立刻把目前累积到的全部 segments 写进 job，
            # 插件那边一旦看到 segments 变长，就会把新出现的部分立刻送去翻译，
            # 不用等这里的 for 循环整个跑完
            segments = []
            offset = 0.0
            t1 = time.time()
            for idx, chunk_path in enumerate(chunk_paths):
                _progress_local.chunk_index = idx
                with _whisper_lock:
                    model = get_whisper_model()
                    result = model.transcribe(str(chunk_path))
                for s in result["segments"]:
                    text = s["text"].strip()
                    if text:
                        segments.append(
                            {"start": s["start"] + offset, "end": s["end"] + offset, "text": text}
                        )
                offset += _probe_duration(chunk_path)

                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job:
                        job["segments"] = list(segments)
                        job["progress"] = min(99, round((idx + 1) / total_chunks * 100))
            print(f"[计时] Whisper 识别耗时 {time.time() - t1:.1f} 秒（共 {total_chunks} 块）")

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
        _jobs[job_id] = {"status": "running", "progress": 0, "segments": []}
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


_caption_jobs = {}  # job_id -> {"status": "running"|"done"|"error", "vtt": "...", "error": "..."}
_caption_jobs_lock = threading.Lock()


def _run_captions_job(job_id, url, lang):
    """视频本身有字幕时，插件以前是直接在浏览器里 fetch YouTube 的字幕文件，
    但这条请求经常被当成爬虫拒绝——返回 200 但内容是空的（YouTube 播放器自己发的
    同一个请求能拿到真实内容，插件发的就不行，大概率是靠请求来源的
    Sec-Fetch-*/来源信息之类的标记做区分）。改成用 yt-dlp 来抓：它是专门维护、
    持续跟进 YouTube 反爬变化的项目，比自己猜怎么绕过靠谱得多。--skip-download
    只拉字幕文件，不下载音视频本体，比识别那条路径快得多"""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_template = str(tmp_path / "sub")
            t0 = time.time()
            subprocess.run(
                [
                    "yt-dlp", "--no-playlist", "--skip-download",
                    "--write-subs", "--write-auto-subs",
                    "--sub-langs", lang or "en",
                    "--sub-format", "vtt",
                    "-o", out_template,
                    url,
                ],
                check=True,
            )
            print(f"[计时] 抓字幕耗时 {time.time() - t0:.1f} 秒")
            vtt_files = list(tmp_path.glob("*.vtt"))
            vtt_text = vtt_files[0].read_text(encoding="utf-8") if vtt_files else ""

        with _caption_jobs_lock:
            _caption_jobs[job_id] = {"status": "done", "vtt": vtt_text}
    except Exception as e:
        with _caption_jobs_lock:
            _caption_jobs[job_id] = {"status": "error", "error": str(e)}


@app.route("/captions/start")
def captions_start():
    url = request.args.get("url")
    lang = request.args.get("lang") or "en"
    if not url:
        return jsonify({"error": "missing url"}), 400

    job_id = str(uuid.uuid4())
    with _caption_jobs_lock:
        _caption_jobs[job_id] = {"status": "running"}
    threading.Thread(target=_run_captions_job, args=(job_id, url, lang), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/captions/status")
def captions_status():
    job_id = request.args.get("job_id")
    with _caption_jobs_lock:
        job = _caption_jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


def _translate_with_deepseek(text, retries=3, delay=1.0):
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "没有设置 DEEPSEEK_API_KEY 环境变量，去 https://platform.deepseek.com 申请一个 API Key 再启动服务"
        )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是专业的字幕翻译。把用户输入的一句台词翻译成简体中文，"
                "只输出译文本身，不要加引号、不要解释、不要保留原文。",
            },
            {"role": "user", "content": text},
        ],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            resp = _http_session.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            zh_text = resp.json()["choices"][0]["message"]["content"].strip()
            if zh_text:
                return zh_text
        except Exception:
            pass
        time.sleep(delay)
    return text  # 重试几次还是不行，退回原文，至少不是空白


_translate_jobs = {}  # job_id -> {"status": ..., "progress": 0, "segments": [已翻好的部分], "error": "..."}
_translate_jobs_lock = threading.Lock()
_translate_cache_lock = threading.Lock()  # 保护翻译缓存文件的读改写，避免上一个还没跑完
# 的任务和刷新页面后的新任务同时写同一个文件


def _load_translate_cache(cache_path):
    """读取某个视频目前为止已经翻好的部分，key 是原始字幕在这一批里的下标"""
    if not cache_path or not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return {int(k): v for k, v in data.get("translated", {}).items()}
    except Exception:
        return {}


def _save_translate_cache_entry(cache_path, index, segment):
    """翻完一句就存一句，而不是等整批翻完才写，这样翻译中途被打断（比如页面被刷新）
    时已经翻完的部分不会丢，下次直接跳过、只接着翻剩下的"""
    if not cache_path:
        return
    with _translate_cache_lock:
        data = {}
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        translated = data.get("translated", {})
        translated[str(index)] = segment
        data["translated"] = translated
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _run_translate_job(job_id, segments, video_id=None, offset=0):
    """并发翻译一批字幕里还没翻过的部分（而不是一句等完再等下一句），每翻完一句就把
    目前为止的结果写回 job 并存进磁盘缓存。插件那边轮询到的是"持续变长的已翻译列表"，
    可以边看边显示。字幕是按时间戳查找显示的（不依赖数组顺序），所以哪句先并发翻完
    就先放进去，完全没问题。

    offset 是这批 segments 在"完整这一个视频的字幕列表"里的起始下标——识别现在是
    分块推进的，同一个视频会分好几批（每块一批）提交到这里翻译，offset 让每批各自
    算出的下标能对上完整字幕里的绝对位置，不然不同块各自从 0 开始编号，会在磁盘缓存
    里互相覆盖对方已经翻好的结果"""
    cache_path = TRANSLATE_CACHE_DIR / f"{video_id}.json" if video_id else None
    cached = _load_translate_cache(cache_path)

    results = [None] * len(segments)
    pending_indices = []
    for i in range(len(segments)):
        if (offset + i) in cached:
            results[i] = cached[offset + i]
        else:
            pending_indices.append(i)

    done_count = len(segments) - len(pending_indices)
    if done_count:
        print(f"[翻译缓存命中] {video_id} 这批已经翻好 {done_count}/{len(segments)} 句，接着翻剩下的")
    with _translate_jobs_lock:
        job = _translate_jobs.get(job_id)
        if job:
            job["segments"] = [s for s in results if s is not None]
            job["progress"] = round(done_count / len(segments) * 100)

    try:
        if pending_indices:
            # 用全局共享的执行器而不是在这里新开一个：识别现在按块推进，一个视频短
            # 时间内可能有好几个块各自对应一个翻译 job 同时在跑，如果每个 job 各开
            # 一个线程池，总并发数会变成"同时在跑的块数 × TRANSLATE_CONCURRENCY"，
            # 失去限流本来的意义
            future_to_index = {
                _translate_executor.submit(_translate_with_deepseek, segments[i]["text"]): i
                for i in pending_indices
            }
            for future in as_completed(future_to_index):
                i = future_to_index[future]
                # 保留原文在 "text" 里，中文译文单独放 "zh"，这样插件那边能同时
                # 显示中英双语字幕，而不是覆盖掉原文
                seg = {**segments[i], "zh": future.result()}
                results[i] = seg
                done_count += 1
                _save_translate_cache_entry(cache_path, offset + i, seg)
                with _translate_jobs_lock:
                    job = _translate_jobs.get(job_id)
                    if job:
                        job["segments"] = [s for s in results if s is not None]
                        job["progress"] = round(done_count / len(segments) * 100)

        with _translate_jobs_lock:
            _translate_jobs[job_id] = {"status": "done", "progress": 100, "segments": results}
    except Exception as e:
        with _translate_jobs_lock:
            _translate_jobs[job_id] = {"status": "error", "progress": 0, "error": str(e)}


@app.route("/translate/start", methods=["POST"])
def translate_start():
    """把一批 {start, end, text} 提交去翻译，立刻返回 job_id，
    翻译本身跟 /transcribe/start 一样放到后台线程里跑，交给轮询 /translate/status 去看进度和已翻好的部分"""
    data = request.get_json()
    segments = data.get("segments", [])
    video_id = data.get("video_id")
    offset = int(data.get("offset", 0))
    if not segments:
        return jsonify({"error": "missing segments"}), 400

    job_id = str(uuid.uuid4())
    with _translate_jobs_lock:
        _translate_jobs[job_id] = {"status": "running", "progress": 0, "segments": []}
    threading.Thread(
        target=_run_translate_job, args=(job_id, segments, video_id, offset), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/translate/status")
def translate_status():
    job_id = request.args.get("job_id")
    with _translate_jobs_lock:
        job = _translate_jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


if __name__ == "__main__":
    # 只监听本机，不对局域网/公网开放；threaded=True 是为了让轮询 /transcribe/status
    # 的请求不会被同时进行的其他请求（比如翻译）卡住
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
