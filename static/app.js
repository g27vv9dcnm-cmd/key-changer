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
  // seq:      キーを変える・曲を読み込むたびに増える番号(古い操作への返事を見分ける)
  // applied:  プレーヤーに反映し終えた操作の番号
  // playerId: プレーヤーに今読み込まれている音源の ID(別の曲なら再生位置を引き継がない)
  // shown:    プレーヤーに今読み込まれている音声 { id, n, direct }(direct = 音源そのものを再生している)
  // compat:   音源そのものをこのブラウザで再生できなかった音源の ID(変換したものを使う)
  // resume:   キーを切り替えたあとに戻す再生位置 { pos, playing }(読み込みが済んだら戻して消す)
  // dl:       ダウンロードボタンの中身 { id, n, ready }(ready = MP3 がもう出来ている)
  const state = {
    id: null, title: "", semitones: 0, seq: 0, applied: 0, debounce: null, peekTimer: null, spinner: null,
    busy: false, playerId: null, shown: null, compat: {}, resume: null, dl: null,
  };

  const MAKING_MP3 = "MP3 を作成中…";

  function disableDownload() {
    state.dl = null;
    if (els.download.textContent === MAKING_MP3) els.download.textContent = "ダウンロード (MP3)";
    els.download.setAttribute("aria-disabled", "true");
    els.download.removeAttribute("href");
    els.download.setAttribute("tabindex", "-1");
  }

  function enableDownload(url, filename, id, n, ready) {
    state.dl = { id: id, n: n, ready: ready };
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
    state.shown = null;
    state.resume = null;
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
    setKey(0, 0);
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
        // 終わるまで(最長 1 秒)サーバー側で待ってもらう。終わったらすぐ返事が来る
        const asked = Date.now();
        const j = await api("/api/jobs/" + job + "?wait=1");
        if (j.status === "done") { onLoaded(j.result); break; }
        if (j.status === "error") throw new Error(j.error || "ダウンロードに失敗しました。");
        if (j.progress != null) {
          setProgress(j.progress);
          els.loadingText.textContent = "ダウンロード中… " + Math.round(j.progress * 100) + "%";
        }
        if (Date.now() - asked < 300) await sleep(300); // すぐ返事が来たときに問い合わせを連発しない
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

  // delay:     変換を頼むまでの待ち時間(ミリ秒)。続けて操作している間は頼まない
  // peekDelay: 「もう変換済みなら、すぐ切り替える」ための問い合わせを出すまでの待ち時間。
  //            近くのキーは先回りで変換済みのことが多いので、変換を頼むより先に聞いてみる
  function setKey(n, delay, peekDelay) {
    n = Math.max(-12, Math.min(12, Math.round(n)));
    state.semitones = n;
    els.slider.value = String(n);
    els.keyValue.textContent = keyText(n);
    els.keyLabel.textContent = keyLabel(n);
    els.minus.disabled = n <= -12;
    els.plus.disabled = n >= 12;
    disableDownload();
    const seq = ++state.seq; // 実行中の古い変換結果は使わない
    clearTimeout(state.debounce);
    clearTimeout(state.peekTimer);
    clearTimeout(state.spinner);
    if (delay === undefined) delay = 450;
    // 変換済みのキーに切り替えるだけのときに「変換中」が一瞬ちらつかないよう、少し遅らせて出す
    state.spinner = setTimeout(() => { els.rendering.hidden = false; }, 150);
    state.debounce = setTimeout(() => renderCurrent(seq, false), delay);
    if (delay > 0) state.peekTimer = setTimeout(() => renderCurrent(seq, true), peekDelay || 0);
  }

  // peek = true: 変換済みのときだけ切り替える(まだなら何もしない。あとで peek = false の依頼が来る)
  async function renderCurrent(seq, peek) {
    if (!state.id || seq !== state.seq) return;
    const n = state.semitones;
    const id = state.id;
    if (!peek) showError("");
    try {
      const r = await api("/api/render", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: id, semitones: n, compat: !!state.compat[id], peek: peek }),
      });
      // もっと新しい操作(キー変更・別の曲の読み込み)がある / もう反映した / まだ変換されていない
      if (seq !== state.seq || state.applied === seq || !r.ready) return;
      state.applied = seq;
      clearTimeout(state.debounce);
      clearTimeout(state.spinner);
      showError("");
      const p = els.player;
      // 同じ曲のキーを変えたときは、聴いていた位置から続ける。前の切り替えがまだ読み込み中なら
      // (続けて切り替えたとき)、そのときに覚えた位置をそのまま使う
      if (state.playerId !== id) state.resume = null;
      else if (!state.resume) state.resume = { pos: p.currentTime || 0, playing: !p.paused && !p.ended };
      p.src = r.url;
      state.playerId = id;
      state.shown = { id: id, n: n, direct: !!r.direct };
      p.load();
      enableDownload(r.download_url, r.filename, id, n, !!r.mp3);
      els.rendering.hidden = true;
    } catch (e) {
      if (peek || seq !== state.seq) return; // peek の失敗は、このあとの本番の依頼に任せる
      clearTimeout(state.spinner);
      els.rendering.hidden = true;
      showError(e.message);
    }
  }

  // MP3 は保存するときに初めて作る(再生には使わない)。出来上がってからダウンロードを始める
  async function download() {
    const dl = state.dl;
    const label = els.download.textContent;
    els.download.setAttribute("aria-disabled", "true");
    els.download.textContent = MAKING_MP3;
    showError("");
    try {
      await api("/api/mp3", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: dl.id, semitones: dl.n }),
      });
    } catch (e) {
      if (state.dl === dl) showError(e.message);
      return;
    } finally {
      if (state.dl === dl) {
        els.download.textContent = label;
        els.download.setAttribute("aria-disabled", "false");
      }
    }
    if (state.dl !== dl) return; // 作っている間に別のキー・別の曲に変えられた
    dl.ready = true;
    els.download.click();
  }

  els.form.addEventListener("submit", (e) => { e.preventDefault(); if (!state.busy) loadUrl(els.url.value); });
  els.file.addEventListener("change", () => { const f = els.file.files && els.file.files[0]; if (f) uploadFile(f); });
  // スライダーは動かしている途中の値も届くので、少し止まってから問い合わせる
  els.slider.addEventListener("input", () => setKey(parseInt(els.slider.value, 10), 450, 120));
  // ボタンは 1 つずつ動かすので短めに待つ(変換済みなら待たずに切り替わる)
  els.minus.addEventListener("click", () => setKey(state.semitones - 1, 200));
  els.plus.addEventListener("click", () => setKey(state.semitones + 1, 200));
  els.reset.addEventListener("click", () => setKey(0, 200));
  els.download.addEventListener("click", (e) => {
    // 準備中のダウンロードボタンはクリック・Enter でも反応させない
    if (els.download.getAttribute("aria-disabled") === "true" || !state.dl) { e.preventDefault(); return; }
    if (state.dl.ready) return; // MP3 はもうある。そのままダウンロードする
    e.preventDefault();
    download();
  });
  els.player.addEventListener("loadedmetadata", () => {
    const r = state.resume;
    state.resume = null;
    if (!r) return;
    if (r.pos > 0 && r.pos < els.player.duration) els.player.currentTime = r.pos;
    if (r.playing) els.player.play().catch(() => {});
  });
  els.player.addEventListener("error", () => {
    if (!els.player.getAttribute("src")) return;
    const s = state.shown;
    if (s && s.direct && !state.compat[s.id]) {
      // 音源そのものを、このブラウザでは再生できなかった。変換したもの(WAV)に切り替える
      state.compat[s.id] = true;
      if (s.id === state.id && s.n === state.semitones) setKey(s.n, 0);
      return;
    }
    showError("音声を再生できませんでした。もう一度お試しください。");
  });

  checkStatus();
})();
