// content.js —— 运行在插件的隔离世界，负责：
// 1. 注入 inject.js 去读取页面的字幕轨道信息
// 2. 没有字幕时，直接请求本地服务器做语音识别
// 3. 把字幕翻译成中文，渲染成悬浮层跟着视频时间走
//
// 直接在这里 fetch 本地服务器（而不是转发给 background.js 的 service worker）：
// manifest 的 host_permissions 已经允许跨域，而 service worker 闲置/跑太久会被
// Chrome 强制杀掉，Whisper 转录动辄几分钟，用它中转会导致请求中途被打断。

const BACKEND = "http://127.0.0.1:8000";

// 识别一个长视频动辄十几分钟，后端把它做成"提交任务 + 轮询进度"，而且现在是
// 按音频块一块一块推进的：job.segments 会随着识别推进持续变长（不用等整段视频识别
// 完才有值），这里每 2 秒查一次状态，把完整的 job（进度 + 目前累积到的 segments）
// 回调出去，交给调用方决定新出现的部分要不要立刻送去翻译
async function transcribeViaBackend(videoUrl, onUpdate) {
  const startRes = await fetch(`${BACKEND}/transcribe/start?url=${encodeURIComponent(videoUrl)}`);
  if (!startRes.ok) throw new Error(`transcribe start failed: ${startRes.status}`);
  const { job_id } = await startRes.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const statusRes = await fetch(`${BACKEND}/transcribe/status?job_id=${job_id}`);
    if (!statusRes.ok) throw new Error(`transcribe status failed: ${statusRes.status}`);
    const job = await statusRes.json();
    if (job.status === "error") throw new Error(job.error || "transcribe failed");
    onUpdate?.(job);
    if (job.status === "done") return;
  }
}

