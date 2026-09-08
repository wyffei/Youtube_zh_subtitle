// content.js —— 运行在插件的隔离世界，负责：
// 1. 注入 inject.js 去读取页面的字幕轨道信息
// 2. 没有字幕时，直接请求本地服务器做语音识别
// 3. 把字幕翻译成中文，渲染成悬浮层跟着视频时间走
//
// 直接在这里 fetch 本地服务器（而不是转发给 background.js 的 service worker）：
// manifest 的 host_permissions 已经允许跨域，而 service worker 闲置/跑太久会被
// Chrome 强制杀掉，Whisper 转录动辄几分钟，用它中转会导致请求中途被打断。

const BACKEND = "http://127.0.0.1:8000";

// 识别一个长视频动辄十几分钟，后端把它做成"提交任务 + 轮询进度"，
// 这里每 2 秒查一次状态，边等边把百分比回调出去显示在字幕悬浮层上
async function transcribeViaBackend(videoUrl, onProgress) {
  const startRes = await fetch(`${BACKEND}/transcribe/start?url=${encodeURIComponent(videoUrl)}`);
  if (!startRes.ok) throw new Error(`transcribe start failed: ${startRes.status}`);
  const { job_id } = await startRes.json();

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const statusRes = await fetch(`${BACKEND}/transcribe/status?job_id=${job_id}`);
    if (!statusRes.ok) throw new Error(`transcribe status failed: ${statusRes.status}`);
    const job = await statusRes.json();
    if (job.status === "error") throw new Error(job.error || "transcribe failed");
    onProgress?.(job.progress ?? 0);
    if (job.status === "done") return job.segments;
  }
}

// 翻译同样做成"提交任务 + 轮询"，后端边翻边把已完成的部分写回 job，
// 这里每 1.5 秒查一次，拿到的是持续变长的已翻译列表，可以边看边显示，
// 不用等几百句字幕全部翻完才能看到第一条
async function translateViaBackend(segments, videoId, onUpdate) {
  const startRes = await fetch(`${BACKEND}/translate/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segments, video_id: videoId }),
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

let progressEl = null;
function ensureProgressBadge() {
  if (progressEl && document.body.contains(progressEl)) return progressEl;
  const player = document.querySelector("#movie_player");
  if (!player) return null;
  progressEl = document.createElement("div");
  progressEl.id = "yt-zh-sub-progress";
  player.appendChild(progressEl);
  return progressEl;
}

function removeProgressBadge() {
  progressEl?.remove();
  progressEl = null;
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

async function fetchExistingCaptionVtt(track) {
  // 加 &fmt=vtt 让 YouTube 直接返回 WebVTT 格式
  const res = await fetch(track.baseUrl + "&fmt=vtt");
  return res.text();
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

async function loadSubtitlesForCurrentVideo(captionTracks) {
  const videoId = getVideoId();
  if (!videoId || videoId === lastVideoId) return;
  lastVideoId = videoId;
  currentSegments = [];
  subtitlesReady = false;

  const track = pickBestTrack(captionTracks);
  let originalSegments = [];

  try {
    if (track) {
      console.log("[YT中文字幕] 找到现成字幕轨道:", track.name);
      const vtt = await fetchExistingCaptionVtt(track);
      originalSegments = parseVtt(vtt);
    } else {
      console.log("[YT中文字幕] 没有字幕轨道，请求本地服务器做语音识别...");
      originalSegments = await transcribeViaBackend(location.href, (percent) => {
        const el = ensureOverlay();
        if (el) el.textContent = `字幕识别中...${percent}%`;
      });
    }

    if (!originalSegments.length) return;

    currentSegments = await translateViaBackend(originalSegments, videoId, (job) => {
      // 已翻好的部分随每次轮询更新，一旦有第一批结果就可以开始显示字幕，
      // 不用等 job 整体 status 变成 done；进度角标独立于字幕悬浮层，
      // 不会被播放同步逻辑覆盖，翻译没跑完之前一直能看到进度
      currentSegments = job.segments ?? [];
      if (currentSegments.length) subtitlesReady = true;

      const badge = ensureProgressBadge();
      if (badge) badge.textContent = `翻译中...${job.progress ?? 0}%`;
    });
    removeProgressBadge();
    subtitlesReady = true;
    console.log(`[YT中文字幕] 已加载 ${currentSegments.length} 条中文字幕`);
  } catch (err) {
    console.error("[YT中文字幕] 加载字幕出错：", err);
    removeProgressBadge();
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
  overlayEl = null;
  syncBound = false;
  subtitlesReady = false;
  removeProgressBadge();
  startSyncLoop();
});
