(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const els = {
    form: $("url-form"), url: $("url"), loadBtn: $("load-btn"), file: $("file"), fileName: $("file-name"),
    loading: $("loading"), loadingText: $("loading-text"), barFill: $("bar-fill"), error: $("error"),
    envWarning: $("env-warning"), card: $("player-card"), title: $("title"), duration: $("duration"),
    slider: $("slider"), minus: $("minus"), plus: $("plus"), reset: $("reset"),
    keyValue: $("key-value"), keyLabel: $("key-label"), rendering: $("rendering"),
    player: $("player"), download: $("download"), engineNote: $("engine-note"),
  };
  // playerId: プレーヤーに今読み込まれている音源の ID(別の曲なら再生位置を引き継がない)
  const state = { id: null, title: "", semitones: 0, renderSeq: 0, debounce: null, busy: false, playerId: null };

  function disableDownload() {
    els.download.setAttribute("aria-disabled", "true");
    els.download.removeAttribute("href");
    els.download.setAttribute("tabindex", "-1");
  }

  function enableDownload(url, filename, n) {
    els.download.href = url;
    els.download.setAttribute("download", filename);
    els.download.textContent = "ダウンロード (" + keyText(n) + ", MP3)";
    els.download.removeAttribute("tabindex");
    els.download.setAttribute("aria-disabled", "false");
  }

  function resetPlayer() {
    const p = els.player;
    p.pause();
    p.removeAttribute("src");
    p.load();
    state.playerId = null;
  }

  function showError(msg) {
    els.error.textContent = msg;
    els.error.hidden = !msg;
  }

  async function api(path, opts) {
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new Error("アプリ本体に接続できません。黒い画面(サーバー)が閉じていないか確認してください。");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 空 */ }
    if (!res.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg) || !msg) msg = "入力内容が正しくありません。";
      throw new Error(msg);
    }
    return data;
  }

  function fmtDuration(sec) {
    if (!sec && sec !== 0) return "";
    sec = Math.round(sec);
    const m = Math.floor(sec / 60), s = sec % 60;
    return "長さ " + m + ":" + String(s).padStart(2, "0");
  }

  function keyText(n) { return n === 0 ? "±0" : (n > 0 ? "+" + n : "−" + Math.abs(n)); }
  function keyLabel(n) {
    if (n === 0) return "原曲キー";
    return "半音 " + Math.abs(n) + " つ" + (n > 0 ? "上げ" : "下げ");
  }

  function setLoading(on, text) {
    state.busy = on;
    els.loading.hidden = !on;
    els.loadBtn.disabled = on;
    els.file.disabled = on;
    if (text) els.loadingText.textContent = text;
  }

  function setProgress(p) {
    const bar = els.barFill.parentElement;
    if (p == null) { bar.classList.add("indeterminate"); els.barFill.style.width = ""; }
    else { bar.classList.remove("indeterminate"); els.barFill.style.width = Math.round(p * 100) + "%"; }
  }

  async function checkStatus() {
    try {
      const s = await api("/api/status");
      if (!s.ffmpeg) {
        els.envWarning.textContent = "ffmpeg が見つかりません。README の手順で ffmpeg をインストールしてから、アプリを起動し直してください。";
        els.envWarning.hidden = false;
      }
      if (s.engine === "fallback") {
        els.engineNote.textContent = "※ 簡易モードで変換しています(ffmpeg に rubberband が無いため、音質が少し落ちることがあります)。";
      }
    } catch (e) { /* 無視 */ }
  }

  function onLoaded(meta) {
    if (state.playerId !== meta.id) resetPlayer();
    state.id = meta.id;
    state.title = meta.title;
    els.download.textContent = "ダウンロード (MP3)";
    els.title.textContent = meta.title;
    els.duration.textContent = fmtDuration(meta.duration);
    els.card.hidden = false;
    setKey(0, true);
    els.card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function loadUrl(url) {
    showError("");
    url = url.trim();
    if (!/^https?:\/\/\S+$/i.test(url)) {
      showError("URL の形式が正しくありません。http:// または https:// で始まる URL を貼り付けてください。");
      return;
    }
    setLoading(true, "ダウンロード中…");
    setProgress(null);
    try {
      const { job } = await api("/api/load", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
      });
      for (;;) {
        await sleep(700);
        const j = await api("/api/jobs/" + job);
        if (j.status === "done") { onLoaded(j.result); break; }
        if (j.status === "error") throw new Error(j.error || "ダウンロードに失敗しました。");
        if (j.progress != null) {
          setProgress(j.progress);
          els.loadingText.textContent = "ダウンロード中… " + Math.round(j.progress * 100) + "%";
        }
      }
    } catch (e) {
      showError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function uploadFile(file) {
    showError("");
    els.fileName.textContent = file.name;
    setLoading(true, "ファイルを読み込み中…");
    setProgress(null);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const meta = await api("/api/upload", { method: "POST", body: fd });
      onLoaded(meta);
    } catch (e) {
      showError(e.message);
    } finally {
      setLoading(false);
      els.file.value = "";
    }
  }

  function setKey(n, immediate) {
    n = Math.max(-12, Math.min(12, Math.round(n)));
    state.semitones = n;
    els.slider.value = String(n);
    els.keyValue.textContent = keyText(n);
    els.keyLabel.textContent = keyLabel(n);
    els.minus.disabled = n <= -12;
    els.plus.disabled = n >= 12;
    disableDownload();
    state.renderSeq++; // 実行中の古い変換結果は使わない
    els.rendering.hidden = false;
    clearTimeout(state.debounce);
    state.debounce = setTimeout(renderCurrent, immediate ? 0 : 450);
  }

  async function renderCurrent() {
    if (!state.id) return;
    const seq = ++state.renderSeq;
    const n = state.semitones;
    const id = state.id;
    disableDownload();
    els.rendering.hidden = false;
    showError("");
    try {
      const r = await api("/api/render", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: id, semitones: n }),
      });
      // もっと新しい操作(キー変更・別の曲の読み込み)がある
      if (seq !== state.renderSeq || n !== state.semitones || id !== state.id) return;
      const p = els.player;
      const sameTrack = state.playerId === id;
      const wasPlaying = sameTrack && !p.paused && !p.ended;
      const pos = sameTrack ? (p.currentTime || 0) : 0;
      p.src = r.url;
      state.playerId = id;
      if (sameTrack) {
        p.addEventListener("loadedmetadata", function restore() {
          p.removeEventListener("loadedmetadata", restore);
          if (pos > 0 && pos < p.duration) p.currentTime = pos;
          if (wasPlaying) p.play().catch(() => {});
        });
      }
      p.load();
      enableDownload(r.download_url, r.filename, n);
    } catch (e) {
      if (seq === state.renderSeq) showError(e.message);
    } finally {
      if (seq === state.renderSeq) els.rendering.hidden = true;
    }
  }

  els.form.addEventListener("submit", (e) => { e.preventDefault(); if (!state.busy) loadUrl(els.url.value); });
  els.file.addEventListener("change", () => { const f = els.file.files && els.file.files[0]; if (f) uploadFile(f); });
  els.slider.addEventListener("input", () => setKey(parseInt(els.slider.value, 10)));
  els.minus.addEventListener("click", () => setKey(state.semitones - 1));
  els.plus.addEventListener("click", () => setKey(state.semitones + 1));
  els.reset.addEventListener("click", () => setKey(0));
  // 準備中のダウンロードボタンはクリック・Enter でも反応させない
  els.download.addEventListener("click", (e) => {
    if (els.download.getAttribute("aria-disabled") === "true") e.preventDefault();
  });
  els.player.addEventListener("error", () => {
    if (els.player.getAttribute("src")) showError("音声を再生できませんでした。もう一度お試しください。");
  });

  checkStatus();
})();
