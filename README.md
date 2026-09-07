# YouTube 网页端 中文字幕插件

给 YouTube 视频自动叠加中文字幕：
- 视频本身有字幕（人工上传或 YouTube 自动生成）→ 插件直接抓取并翻译
- 视频完全没有字幕 → 调用本地跑的 Whisper 做语音识别，再翻译

架构：浏览器插件只负责显示，语音识别和翻译这些重活丢给本机跑的 Python 服务，
不需要买/租云服务器，仅供自己电脑本地使用。插件的 `content.js` 直接 fetch 本地
后端（manifest 的 `host_permissions` 已经放行跨域），没有走 service worker 中转——
Whisper 转录动辄几分钟，service worker 闲置太久会被 Chrome 强制杀掉，中转会导致
请求中途被打断。

## 安装步骤

### 1. 启动本地后端

```bash
cd backend
pip install -r requirements.txt
python server.py
```

第一次做语音识别时会自动下载 Whisper 模型权重，会比较慢，之后就快了。

音频转码用的 ffmpeg/ffprobe 由 `static-ffmpeg` 这个包提供（`pip install` 一起装好，
首次用到时自动下载对应平台的二进制），不需要自己单独装 ffmpeg 或改系统 PATH。

Whisper 模型大小在 `backend/server.py` 的 `get_whisper_model(size="small")` 里配置，
从小到大依次是 `tiny` / `base` / `small` / `medium` / `large`：模型越大识别越准但越慢，
越小则相反。默认用 `small`，兼顾速度和可用的准确率；纯 CPU 跑长视频建议用更小的档位。

#### 可选：用 NVIDIA GPU 加速

默认装的是 CPU 版 torch，长视频转录会很慢。有 NVIDIA 显卡的话换成 CUDA 版能快一个数量级：

```bash
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cu128
python -c "import torch; print(torch.cuda.is_available())"  # 打印 True 就是装对了
```

`cu128` 对应 CUDA 12.8，具体版本号以 [pytorch.org](https://pytorch.org) 当前给出的安装命令为准。

> `server.py` 里内置了一个规避设置（关掉 flash / memory-efficient 这两个注意力后端），
> 用来绕开新版 torch 在 GPU 上和 whisper 的一个已知兼容性报错，速度会略打折扣但换来稳定。

### 2. 加载浏览器插件

1. 打开 Chrome，访问 `chrome://extensions`
2. 打开右上角"开发者模式"(Developer mode)
3. 点击"加载已解压的扩展程序"(Load unpacked)，选择 `extension` 文件夹

### 3. 使用

打开任意 YouTube 视频，插件会自动尝试加载中文字幕，可以点插件图标看本地服务是否连接成功。

- **没有字幕、要跑语音识别时**：字幕悬浮层会先显示实时进度，比如"字幕识别中...42%"，
  跑完切到"翻译中..."，再自动切换成真正的中文字幕。进度来自 Whisper 内部按音频帧数
  汇报的解码进度，每 2 秒轮询一次。
- **结果会缓存**：语音识别结果按视频 ID 存在 `backend/.cache/transcripts/`，同一个视频
  不会重复下载音频、重新跑 Whisper（刷新页面/重新打开也直接用缓存，基本秒开）。想强制
  重新识别的话，删掉对应的缓存文件（或整个 `.cache` 目录）即可。

## 已知局限

- VTT 解析是简化版本，遇到复杂样式标签可能有瑕疵
- 翻译用的是免费的 Google 翻译接口，长视频（几百段字幕）逐句调用可能会被限流或变慢，
  可以考虑合并成批量翻译，或换成大模型 API 做上下文感知翻译；这一步目前没有进度显示。
  遇到限流时会自动重试几次，还不行就退回显示原文，不会再把 Google 的错误页内容当成字幕显示出来
- 字幕轨道选择逻辑比较简单：优先选英语字幕（其中再优先人工字幕），没有英语的话就
  随便挑一条（同样优先人工字幕）
- 识别任务状态存在进程内存里（`_jobs` 字典），重启后端会丢失所有进行中任务的进度
  （已完成的结果因为落了缓存文件，不受影响）

## 注意事项

- 后端只监听 127.0.0.1，不会暴露给外网
- 只有本机跑着 `server.py` 的时候插件才能工作
- 下载/转写他人视频内容涉及版权和平台条款，仅建议用于个人学习场景