// 翻译同样做成"提交任务 + 轮询"，后端边翻边把已完成的部分写回 job，
// 这里每 1.5 秒查一次，拿到的是持续变长的已翻译列表，可以边看边显示，
// 不用等几百句字幕全部翻完才能看到第一条。
//
// offset 是这批 segments 在"完整这一个视频的字幕列表"里的起始下标——识别现在是
// 分块推进的，同一个视频会分好几批（每块一批）各自调这个函数，offset 让每批各自
// 算出的下标能对上完整字幕里的绝对位置，不然不同块各自从 0 开始编号，会在后端的
// 磁盘缓存里互相覆盖对方已经翻好的结果
async function translateViaBackend(segments, videoId, onUpdate, offset = 0) {
  const startRes = await fetch(`${BACKEND}/translate/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segments, video_id: videoId, offset }),
  });
  if (!startRes.ok) throw new Error(`translate start failed: ${startRes.status}`);
  const { job_id } = await startRes.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const statusRes = await fetch(`${BACKEND}/translate/status?job_id=${job_id}`);
    if (!statusRes.ok) throw new Error(`translate status failed: ${statusRes.status}`);
    const job = await statusRes.json();
    if (job.status === "error") throw new Error(job.error || "translate failed");
    onUpdate?.(job);
    if (job.status === "done") return job.segments;
  }
}

let currentSegments = []; // [{start, end, text}]，已经是中文
let overlayEl = null;
let lastVideoId = null;
let subtitlesReady = false; // 字幕就绪前，悬浮层用来显示加载/识别进度，不能被同步循环覆盖

function getVideoId() {
  const url = new URL(location.href);
  return url.searchParams.get("v");
}

function injectPageScript() {
  const s = document.createElement("script");
  s.src = chrome.runtime.getURL("inject.js");
  (document.head || document.documentElement).appendChild(s);
  s.remove();
}

function ensureOverlay() {
  if (overlayEl && document.body.contains(overlayEl)) return overlayEl;
  const player = document.querySelector("#movie_player");
  if (!player) return null;
  overlayEl = document.createElement("div");
  overlayEl.id = "yt-zh-sub-overlay";
  player.appendChild(overlayEl);
  return overlayEl;
}

// 进度角标容器，识别和翻译两条流水线可能同时在跑，各自占一个角标（比如
// "识别中...40%" 和 "翻译中...15%" 同时挂在右上角），用 key 区分、独立增删
let progressContainerEl = null;
const progressBadges = {}; // key -> 对应的角标元素

function ensureProgressContainer() {
  if (progressContainerEl && document.body.contains(progressContainerEl)) return progressContainerEl;
  const player = document.querySelector("#movie_player");
  if (!player) return null;
  progressContainerEl = document.createElement("div");
  progressContainerEl.id = "yt-zh-sub-progress";
  player.appendChild(progressContainerEl);
  return progressContainerEl;
}

function setProgressBadge(key, text) {
  const container = ensureProgressContainer();
  if (!container) return;
  let el = progressBadges[key];
  if (!el || !container.contains(el)) {
    el = document.createElement("div");
    progressBadges[key] = el;
    container.appendChild(el);
  }
  el.textContent = text;
}

function removeProgressBadge(key) {
  progressBadges[key]?.remove();
  delete progressBadges[key];
}

function removeAllProgressBadges() {
  Object.keys(progressBadges).forEach(removeProgressBadge);
  progressContainerEl?.remove();
  progressContainerEl = null;
}

function findCurrentText(t) {
  // 分段数量不大，线性查找足够用；中英双语显示，zh 是翻译，text 是原文
  for (const seg of currentSegments) {
    if (t >= seg.start && t <= seg.end) {
      return seg.zh ? `${seg.text}\n${seg.zh}` : seg.text;
    }
  }
  return "";
}

let syncBound = false;
function startSyncLoop() {
  const video = document.querySelector("video");
  if (!video || syncBound) return;
  syncBound = true;
  video.addEventListener("timeupdate", () => {
    if (!subtitlesReady) return;
    const el = ensureOverlay();
    if (!el) return;
    const text = findCurrentText(video.currentTime);
    el.textContent = text;
    el.hidden = !text; // 没有字幕的时间段不显示空的黑框
  });
}

// 以前是在插件里直接 fetch YouTube 自己的字幕文件（track.baseUrl + "&fmt=vtt"），
// 但这条请求经常被 YouTube 当成爬虫拒绝——返回 200 但内容是空的（YouTube 播放器
// 自己发的同款请求能拿到真实内容，插件发的就不行，大概率是靠请求来源的
// Sec-Fetch-*/来源信息做区分）。改成让后端用 yt-dlp 去抓，跟 /transcribe/start
// 一样"提交任务 + 轮询"，yt-dlp 专门维护、持续跟进 YouTube 反爬变化，比自己猜
// 请求头靠谱得多，代价是比之前浏览器直接 fetch 多花几秒钟
async function fetchCaptionsViaBackend(videoUrl, lang) {
  const startRes = await fetch(
    `${BACKEND}/captions/start?url=${encodeURIComponent(videoUrl)}&lang=${encodeURIComponent(lang || "en")}`
  );
  if (!startRes.ok) throw new Error(`captions start failed: ${startRes.status}`);
  const { job_id } = await startRes.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const statusRes = await fetch(`${BACKEND}/captions/status?job_id=${job_id}`);
    if (!statusRes.ok) throw new Error(`captions status failed: ${statusRes.status}`);
    const job = await statusRes.json();
    if (job.status === "error") throw new Error(job.error || "captions fetch failed");
    if (job.status === "done") return job.vtt || "";
  }
}

function parseVtt(vttText) {
  // 极简 VTT 解析：够用即可，复杂样式/嵌套 span 会被忽略
  const lines = vttText.split("\n");
  const segments = [];
  let cur = null;
  const timeRe =
    /(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})/;

  function toSeconds(ts) {
    const parts = ts.split(":").map(Number);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return parts[0] * 60 + parts[1];
  }

  for (const line of lines) {
    const m = line.match(timeRe);
    if (m) {
      if (cur) segments.push(cur);
      cur = { start: toSeconds(m[1]), end: toSeconds(m[2]), text: "" };
    } else if (cur && line.trim() && !/^\d+$/.test(line.trim())) {
      cur.text += (cur.text ? " " : "") + line.replace(/<[^>]+>/g, "").trim();
    }
  }
  if (cur) segments.push(cur);
  return segments.filter((s) => s.text);
}

function pickBestTrack(tracks) {
  if (!tracks.length) return null;
  // 优先选英语字幕轨道（其中再优先人工字幕），没有英语的话就随便挑一条
  // （同样优先人工字幕），反正最终都要翻译成中文
  const isEnglish = (t) => (t.languageCode || "").toLowerCase().startsWith("en");
  const englishTracks = tracks.filter(isEnglish);
  const pool = englishTracks.length ? englishTracks : tracks;
  return pool.find((t) => t.kind !== "asr") || pool[0];
}

// 没有现成字幕、或者字幕轨道存在但抓不到内容时走这里：后端识别是按音频块推进的，一块识别完就会让
// transcribeViaBackend 的 job.segments 变长，这里一旦看到新出现的部分，立刻单独
// 送去翻译（不等整段视频识别完），让识别和翻译两段流水线并行推进，第一批字幕能
// 早很多出现，而不用等整部视频的语音识别全部跑完才开始翻第一句。
//
// chunkResults 按"送去翻译的批次"分槛存放各自目前翻好的部分（哪一批内部哪句先
// 翻完完全不保证顺序，见 translateViaBackend），拼起来才是完整的字幕列表——不能
// 简单地把新到的部分往 currentSegments 后面 append，因为同一批内部的翻译结果
// 会随着轮询反复整批替换，不是只在末尾增量追加
async function recognizeAndTranslateViaBackend(videoId) {
  let sentCount = 0; // 已经切给某一批翻译的识别结果数量，累积识别结果只会变长不会重排
  let originalSoFar = 0;
  let translatedSoFar = 0;
  const chunkResults = [];
  const pendingTranslations = [];

  const rebuildCurrentSegments = () => {
    currentSegments = chunkResults.flat();
    if (currentSegments.length) subtitlesReady = true;
  };

  const updateTranslateBadge = () => {
    if (!originalSoFar) return;
    setProgressBadge("translate", `翻译中...${Math.round((translatedSoFar / originalSoFar) * 100)}%`);
  };

  const spawnTranslateForNewSegments = (allSegments) => {
    const newSegments = allSegments.slice(sentCount);
    if (!newSegments.length) return;
    const offset = sentCount;
    sentCount = allSegments.length;
    originalSoFar += newSegments.length;
    const chunkIndex = chunkResults.length;
    chunkResults.push([]);

    let prevDone = 0;
    const p = translateViaBackend(
      newSegments,
      videoId,
      (job) => {
        const doneSegs = job.segments ?? [];
        translatedSoFar += Math.max(0, doneSegs.length - prevDone);
        prevDone = doneSegs.length;
        chunkResults[chunkIndex] = doneSegs;
        rebuildCurrentSegments();
        updateTranslateBadge();
      },
      offset
    );
    pendingTranslations.push(p);
  };

  // 识别进度跟翻译一样，用右上角独立角标显示（而不是写进中间的字幕框），这样即使
  // 第一块已经翻完开始显示正式字幕了，后面几块还在识别的进度也依然能看到
  await transcribeViaBackend(location.href, (job) => {
    if (job.status === "running") {
      setProgressBadge("recognize", `识别中...${job.progress ?? 0}%`);
    }
    if (job.segments?.length) spawnTranslateForNewSegments(job.segments);
  });
  removeProgressBadge("recognize");

  await Promise.all(pendingTranslations);
}

async function loadSubtitlesForCurrentVideo(captionTracks) {
  const videoId = getVideoId();
  if (!videoId || videoId === lastVideoId) return;
  lastVideoId = videoId;
  currentSegments = [];
  subtitlesReady = false;

  const track = pickBestTrack(captionTracks);

  try {
    let originalSegments = [];
    if (track) {
      console.log("[YT中文字幕] 找到现成字幕轨道:", track.name);
      setProgressBadge("captions", "抓取字幕中...");
      const vtt = await fetchCaptionsViaBackend(location.href, track.languageCode);
      removeProgressBadge("captions");
      originalSegments = parseVtt(vtt);
      if (!originalSegments.length) {
        console.warn("[YT中文字幕] 后端抓字幕也是空的，改走语音识别...");
      }
    }

    if (originalSegments.length) {
      currentSegments = await translateViaBackend(originalSegments, videoId, (job) => {
        // 已翻好的部分随每次轮询更新，一旦有第一批结果就可以开始显示字幕，
        // 不用等 job 整体 status 变成 done；进度角标独立于字幕悬浮层，
        // 不会被播放同步逻辑覆盖，翻译没跑完之前一直能看到进度
        currentSegments = job.segments ?? [];
        if (currentSegments.length) subtitlesReady = true;

        setProgressBadge("translate", `翻译中...${job.progress ?? 0}%`);
      });
    } else {
      // 没有字幕轨道，或者字幕轨道存在但抓不到内容（比如 yt-dlp 那边也拿不到），
      // 两种情况都退回语音识别，不让字幕就这么悄无声息地卡住
      console.log("[YT中文字幕] 请求本地服务器做语音识别...");
      await recognizeAndTranslateViaBackend(videoId);
    }

    removeAllProgressBadges();
    subtitlesReady = true;
    console.log(`[YT中文字幕] 已加载 ${currentSegments.length} 条中文字幕`);
  } catch (err) {
    console.error("[YT中文字幕] 加载字幕出错：", err);
    removeAllProgressBadges();
  }
}

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  if (event.data?.source !== "yt-zh-sub-inject") return;
  loadSubtitlesForCurrentVideo(event.data.captionTracks || []);
});

injectPageScript();
startSyncLoop();

// 处理 YouTube 单页跳转（切视频不整页刷新）
document.addEventListener("yt-navigate-finish", () => {
  // 光把 overlayEl 变量设成 null 不够：旧视频最后显示的那行字幕文字所在的
  // DOM 元素本身还留在 #movie_player 里没被移除，新视频这边会因为 overlayEl
  // 是 null 而创建一个全新的 div，旧的那个孤儿节点从此没人再更新，永远停留在
  // 屏幕上——变成"叠着上一个视频残留字幕"的样子，必须先真正移除旧元素
  overlayEl?.remove();
  overlayEl = null;
  syncBound = false;
  subtitlesReady = false;
  removeAllProgressBadges();
  startSyncLoop();
});
