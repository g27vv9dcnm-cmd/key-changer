// リアルタイムのキー変更(ピッチシフト)。AudioWorklet で動かす。依存なし・ビルド不要のただの JavaScript で、
// ほかのプログラムのコードは使っていない(このアプリのために書いたもの)。
//
// 何をしているか: 画面は原曲キーの音声だけを再生し、その音をこのプログラムに通して音の高さを変える。
// キーを変えると semitones(AudioParam、−12〜+12)が変わるだけなので、音は数十ミリ秒で切り替わり、
// 再生位置も途切れずにそのまま続く。保存用の MP3 はこれとは別に、サーバーが rubberband で丁寧に作る。
//
// 方式: フェーズボコーダーで長さを r 倍に引き伸ばし(テンポだけ変える)、それを r 倍の速さで読み直す
// (リサンプル)ことで、テンポはそのままで音の高さを r = 2^(n/12) 倍にする。
//   - 分析の間隔(ホップ)は Ha = N/8 で一定。合成の間隔は Hs = r * Ha(フレームごとに整数に丸め、端数は持ち越す)
//   - 位相: 「位相勾配のヒープ積分」(Prusa & Holighaus 2017, "phase vocoder done right")。各ビンの位相を、
//     前のフレームの同じビンから(時間方向。瞬時周波数を使う)か、今のフレームのもっと大きい隣のビンから
//     (周波数方向。群遅延を r 倍する)のどちらかで決める。大きく鳴り続ける音は時間方向のつながりを保ち、
//     音の立ち上がりでは周波数方向にそろう。立ち上がりの検出もピークの拾い出しも要らない。
//   - ステレオ: ビンごとの位相の回転量は L と R で共通にする(左右の位相差がそのまま残り、定位が崩れない)。
//     位相の傾きは、そのビンで大きい方のチャンネルで測る。L/R は 1 回の複素 FFT でまとめて変換する。
//   - r == 1(原曲キー)では回転量が 2 秒ほどで 0 に戻り、そのあとはただの遅延(音はまったく変わらない)になる。
//   - r > 1 のときの折り返し防止は、逆 FFT の前に N/(2r) より上のビンを 0 にして行う。
//   - リサンプラー: 32 タップの Kaiser 窓付き sinc、256 位相、位相の間は直線補間。
//   - キーを変えたときは、読み出しの速さを 1 ホップ(約 11 ミリ秒)かけてなめらかに変える。
//   - 遅延(サンプル数): N/2 + (N/2 + 17) / r。44.1kHz で 80〜140 ミリ秒(原曲キーで約 93 ミリ秒)。
//
// パラメータを選んだ理由(試験用の音で、変換結果を本物の音程の音と比べて決めた):
//   - FFT 4096 点(64kHz を超えるときは 8192)・ホップ 512(N/8): FFT 2048 では、和音に含まれない
//     周波数の成分(にじみ)が +3・+7・−5 で −17〜−21 dB まで増えた(4096 では同じキーで −39〜−47 dB)。
//   - 周波数方向の位相合わせ: これを入れないと、クリック音の前に出る「前ぶれ」(プリエコー)が
//     +7 で −7 dB と大きく聞こえた(入れると −31 dB)。
//
// 操作: semitones(AudioParam)でキーを変える。シークや曲の入れ替えのときは port に {type: 'flush'} を
// 送ると、前の位置の音の残りを捨てる(キーはそのまま)。port からは時々 {type: 'latency', seconds} が届く。
// Node でも動く(globalThis.PitchShifter。試験用)。
(function (root) {
  'use strict';
  const TAPS = 32, PHASES = 256, TWO_PI = 2 * Math.PI;

  function bessel0(x) {
    let s = 1, t = 1;
    for (let k = 1; k < 40; k++) { t *= (x / (2 * k)) * (x / (2 * k)); s += t; if (t < 1e-12 * s) break; }
    return s;
  }
  function makeSincTable() {
    const tab = new Float32Array((PHASES + 1) * TAPS), beta = 8.0, half = TAPS / 2;
    for (let p = 0; p <= PHASES; p++) {
      const frac = p / PHASES;
      let sum = 0;
      for (let j = 0; j < TAPS; j++) {
        const x = j - (half - 1) - frac; // 求めたい点からの距離
        const u = x / half;
        const w = Math.abs(u) < 1 ? bessel0(beta * Math.sqrt(1 - u * u)) / bessel0(beta) : 0;
        const s = x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
        tab[p * TAPS + j] = s * w; sum += s * w;
      }
      for (let j = 0; j < TAPS; j++) tab[p * TAPS + j] /= sum;
    }
    return tab;
  }

  class FFT {
    constructor(n) {
      this.n = n;
      this.rev = new Uint32Array(n);
      const bits = Math.round(Math.log2(n));
      for (let i = 0; i < n; i++) { let r = 0; for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b); this.rev[i] = r; }
      this.cos = new Float64Array(n / 2); this.sin = new Float64Array(n / 2);
      for (let i = 0; i < n / 2; i++) { this.cos[i] = Math.cos(TWO_PI * i / n); this.sin[i] = -Math.sin(TWO_PI * i / n); }
    }
    transform(re, im) { // 順方向。その場で書き換える
      const n = this.n, rev = this.rev, cos = this.cos, sin = this.sin;
      for (let i = 0; i < n; i++) { const j = rev[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
      for (let size = 2; size <= n; size <<= 1) {
        const half = size >> 1, step = n / size;
        for (let i = 0; i < n; i += size) {
          for (let j = 0, k = 0; j < half; j++, k += step) {
            const a = i + j, b = a + half, wr = cos[k], wi = sin[k];
            const xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;
            re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
          }
        }
      }
    }
  }

  class PitchShifter {
    constructor(sampleRate, opt) {
      opt = opt || {};
      const N = opt.fftSize || (sampleRate > 64000 ? 8192 : 4096);
      this.N = N; this.K = N / 2 + 1; this.Ha = opt.hop || N / 8;
      this.mode = opt.mode || 'pghi';            // 'pghi' | 'plain'(周波数方向の位相合わせなし。比較用)
      this.tol = opt.tol || 1e-10;               // これより小さいビンは位相を計算しない(いちばん大きいビンに対するパワーの比)
      this.fft = new FFT(N);
      this.win = new Float64Array(N);
      for (let i = 0; i < N; i++) this.win[i] = 0.5 - 0.5 * Math.cos(TWO_PI * i / N);
      this.sinc = makeSincTable();
      this.inL = new Float32Array(N); this.inR = new Float32Array(N);
      this.re = new Float64Array(N); this.im = new Float64Array(N);
      const K = this.K;
      this.Lr = new Float64Array(K); this.Li = new Float64Array(K); this.Rr = new Float64Array(K); this.Ri = new Float64Array(K);
      this.pLr = new Float64Array(K); this.pLi = new Float64Array(K); this.pRr = new Float64Array(K); this.pRi = new Float64Array(K);
      this.pw = new Float64Array(K); this.ppw = new Float64Array(K);
      this.theta = new Float64Array(K); this.ptheta = new Float64Array(K);
      this.done = new Uint8Array(K);
      this.heap = new Int32Array(2 * K + 4); this.key = new Float64Array(2 * K);
      this.M = 8 * N; this.mask = this.M - 1;
      this.accL = new Float32Array(this.M); this.accR = new Float32Array(this.M); this.wsum = new Float32Array(this.M);
      this.LAG = N / 2 + TAPS / 2 + 1;
      this.ratio = 1; this.target = 1;
      this.reset();
    }
    reset() {
      this.inL.fill(0); this.inR.fill(0); this.accL.fill(0); this.accR.fill(0); this.wsum.fill(0);
      this.ptheta.fill(0); this.ppw.fill(0); this.pLr.fill(0); this.pLi.fill(0); this.pRr.fill(0); this.pRi.fill(0);
      this.inCount = 0; this.first = true;
      this.sC = this.N;                 // 最後のフレームの中心の、引き伸ばした時間軸での位置
      this.end = this.sC + this.N / 2;  // これまでに書いた最後の位置の次
      this.edge = this.sC - this.N / 2; // これより前は確定済み(正規化済み)
      this.rp = this.sC - this.LAG;
      this.ratio = this.rA = this.rB = this.rate = this.target; this.rateStep = 0;
    }
    setSemitones(n) { this.target = Math.pow(2, n / 12); }
    // 本物の音が来る前に、雑音で一度動かしておく(JIT にコンパイルさせて、最初の音で処理が遅れないように)
    warmUp() {
      const n = 128, a = new Float32Array(n), b = new Float32Array(n), o1 = new Float32Array(n), o2 = new Float32Array(n);
      const keep = this.target; let seed = 1;
      for (let blk = 0; blk < 60 * this.Ha / n; blk++) {
        if (blk % 40 === 0) this.target = 0.6 + 0.02 * (blk % 70);
        for (let i = 0; i < n; i++) { seed = (seed * 1103515245 + 12345) & 0x7fffffff; a[i] = seed / 0x7fffffff - 0.5; b[i] = a[i] * 0.5; }
        this.process(a, b, o1, o2, n);
      }
      this.target = keep; this.reset();
    }
    // いまの比率での遅延(サンプル数)
    get latency() { return this.N / 2 + this.LAG / this.ratio; }

    _frame() {
      const N = this.N, K = this.K, Ha = this.Ha, nm = N - 1, re = this.re, im = this.im, win = this.win;
      const wi = this.inCount & nm, h2 = N >> 1;
      for (let i = 0; i < N; i++) { const j = (i + h2) & nm, s = (wi + i) & nm; re[j] = win[i] * this.inL[s]; im[j] = win[i] * this.inR[s]; }
      this.fft.transform(re, im);
      const Lr = this.Lr, Li = this.Li, Rr = this.Rr, Ri = this.Ri, pw = this.pw;
      let pmax = 0;
      for (let k = 0; k < K; k++) {
        const m = (N - k) & nm;
        Lr[k] = 0.5 * (re[k] + re[m]); Li[k] = 0.5 * (im[k] - im[m]);
        Rr[k] = 0.5 * (im[k] + im[m]); Ri[k] = 0.5 * (re[m] - re[k]);
        const p = Lr[k] * Lr[k] + Li[k] * Li[k] + Rr[k] * Rr[k] + Ri[k] * Ri[k];
        pw[k] = p; if (p > pmax) pmax = p;
      }
      // 比率の変化: 読み出し位置の進む速さは、1 ホップ(44.1kHz で 11.6 ミリ秒)かけて rA から rB へ直線的に変わる。
      // このフレームは、前のフレームから読み出し位置が進んだのとちょうど同じだけ先に置くので、両者は常にそろう。
      // 次の変化(rB → target)はここから始まる。
      const r = 0.5 * (this.rA + this.rB);          // いま終わったホップの平均の比率
      this.rA = this.rB; this.rB = this.target;
      const rNext = 0.5 * (this.rA + this.rB);      // これから始まるホップの平均の比率
      this.rate = this.rA; this.rateStep = (this.rB - this.rA) / Ha;
      this.ratio = this.rB;
      let sNew = this.sC + r * Ha;
      if (r === 1 && this.rB === 1) { // 原曲キーでは、時間軸を少しずつ整数のサンプル位置にずらしていく(1 フレームに 0.05 サンプル以下)
        const fr = sNew - Math.round(sNew);
        sNew = Math.abs(fr) < 1e-3 ? Math.round(sNew) : sNew - 0.1 * fr;
      }
      const Hs = Math.round(sNew) - Math.round(this.sC), dH = Hs - Ha;
      const theta = this.theta, pth = this.ptheta;
      if (this.first) {
        theta.fill(0);
      } else if (r === 1 && this.rB === 1) {
        // 原曲キーに戻った: 変えていたキーの回転量を少しずつ 0 に戻す(一気に戻すと、伸ばしている音が
        // 1 窓分へこむ)。2 秒ほどで、ただの遅延になる
        let mx = 0;
        for (let k = 0; k < K; k++) { const t = pth[k] * 0.97; theta[k] = t; if (t > mx) mx = t; else if (-t > mx) mx = -t; }
        if (mx < 0.02) theta.fill(0);
      } else {
        const ppw = this.ppw, done = this.done, pLr = this.pLr, pLi = this.pLi, pRr = this.pRr, pRi = this.pRi;
        let prevMax = 0; for (let k = 0; k < K; k++) if (ppw[k] > prevMax) prevMax = ppw[k];
        const abstol = this.tol * Math.max(pmax, prevMax);
        const vert = this.mode === 'pghi';
        // パワーの大きい順に取り出すヒープ。id < K は前のフレームのビン、id >= K は今のフレームのビン
        const key = this.key, h = this.heap; let hn = 0, todo = 0;
        key.set(ppw, 0); key.set(pw, K);
        for (let k = 0; k < K; k++) {
          if (pw[k] > abstol) { done[k] = 0; todo++; h[hn++] = k; } else { done[k] = 1; theta[k] = 0; }
        }
        for (let i0 = (hn >> 1) - 1; i0 >= 0; i0--) { // ヒープを組む
          const v = h[i0], kv = key[v]; let i = i0;
          for (;;) { let c = 2 * i + 1; if (c >= hn) break; if (c + 1 < hn && key[h[c + 1]] > key[h[c]]) c++; if (key[h[c]] <= kv) break; h[i] = h[c]; i = c; }
          h[i] = v;
        }
        const bscale = r - 1; // (beta - 1)、beta = r: 群遅延は引き伸ばした分だけ伸びる
        const expK = TWO_PI * Ha / N, dHa = dH / Ha;
        while (todo > 0 && hn > 0) {
          const id = h[0];
          { // 取り出す
            const v = h[--hn], kv = key[v]; let i = 0;
            for (;;) { let c = 2 * i + 1; if (c >= hn) break; if (c + 1 < hn && key[h[c + 1]] > key[h[c]]) c++; if (key[h[c]] <= kv) break; h[i] = h[c]; i = c; }
            h[i] = v;
          }
          if (id < K) {
            const k = id; if (done[k]) continue;
            // 時間方向: theta += (Hs - Ha) * 瞬時周波数(後退差分)
            let ar, ai;
            if (Lr[k] * Lr[k] + Li[k] * Li[k] >= Rr[k] * Rr[k] + Ri[k] * Ri[k]) { ar = Lr[k] * pLr[k] + Li[k] * pLi[k]; ai = Li[k] * pLr[k] - Lr[k] * pLi[k]; }
            else { ar = Rr[k] * pRr[k] + Ri[k] * pRi[k]; ai = Ri[k] * pRr[k] - Rr[k] * pRi[k]; }
            const expct = expK * k;
            let d = Math.atan2(ai, ar) - expct; d -= TWO_PI * Math.round(d / TWO_PI);
            let t = pth[k] + dHa * (expct + d); t -= TWO_PI * Math.round(t / TWO_PI);
            theta[k] = t; done[k] = 1; todo--;
            { const v = K + k, kv = key[v]; let i = hn++; while (i > 0) { const p = (i - 1) >> 1; if (key[h[p]] >= kv) break; h[i] = h[p]; i = p; } h[i] = v; }
          } else {
            const k = id - K;
            const useL = Lr[k] * Lr[k] + Li[k] * Li[k] >= Rr[k] * Rr[k] + Ri[k] * Ri[k];
            for (let s = -1; s <= 1; s += 2) {
              const q = k + s; if (q < 0 || q >= K || done[q]) continue;
              let t = theta[k];
              if (vert) {
                // 周波数方向: theta(q) = theta(k) + (beta - 1) * (phase(q) - phase(k))
                let ar, ai;
                if (useL) { ar = Lr[q] * Lr[k] + Li[q] * Li[k]; ai = Li[q] * Lr[k] - Lr[q] * Li[k]; }
                else { ar = Rr[q] * Rr[k] + Ri[q] * Ri[k]; ai = Ri[q] * Rr[k] - Rr[q] * Ri[k]; }
                t += bscale * Math.atan2(ai, ar); t -= TWO_PI * Math.round(t / TWO_PI);
              }
              theta[q] = t; done[q] = 1; todo--;
              { const v = K + q, kv = key[v]; let i = hn++; while (i > 0) { const p = (i - 1) >> 1; if (key[h[p]] >= kv) break; h[i] = h[p]; i = p; } h[i] = v; }
            }
          }
        }
        if (todo > 0) for (let k = 0; k < K; k++) if (!done[k]) theta[k] = 0;
        theta[0] = 0;
      }
      // このフレームを「前のフレーム」として覚えておく
      this.pLr.set(Lr); this.pLi.set(Li); this.pRr.set(Rr); this.pRi.set(Ri); this.ppw.set(pw); pth.set(theta);
      // 回転し、(r > 1 なら)高い方を切り、L + iR のスペクトルに組み直す
      const rc = Math.max(r, this.rB), kcut = rc > 1 ? Math.min(K - 1, Math.floor(0.98 * (N / 2) / rc)) : K - 1;
      re.fill(0); im.fill(0);
      for (let k = 0; k <= kcut; k++) {
        const c = Math.cos(theta[k]), s = Math.sin(theta[k]);
        const lr = Lr[k] * c - Li[k] * s, li = Lr[k] * s + Li[k] * c, rr = Rr[k] * c - Ri[k] * s, ri = Rr[k] * s + Ri[k] * c;
        if (k === 0 || k === N / 2) { re[k] = Lr[k]; im[k] = Rr[k]; continue; }
        re[k] = lr - ri; im[k] = li + rr;
        re[N - k] = lr + ri; im[N - k] = rr - li;
      }
      this.fft.transform(im, re); // 実部と虚部を入れ替えて渡すと逆変換になる(1/N はあとで掛ける)
      // 引き伸ばした時間軸に重ねて足す
      const start = Math.round(sNew) - h2, newEnd = start + N, mask = this.mask, accL = this.accL, accR = this.accR, wsum = this.wsum;
      for (let p = this.end; p < newEnd; p++) { const j = p & mask; accL[j] = 0; accR[j] = 0; wsum[j] = 0; }
      if (newEnd > this.end) this.end = newEnd;
      const inv = 1 / N, edge = this.edge;
      for (let i = 0; i < N; i++) {
        const p = start + i; if (p < edge) continue;
        const j = p & mask, y = (i + h2) & nm, w = win[i];
        accL[j] += w * re[y] * inv; accR[j] += w * im[y] * inv; wsum[j] += w * w;
      }
      this.sC = sNew;
      // 次のフレームの始まりより前は、もう足されない(確定): 窓の重なりで割って正規化する
      const newEdge = Math.round(sNew + rNext * Ha) - h2;
      for (let p = this.edge; p < newEdge; p++) { const j = p & mask, g = 1 / Math.max(wsum[j], 0.1); accL[j] *= g; accR[j] *= g; }
      if (newEdge > this.edge) this.edge = newEdge;
      this.rp = this.sC - this.LAG;
      this.first = false;
      if (this.sC > 16777216) { const off = this.M * Math.floor((this.sC - 4 * N) / this.M); this.sC -= off; this.rp -= off; this.edge -= off; this.end -= off; }
    }

    // inL/inR/outL/outR: 長さ n の Float32Array(AudioWorklet からは n = 128)
    process(inL, inR, outL, outR, n) {
      const nm = this.N - 1, Ha = this.Ha, mask = this.mask, accL = this.accL, accR = this.accR, tab = this.sinc, half = TAPS / 2 - 1;
      for (let i = 0; i < n; i++) {
        const w = this.inCount & nm;
        this.inL[w] = inL[i]; this.inR[w] = inR[i]; this.inCount++;
        const rp = this.rp, ip = Math.floor(rp), f = (rp - ip) * PHASES, p0 = Math.floor(f), a = f - p0;
        const t0 = p0 * TAPS, t1 = t0 + TAPS, base = ip - half;
        let l = 0, rr = 0;
        for (let j = 0; j < TAPS; j++) {
          const c = tab[t0 + j] + a * (tab[t1 + j] - tab[t0 + j]), q = (base + j) & mask;
          l += c * accL[q]; rr += c * accR[q];
        }
        outL[i] = l; outR[i] = rr;
        this.rp = rp + this.rate + 0.5 * this.rateStep; this.rate += this.rateStep;
        if (this.inCount % Ha === 0) this._frame();
      }
    }
  }

  root.PitchShifter = PitchShifter;

  if (typeof AudioWorkletProcessor !== 'undefined' && typeof registerProcessor === 'function') {
    class KeyShiftProcessor extends AudioWorkletProcessor {
      static get parameterDescriptors() {
        return [{ name: 'semitones', defaultValue: 0, minValue: -12, maxValue: 12, automationRate: 'k-rate' }];
      }
      constructor() {
        super();
        this.ps = new PitchShifter(sampleRate);
        this.ps.warmUp();
        this.zero = new Float32Array(128);
        this.tail = 0; this.blocks = 0; this.last = null;
        this.port.onmessage = (e) => { if (e.data && e.data.type === 'flush') this.ps.reset(); };
      }
      process(inputs, outputs, params) {
        const inp = inputs[0], out = outputs[0], n = out[0].length;
        if (this.zero.length !== n) this.zero = new Float32Array(n);
        const st = params.semitones[0];
        if (st !== this.last) { this.ps.setSemitones(st); this.last = st; }
        const has = inp && inp.length > 0;
        if (has) this.tail = 3 * this.ps.N; else if (this.tail <= 0) return true; else this.tail -= n; // 入力が無くなったら、残りを出し切ってから休む
        const l = has ? inp[0] : this.zero, r = has ? (inp[1] || inp[0]) : this.zero;
        this.ps.process(l, r, out[0], out[1] || new Float32Array(n), n);
        if (out[1] === undefined) { /* モノラル出力: 左だけ */ }
        if ((++this.blocks & 63) === 0) this.port.postMessage({ type: 'latency', seconds: this.ps.latency / sampleRate });
        return true;
      }
    }
    registerProcessor('key-shift', KeyShiftProcessor);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
