const wsUrlEl = document.getElementById("wsUrl");
const tokenEl = document.getElementById("token");
const tempChatEl = document.getElementById("tempChat");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("saveBtn");
const testBtn = document.getElementById("testBtn");

function renderStatus(state) {
  const cls = state?.connected ? "ok" : "err";
  const errLine = state?.lastError ? `<div class="err">${state.lastError}</div>` : "";
  statusEl.innerHTML = `<div class="${cls}">${state?.lastStatus || "Offline"}</div>${errLine}`;
}

function refresh() {
  chrome.runtime.sendMessage({ action: "get_status" }, (res) => {
    if (!res) return;
    wsUrlEl.value = res.cfg?.wsUrl || "";
    tokenEl.value = res.cfg?.token || "";
    tempChatEl.checked = res.cfg?.tempChat !== false;
    renderStatus(res.state);
  });
}

saveBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage(
    {
      action: "save_config",
      wsUrl: wsUrlEl.value,
      token: tokenEl.value,
      tempChat: tempChatEl.checked,
    },
    (res) => {
      renderStatus(res?.state);
      setTimeout(refresh, 800);
    }
  );
});

testBtn.addEventListener("click", () => {
  statusEl.textContent = "Đang test sinh ảnh qua Chrome profile hiện tại...";
  chrome.runtime.sendMessage(
    { action: "test_generate", prompt: "a cute orange tabby cat sitting on a wooden desk" },
    (res) => {
      if (res?.ok) {
        statusEl.innerHTML = `<div class="ok">OK! ${res.mime} (${res.bytesLen} bytes)</div>`;
      } else {
        statusEl.innerHTML = `<div class="err">Lỗi: ${res?.error || "unknown"}</div>`;
      }
    }
  );
});

function refreshStatusOnly() {
  chrome.runtime.sendMessage({ action: "get_status" }, (res) => {
    if (res) renderStatus(res.state);
  });
}

refresh();
setInterval(refreshStatusOnly, 1500);
