fetch("http://127.0.0.1:8000/health")
  .then((r) => (r.ok ? r.json() : Promise.reject()))
  .then(() => {
    const el = document.getElementById("status");
    el.textContent = "本地服务器已连接 ✅";
    el.className = "ok";
  })
  .catch(() => {
    const el = document.getElementById("status");
    el.textContent = "本地服务器未启动（先运行 backend/server.py）";
    el.className = "bad";
  });
