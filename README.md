# YouTube 网页端 无字幕轨道视频 加中文字幕插件

给 YouTube 视频自动叠加中文字幕：
- 视频本身有字幕（人工上传或 YouTube 自动生成）→ 插件直接抓取并翻译
- 视频完全没有字幕 → 调用本地跑的 Whisper 做语音识别，再翻译

架构：浏览器插件只负责显示，语音识别和翻译交给本机运行的 Python 服务。

> 插件内部，内容脚本 `content.js` 直接 `fetch` 本地后端，不经过 Manifest V3 的
> background service worker 中转。

> 原因：Chrome 会在 service worker 闲置或单次任务
> 运行过久后将其终止，而 Whisper 转录常需十几分钟，经 service worker 转发的请求会
> 在运行过程中被中断。manifest 的 `host_permissions` 已授予内容脚本访问本地后端的
> 跨域权限，因此 `content.js` 可以绕开 service worker，不受其生命周期限制。

## 使用步骤

### 1. 启动本地后端

```bash
cd backend
pip install -r requirements.txt
python server.py
```

第一次做语音识别时会自动下载 Whisper 模型权重，会比较慢，之后就快了。

yt-dlp/whisper 处理音频需要用到 ffmpeg/ffprobe，这两个可执行文件由 `static-ffmpeg`
这个 pip 包提供，不单独装 ffmpeg（不装到系统里，也不改系统全局的 PATH）。

Whisper 模型大小在 `backend/server.py` 的 `get_whisper_model(size="small")` 里配置，
从小到大依次是 `tiny` / `base` / `small` / `medium` / `large`：模型越大识别越准但越慢，
越小则相反。默认用 `small`，兼顾速度和可用的准确率；纯 CPU 跑长视频建议用更小的档位。

#### 可选：用 NVIDIA GPU 加速

默认是 CPU 版 torch，可换成 CUDA 版：

```bash
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cu128
python -c "import torch; print(torch.cuda.is_available())"  # 打印 True 就是装对了
```

`cu128` 对应 CUDA 12.8，具体版本号以 [pytorch.org](https://pytorch.org) 当前给出的安装命令为准。

> `server.py` 里内置了一个规避设置（关掉 flash / memory-efficient 这两个注意力后端），
> 用来绕开新版 torch 在 GPU 上和 whisper 的一个已知兼容性报错，速度会略打折扣但换来稳定。

#### 配置 DeepSeek 翻译 API

翻译调用的是 [DeepSeek 官方 API](https://platform.deepseek.com)（`deepseek-v4-flash` 模型），按量计费，
新注册账号有一次性免费额度。去官网注册、申请一个 API Key，然后在**启动后端之前**把它设成环境变量：

```powershell
# Windows / PowerShell，只在当前终端窗口生效，关掉窗口要重新设
$env:DEEPSEEK_API_KEY = "sk-你的key"
python server.py
```

不想每次开新终端都重新设置的话，可以在 Windows"系统属性 → 环境变量"里加一条永久生效的用户环境变量。
Key 只存在你自己电脑的环境变量里，不会写进代码或提交到 Git。没设置这个环境变量的话，翻译请求会报错提示先去申请。

实测一部两小时的电影，翻译下来大概 2 块钱人民币，耗时大概 20 分钟左右（并发调用，见下）。

翻译是并发调用 DeepSeek（而不是一句等完再等下一句），并发数在 `backend/server.py` 的
`TRANSLATE_CONCURRENCY` 里配置，默认 6。调大能进一步缩短翻译时间，但太大容易触发
DeepSeek 账号自己的限流（表现为更多重试、偶尔退回英文原文），6 是比较保守的起点。

### 2. 加载浏览器插件

1. 打开 Chrome，访问 `chrome://extensions`
2. 打开右上角"开发者模式"(Developer mode)
3. 点击"加载已解压的扩展程序"(Load unpacked)，选择 `extension` 文件夹

### 3. 使用

打开任意 YouTube 视频，插件会自动尝试加载中文字幕，可以点插件图标看本地服务是否连接成功。

- **没有字幕、要跑语音识别时**：字幕悬浮层会先显示实时进度，比如"字幕识别中...42%"
  （来自 Whisper 内部按音频帧数汇报的解码进度，每 2 秒轮询一次）；识别完开始翻译后，
  播放器右上角会出现一个独立的小角标显示"翻译中...N%"，一旦翻好第一批就立刻开始
  显示真正的中文字幕，角标继续留在右上角汇报后面部分的翻译进度，直到全部翻完自动
  消失——不用等整部视频翻完才能看，也不会因为字幕已经开始显示就看不到进度了。
- **结果会缓存，而且能续译**：语音识别结果按视频 ID 存在 `backend/.cache/transcripts/`，
  翻译结果同样按视频 ID 存在 `backend/.cache/translations/`，而且是每翻完一句就存一句
  （不是等整批翻完才存）。同一个视频不会重复下载音频、重新跑 Whisper，也不会重新调用
  DeepSeek 花 token（刷新页面/重新打开都直接用缓存，基本秒开）；如果翻译跑到一半页面
  被刷新或者后端被重启，已经翻完的部分也不会丢，下次会自动跳过、接着翻剩下的。想强制
  重新识别/翻译的话，删掉对应的缓存文件（或整个 `.cache` 目录）即可。

### 4. 结束使用

**停止后端**：回到跑 `python server.py` 的那个终端窗口，按 `Ctrl+C` 就行，不需要
一直挂着——不用的时候关掉，下次要用再开一下即可。如果终端窗口已经关掉、不确定
服务还在不在跑，可以按端口号（8000）查出对应进程再结束掉：

```powershell
# Windows / PowerShell
netstat -ano | findstr :8000     # 最后一列是 PID
taskkill /PID <上面查到的PID> /F
```

**停用/卸载插件**：去 `chrome://extensions`，找到"YouTube 中文字幕"这张卡片——
右上角的开关是临时禁用（保留安装，随时能重新打开）；卡片上的"移除"(Remove) 按钮
是彻底卸载。

## 已知局限

- VTT 解析是简化版本，遇到复杂样式标签可能有瑕疵
- 翻译改用 DeepSeek API 后是按量计费，长视频
  （几百上千段字幕）会产生对应量级的 token 消耗；调用失败会自动重试几次，重试还不行
  就退回显示英文原文，不会显示报错内容当成字幕
- 需要自己申请并配置 `DEEPSEEK_API_KEY`（见上文"配置 DeepSeek 翻译 API"），没配置
  的话翻译这一步会直接报错，字幕会一直停在"翻译中...0%"
- 翻译缓存按"字幕在这一批里的下标"对应，隐含假设同一个视频每次抓到的原文字幕
  行数、顺序都一样。正常情况下（人工/自动字幕轨道、Whisper 转录结果本身也有缓存）
  确实如此，但如果 YouTube 后台重新生成了这条视频的自动字幕导致行数变化，理论上
  可能出现缓存对错行、翻译对不上原文的情况——概率很低，遇到了删掉对应缓存文件重翻即可
- 字幕轨道选择逻辑：优先人工字幕中的英语字幕，没有的话就随便挑一条，其次是自动生成字幕。
- 识别任务状态存在进程内存里（`_jobs` 字典），重启后端会丢失所有进行中任务的进度
  （已完成的结果因为落了缓存文件，不受影响）

## 注意事项

- 后端只监听 127.0.0.1，不会暴露给外网
- 只有本机跑着 `server.py` 的时候插件才能工作
- 下载/转写他人视频内容涉及版权和平台条款，仅建议用于个人学习场景
