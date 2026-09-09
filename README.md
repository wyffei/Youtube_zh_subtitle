# YouTube 中文字幕插件

为 YouTube 视频自动叠加中文字幕。浏览器插件只负责渲染显示，字幕获取、语音识别、
翻译均由本机运行的 Python 后端完成。

## 功能特点

- **有字幕轨道**：通过 `yt-dlp` 抓取原始字幕
- **无字幕轨道**：下载音频，用 Whisper 分块转写
- **流式翻译**：并发调用 DeepSeek API，译好一句显示一句，无需等待整批完成
- **本地缓存**：识别结果、翻译结果按视频 ID 缓存，避免重复消耗算力和 API 额度
- **进度可视化**：播放器右上角显示识别、翻译两个独立的进度角标

## 目录

- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [设计要点](#设计要点)
- [已知局限](#已知局限)
- [注意事项](#注意事项)

## 系统架构

```
YouTube 页面
  ├─ inject.js   读取播放器实例当前的字幕轨道列表
  └─ content.js    决定走哪条路径，渲染字幕 / 进度角标
        │ 直接 fetch，不经过 service worker
        ▼
  本地后端 server.py（127.0.0.1:8000，Flask）
        ├─ 有字幕轨道 → /captions   用 yt-dlp 抓取字幕文件
        ├─ 无字幕轨道 → /transcribe 下载音频 → 分块 → Whisper 逐块识别
        └─ 拿到原文后 → /translate  并发调用 DeepSeek API 翻译成中文
```

`/captions`、`/transcribe`、`/translate` 三个接口均采用"提交任务 + 轮询"模式：
`xxx/start` 立即返回 `job_id`，插件每隔 1.5~2 秒轮询一次 `xxx/status`，获取当前
进度和已产出的结果，无需等整个任务跑完才有响应。


## 快速开始

### 1. 启动本地后端

```bash
cd backend
pip install -r requirements.txt
python server.py
```

首次进行语音识别时会自动下载 Whisper 模型权重，耗时较长，之后会快很多。

> yt-dlp / Whisper 处理音频需要 ffmpeg / ffprobe，这两个可执行文件由
> `static-ffmpeg` 这个 pip 包提供，不会单独安装 ffmpeg 到系统或修改系统 PATH。

### 2. 配置 DeepSeek 翻译 API

翻译调用的是 [DeepSeek 官方 API](https://platform.deepseek.com)
（`deepseek-v4-flash` 模型），按量计费，新注册账号有一次性免费额度。前往官网注册、
申请 API Key，**在启动后端之前**将其设为环境变量：

```powershell
# Windows / PowerShell，仅在当前终端窗口生效，关闭窗口后需重新设置
$env:DEEPSEEK_API_KEY = "sk-你的key"
python server.py
```

不想每次开新终端都重新设置的话，可在 Windows"系统属性 → 环境变量"中添加一条
永久生效的用户环境变量。未设置该环境变量时，翻译请求会报错并提示先申请 Key。

实测一部两小时的电影，翻译总花费约 2 元人民币。

### 3.（可选）使用 NVIDIA GPU 加速

默认安装的是 CPU 版 torch，可替换为 CUDA 版：

```bash
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cu128
python -c "import torch; print(torch.cuda.is_available())"  # 打印 True 即为安装成功
```

`cu128` 对应 CUDA 12.8，具体版本号以 [pytorch.org](https://pytorch.org) 当前给出的
安装命令为准。

> `server.py` 内置了一项规避设置（关闭 flash / memory-efficient 两种注意力后端），
> 用于绕开新版 torch 在 GPU 上与 Whisper 的一个已知兼容性报错，速度会略有下降但
> 换来运行稳定。

### 4. 加载浏览器插件

1. 打开 Chrome，访问 `chrome://extensions`
2. 打开右上角"开发者模式"（Developer mode）
3. 点击"加载已解压的扩展程序"（Load unpacked），选择 `extension` 文件夹

### 5. 使用

打开任意 YouTube 视频，插件会自动尝试加载中文字幕，可点击插件图标查看本地服务
是否连接成功。具体走哪条路径、进度如何显示、结果如何缓存，参见
[系统架构](#系统架构)。

### 6. 结束使用

**停止后端**：回到运行 `python server.py` 的终端窗口，按 `Ctrl+C` 即可，无需
一直挂着——不用时关闭，下次使用再启动。若终端窗口已关闭、不确定服务是否还在
运行，可按端口号（8000）查出对应进程并结束：

```powershell
# Windows / PowerShell
netstat -ano | findstr :8000     # 最后一列为 PID
taskkill /PID <上面查到的PID> /F
```

**停用/卸载插件**：前往 `chrome://extensions`，找到"YouTube 中文字幕"卡片——
右上角开关为临时禁用（保留安装，可随时重新启用）；卡片上的"移除"（Remove）按钮
为彻底卸载。

## 配置项

以下配置项均在 `backend/server.py` 中修改：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无（需自行设置环境变量） | DeepSeek API Key，未设置时翻译请求会报错 |
| `get_whisper_model(size=...)` | `small` | Whisper 模型规格，可选 `tiny` / `base` / `small` / `medium` / `large`，模型越大识别越准但越慢；纯 CPU 跑长视频建议选更小的档位 |
| `CHUNK_SECONDS` | `45` | 音频分块时长（秒），越小首批字幕出现越快，但 Whisper 调用次数增多，且更容易在块边界切断句子 |
| `TRANSLATE_CONCURRENCY` | `6` | 翻译请求并发数，全局共享一个线程池 |

### 设计要点

<details>
<summary><b>content.js 为何绕开 service worker 直接请求后端</b></summary>

Manifest V3 的 background service worker 闲置一段时间、或单次任务耗时过长会被
Chrome 强制终止，而 Whisper 转录常需十几分钟，经 service worker 转发的请求会在
运行过程中被中断。manifest 的 `host_permissions` 已将访问本地后端的跨域权限直接
授权给内容脚本，因此 `content.js` 可以绕开 service worker，不受其生命周期限制。

</details>

<details>
<summary><b>字幕轨道存在与否的判断依据</b></summary>

`inject.js` 运行在页面自身的 JS 世界中，读取的是 YouTube 播放器实例**当前**的
实时数据（`#movie_player` 的 `getPlayerResponse()`）；页面初次加载时写入的全局
变量 `ytInitialPlayerResponse` 仅作兜底——该变量只在整页刷新时写入当前视频数据，
站内跳转到下一视频（非整页刷新）时不会更新，若直接使用会读到上一视频的残留数据，
误判"当前视频没有字幕"。

</details>

<details>
<summary><b>有字幕时为何由后端抓取，而非浏览器直接请求</b></summary>

插件在浏览器中直接 fetch YouTube 字幕文件常被判定为爬虫请求（返回 200 但内容为
空——YouTube 播放器自身发出的同款请求能拿到真实内容，插件发出的则不能，大概率是
按请求来源做的区分）。因此改为由后端使用 `yt-dlp` 抓取（参数
`--skip-download --write-subs --write-auto-subs`，与下方下载音频用的是同一工具），
比浏览器直接请求慢几秒，但更稳定。若这一步抓到的内容仍为空，会自动回退到语音识别，
不会卡住。

</details>

<details>
<summary><b>无字幕时的分块识别与流水线翻译</b></summary>

音频下载后不会整段一次性丢给 Whisper，而是按 `CHUNK_SECONDS`（默认 45 秒，配置于
`backend/server.py`）切块推进：一块识别完成即立即送去翻译，无需等整部视频识别完
才开始翻译第一句——识别与翻译两条流水线并行，字幕出现得更早。块切得越小，首批字幕
出现越快，但 Whisper 调用次数增多，且块边界处偶尔会切断句子，识别准确率略有下降。

无论走 `/captions` 还是 `/transcribe`，下载/抓取时都附加了 `--no-playlist`：若当前
视频是从播放列表中点开的，地址栏会带上 `&list=...` 参数，不加此参数 `yt-dlp` 会将
整个播放列表当作任务处理。

</details>

<details>
<summary><b>翻译并发与流式显示</b></summary>

翻译请求并发发出，而非逐句等待，并发数由 `backend/server.py` 中的
`TRANSLATE_CONCURRENCY` 配置（默认 6，全局共享同一线程池，因此不会因识别分块而
导致多批翻译任务叠加、把总并发数顶到"块数 × 6"）。每翻完一句即存储并显示，无需
等整批翻译完成才能看到第一条结果。

</details>

<details>
<summary><b>缓存机制</b></summary>

- 语音识别结果按视频 ID 缓存在 `backend/.cache/transcripts/`，翻译结果按视频 ID
  缓存在 `backend/.cache/translations/`（且是逐句存储，而非等整批完成才写入）。
  同一视频不会重复下载音频、重新运行 Whisper，也不会重新调用 DeepSeek 消耗
  token；翻译中途被打断（页面刷新 / 后端重启）也不会丢失进度，下次会自动跳过
  已翻译部分，接着处理剩余内容。
- 翻译缓存以"该句字幕在完整视频中的绝对下标"为键（识别分块推进、翻译分批提交，
  但下标是连续累加的，并非每批各自从 0 开始编号），隐含假设是同一视频每次抓取到
  的原文字幕行数与顺序一致——正常情况下确实如此。
- 抓取现成字幕这一步**没有缓存**，每次打开都会重新调用一次 `yt-dlp`。
- 如需强制重新识别/翻译，删除对应缓存文件（或整个 `.cache` 目录）即可。

</details>

## 已知局限

- VTT 解析是简化版本，遇到复杂样式标签可能有瑕疵。
- 翻译改用 DeepSeek API 后按量计费，长视频（几百上千段字幕）会产生相应量级的
  token 消耗；调用失败会自动重试几次，仍失败则回退显示英文原文，不会将报错内容
  当作字幕显示。
- 需自行申请并配置 `DEEPSEEK_API_KEY`（见[配置 DeepSeek 翻译 API](#2-配置-deepseek-翻译-api)），
  未配置时翻译步骤会直接报错，字幕会停留在"翻译中...0%"。
- 翻译缓存下标对应关系的隐含假设（同一视频每次抓取到的原文字幕行数、顺序一致）
  一旦被打破——例如 YouTube 后台重新生成了该视频的自动字幕导致行数变化——理论上
  可能出现缓存错位、翻译与原文对不上的情况，概率很低，遇到时删除对应缓存文件
  重新翻译即可。
- 字幕轨道选择逻辑：优先选择人工字幕中的英语字幕，没有则随机选择一条，其次才是
  自动生成字幕。
- 识别/翻译任务状态保存在进程内存中（`_jobs` / `_translate_jobs` / `_caption_jobs`
  字典），重启后端会丢失所有进行中任务的进度（已完成的结果因落有缓存文件，不受
  影响）。
- 音频分块识别在块边界处可能切断句子，导致该处识别准确率略有下降；每块各自进行
  语言检测，理论上存在极小概率某块被误判为其他语言。

## 注意事项

- 后端仅监听 `127.0.0.1`，不会暴露给外网。
- 仅在本机运行 `server.py` 期间，插件才能正常工作。
- 下载/转写他人视频内容涉及版权和平台条款，仅建议用于个人学习场景。
