// inject.js —— 运行在页面自己的 JS 上下文里（不是插件的隔离世界），
// 这样才能读到 YouTube 页面自己的 window.ytInitialPlayerResponse
(function () {
  function getPlayerResponse() {
    // window.ytInitialPlayerResponse 只在整页加载时才会被写入当前视频的数据；
    // YouTube 站内跳转（单页应用导航，不整页刷新）时它不会跟着更新，会一直停留
    // 在上一个视频上，导致这里读到旧视频的字幕信息（或者干脆读不到）。播放器
    // 实例自己的 getPlayerResponse() 才是当前正在播放视频的实时数据，不受这个
    // 全局变量是否刷新影响，优先用它；只有播放器还没就绪（比如脚本注入得比
    // 播放器初始化还早）时才退回全局变量兜底
    try {
      const player = document.querySelector("#movie_player");
      const live = player?.getPlayerResponse?.();
      if (live) return live;
    } catch (e) {
      // 忽略，走兜底
    }
    return window.ytInitialPlayerResponse;
  }

  function getCaptionTracks() {
    try {
      const data = getPlayerResponse();
      const tracks =
        data?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
      return tracks.map((t) => ({
        baseUrl: t.baseUrl,
        languageCode: t.languageCode,
        kind: t.kind || "manual", // 'asr' = YouTube 自动生成
        name: t.name?.simpleText || t.languageCode,
      }));
    } catch (e) {
      return [];
    }
  }

  function report() {
    window.postMessage(
      { source: "yt-zh-sub-inject", captionTracks: getCaptionTracks() },
      "*"
    );
  }

  report();

  // YouTube 是单页应用，切视频不会整页刷新，需要监听导航事件重新上报
  document.addEventListener("yt-navigate-finish", report);
})();
