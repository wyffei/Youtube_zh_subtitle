// inject.js —— 运行在页面自己的 JS 上下文里（不是插件的隔离世界），
// 这样才能读到 YouTube 页面自己的 window.ytInitialPlayerResponse
(function () {
  function getCaptionTracks() {
    try {
      const data = window.ytInitialPlayerResponse;
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
