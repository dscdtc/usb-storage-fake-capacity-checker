/* ============================================================
   U盘体检台 — 交互逻辑
   纯原生 JS，无依赖。所有计算都在浏览器本地完成。
   ============================================================ */
'use strict';

/* 仓库地址：填上后顶栏会自动出现 GitHub 链接 */
const REPO_URL = 'https://github.com/dscdtc/usb-storage-fake-capacity-checker';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

/* ---------------- 通用工具 ---------------- */

const fmtInt = n => Math.round(n).toLocaleString('en-US');

function fmtGB(bytes) { return (bytes / 1e9).toFixed(2); }
function fmtGiB(bytes) { return (bytes / GIB).toFixed(2); }

/** 解析 "250GiB" / "2TB" / "268435456000" → 字节数 */
function parseCap(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number') return v > 0 && isFinite(v) ? Math.round(v) : null;
  const s = String(v).trim().replace(/[, _]/g, '');
  if (!s) return null;
  const m = s.match(/^([\d.]+)(b|kb|mb|gb|tb|kib|mib|gib|tib|k|m|g|t)?$/i);
  if (!m) return null;
  const n = parseFloat(m[1]);
  if (!isFinite(n) || n <= 0) return null;
  // 没写单位时：小数 / 小数值（< 1e5）按 GB 理解；很大的纯数字按字节理解，
  // 否则 JSON 里 "268435456000" 这种字节串会被当成 GB，直接放大 10 亿倍。
  if (!m[2]) return Math.round(n * (n < 1e5 ? 1e9 : 1));
  const u = m[2].toLowerCase();
  const mult = {
    b: 1, k: 1e3, m: 1e6, g: 1e9, t: 1e12,
    kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12,
    kib: 1024, mib: 1024 ** 2, gib: GIB, tib: 1024 ** 4,
  };
  return Math.round(n * (mult[u] ?? 1e9));
}

/** 建议分区大小（MB），取真实容量 × 98% */
const suggestMB = bytes => Math.max(1, Math.floor(bytes * 0.98 / MIB));

/** Wilson 95% 置信区间，比正态近似更稳，小样本不会越界 */
function wilson(k, n, z = 1.96) {
  if (!n) return [0, 0];
  const p = k / n;
  const d = 1 + z * z / n;
  const centre = (p + z * z / (2 * n)) / d;
  const half = (z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d;
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}

function copyText(text, btn) {
  const done = () => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = '已复制';
    btn.classList.add('done');
    setTimeout(() => { btn.textContent = old; btn.classList.remove('done'); }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback());
  } else { fallback(); }

  function fallback() {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (_) { /* 忽略 */ }
    ta.remove();
  }
}

function toast(msg) {
  const el = document.createElement('div');
  el.textContent = msg;
  el.style.cssText = `position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(8px);
    background:#0f172a;color:#fff;font-size:13px;padding:10px 18px;border-radius:99px;z-index:999;
    box-shadow:0 10px 30px -10px rgba(0,0,0,.5);opacity:0;transition:opacity .2s,transform .2s`;
  document.body.appendChild(el);
  requestAnimationFrame(() => { el.style.opacity = '1'; el.style.transform = 'translateX(-50%) translateY(0)'; });
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 220);
  }, 2200);
}

/* ---------------- 顶栏仓库链接 ---------------- */
if (REPO_URL) {
  const link = $('#repoLink');
  link.href = REPO_URL;
  link.hidden = false;
  link.target = '_blank';
  link.rel = 'noopener';
}

/* ============================================================
   ① 风险速诊
   ============================================================ */

const MODEL_STRONG = [
  'vendorco', 'productcode', 'product code', 'flash disk', 'general udisk',
  'mass storage', 'generic', 'no name', 'chiavetta', 'usb2.0 flash',
];
const MODEL_WEAK = ['usb disk', 'usb flash', 'usb device'];

/* 大致的正品参考价（元），仅用于风险排序，不是行情报价 */
const PRICE_BENCH = [
  [32, 25], [64, 40], [128, 75], [256, 140],
  [512, 280], [1024, 600], [2048, 1200], [4096, 2600],
];

function benchPrice(bytes) {
  const gb = bytes / 1e9;
  let best = null;
  for (const [cap, price] of PRICE_BENCH) {
    if (gb <= cap * 1.15) { best = price; break; }
  }
  return best ?? PRICE_BENCH[PRICE_BENCH.length - 1][1];
}

function runDiagnose() {
  const model = ($('#dModel').value || '').toLowerCase();
  const capBytes = parseCap($('#dCap').value);
  const channel = $('#dChannel').value;
  const price = parseFloat($('#dPrice').value);
  const symptoms = $$('#dSymptoms input:checked').map(i => Number(i.dataset.w));

  const reasons = [];
  let score = 0;
  const add = (pts, text) => { score += pts; reasons.push({ pts, text }); };

  // 型号串
  if (model) {
    if (MODEL_STRONG.some(k => model.includes(k))) {
      add(30, `型号串「${$('#dModel').value.trim()}」属于无品牌 / 公版主控签名，是扩容盘最常见的伪装`);
    } else if (MODEL_WEAK.some(k => model.includes(k))) {
      add(12, `型号串「${$('#dModel').value.trim()}」不含厂商信息，品牌归属无法确认`);
    }
  }

  // 容量档位
  if (capBytes && capBytes >= 900e9) {
    add(12, `标称 ${fmtGB(capBytes)} GB：U 盘做到 1 TB 以上本来就少且贵，假货最爱在这个档位动手`);
  }

  // 价格
  if (isFinite(price) && price > 0 && capBytes) {
    const bench = benchPrice(capBytes);
    const ratio = price / bench;
    if (ratio < 0.5) {
      add(30, `实付 ¥${price}，不到同容量正品参考价 ¥${bench} 的一半（${(ratio * 100).toFixed(0)}%），低得不合常理`);
    } else if (ratio < 0.75) {
      add(15, `实付 ¥${price}，明显低于同容量参考价 ¥${bench}，需要留意`);
    }
  }

  // 渠道
  if (channel === 'used' || channel === 'gift') add(8, '二手 / 赠品来源，无法追溯渠道与售后');
  else if (channel === 'third') add(4, '第三方店铺，货源需自行核实');

  // 症状（权重直接取自经验，合并后封顶 55）
  if (symptoms.length) {
    const sum = Math.min(55, symptoms.reduce((a, b) => a + b, 0));
    const texts = [];
    if (symptoms.includes(35)) texts.push('文件复制成功却打不开');
    if (symptoms.includes(40)) texts.push('已存文件自行消失');
    if (symptoms.includes(20)) texts.push('写入速度断崖式下跌');
    if (symptoms.includes(25)) texts.push('容量与标称不一致');
    if (symptoms.includes(15)) texts.push('预装可疑文件');
    if (symptoms.includes(10)) texts.push('大文件复制中断');
    add(sum, `你遇到的症状（${texts.join('、')}）与扩容盘的典型表现高度吻合`);
  }

  score = Math.min(99, score);
  reasons.sort((a, b) => b.pts - a.pts);
  if (!reasons.length) reasons.push({ pts: 0, text: '没有命中任何已知风险特征——但这只说明「不像典型假盘」，实测才有结论' });

  let level, color, advice;
  if (score >= 75)      { level = '极高风险'; color = '#dc2626'; advice = '几乎可以确定是扩容盘。建议直接做第 ② 步的二分定位，拿到证据后考虑退货。'; }
  else if (score >= 50) { level = '高风险';   color = '#ea580c'; advice = '多个特征同时命中。强烈建议实测——空盘最安全，几分钟就出结果。'; }
  else if (score >= 25) { level = '中等风险'; color = '#d97706'; advice = '有可疑之处，但证据不足。花两分钟跑一次二分定位，成本很低。'; }
  else                  { level = '低风险';   color = '#059669'; advice = '从描述看不太像假盘。如果盘里已有重要数据，仍然建议先测再放心用。'; }

  $('#dEmpty').hidden = true;
  const out = $('#dOut');
  out.hidden = false;
  out.innerHTML = `
    <div class="score-top">
      <div class="ring" style="--p:${score};--c:${color}">
        <div class="ring-txt"><b>${score}</b><span>风险分</span></div>
      </div>
      <div class="score-meta">
        <div class="score-level" style="color:${color}">${level}</div>
        <div class="score-advice">${advice}</div>
      </div>
    </div>
    <div class="micro" style="margin-bottom:10px">逐条依据（分数越高越可疑）</div>
    <ul class="reasons">
      ${reasons.map(r => `<li><span class="pts${r.pts ? '' : ' zero'}">${r.pts ? '+' + r.pts : '—'}</span><span>${r.text}</span></li>`).join('')}
    </ul>
    <p class="note">风险分只做排序，<strong>不是证据</strong>：一支没有风险特征的盘也可能虚标，一支长得像公版的盘也可能货真价实。</p>
  `;
}

/* ============================================================
   ② 取证命令
   ============================================================ */

function fieldVal(sel, fallback) {
  const v = ($(sel).value || '').trim();
  return v || fallback;
}

function buildMeasureCmd(letter, mode) {
  const L = letter.toUpperCase();
  if (mode === 'probe')  return `python usb_quickprobe.py --letter ${L} --count 1024 --yes`;
  if (mode === 'full')   return `python usb_fake_capacity.py identify ${L}:\npython usb_fake_capacity.py check ${L} --yes`;
  return `python usb_quickprobe.py --letter ${L} --locate --yes`;
}

function refreshCommands() {
  const letter = (fieldVal('#mLetter', 'E')).slice(0, 1).toUpperCase();
  const mode = $('#mMode').value;
  $('#mCmd').textContent = buildMeasureCmd(letter, mode);
  if (state.calc) {
    state.calc.letter = letter;
    const kv = $('#kvDisk');
    if (kv) kv.textContent = `Disk ${state.calc.disk} · ${letter}:`;
    renderRestore();
  }
}

/* ============================================================
   ③ 计算真实容量
   ============================================================ */

const state = { calc: null };

$$('#calcTabs .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    $$('#calcTabs .tab').forEach(t => t.classList.toggle('is-on', t === tab));
    $$('.tabpane').forEach(p => p.classList.toggle('is-on', p.dataset.pane === tab.dataset.tab));
  });
});

$$('#capChips button').forEach(b => {
  b.addEventListener('click', () => {
    $('#dCap').value = b.dataset.cap;
    $$('#capChips button').forEach(x => x.classList.toggle('is-on', x === b));
  });
});

function calcError(msg) {
  $('#cEmpty').hidden = true;
  const out = $('#cOut');
  out.hidden = false;
  out.innerHTML = `<div class="alert alert-bad">${msg}</div>`;
  state.calc = null;
  renderRestore();
  $('#shareBtn').disabled = true;
}

/**
 * 统一渲染结果。
 * data = { bytes, lo, hi, nominal, method, note }
 */
function renderCalc(data) {
  const { bytes, lo, hi, nominal, method, note } = data;
  if (!bytes || bytes <= 0) return calcError('算出来的容量是 0，请检查输入。');

  const mb = suggestMB(bytes);
  const pct = nominal ? bytes / nominal * 100 : null;
  const exact = lo === hi;
  const letter = (fieldVal('#mLetter', 'E')).slice(0, 1).toUpperCase();
  const disk = fieldVal('#mDisk', '3');

  state.calc = { bytes, lo, hi, nominal, mb, letter, disk, method };

  $('#cEmpty').hidden = true;
  const out = $('#cOut');
  out.hidden = false;

  const barW = nominal ? Math.max(1.5, Math.min(100, pct)) : 100;

  out.innerHTML = `
    <div class="micro">${method}</div>
    <div class="big-num" style="margin-top:6px">${exact ? '' : '≈ '}${fmtGB(bytes)} GB</div>
    <div class="big-unit">
      ${fmtGiB(bytes)} GiB · ${fmtInt(bytes)} 字节
      ${pct !== null ? ` · 标称的 <b style="color:#dc2626">${pct.toFixed(2)}%</b>` : ''}
    </div>

    ${nominal ? `
    <div class="bar-compare">
      <div class="bar-track"><span class="bar-real" style="width:${barW}%"></span></div>
      <div class="bar-legend">
        <span>真实 ≈ ${fmtGB(bytes)} GB</span>
        <span>标称 ${fmtGB(nominal)} GB</span>
      </div>
    </div>` : ''}

    <dl class="kv">
      <dt>真实容量${exact ? '' : '（区间）'}</dt>
      <dd>${exact ? fmtInt(bytes) + ' B' : fmtInt(lo) + ' ~ ' + fmtInt(hi) + ' B'}</dd>
      <dt>换算</dt>
      <dd>${fmtGB(bytes)} GB / ${fmtGiB(bytes)} GiB</dd>
      ${nominal ? `<dt>占标称容量</dt><dd>${pct.toFixed(2)}%</dd>` : ''}
      <dt>建议分区</dt>
      <dd>${fmtInt(mb)} MB（真实容量 × 98%）</dd>
      <dt>目标磁盘</dt>
      <dd id="kvDisk">Disk ${disk} · ${letter}:</dd>
    </dl>

    <hr class="dash">
    <div class="row-2">
      <button class="btn btn-sm btn-primary" id="calcToRestore">带入恢复方案</button>
      <button class="btn btn-sm" id="calcShare">生成分享卡片</button>
    </div>
    <p class="note">${note || ''}</p>
  `;

  $('#calcToRestore').addEventListener('click', () => {
    renderRestore();
    $('#restore').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('#calcShare').addEventListener('click', () => { drawShareCard(); $('#shareWrap').hidden = false; });
  $('#shareBtn').disabled = false;

  renderRestore();
}

function calcFromLBA() {
  const good = Number($('#cGood').value);
  const bad = Number($('#cBad').value);
  const sector = Number($('#cSector').value);
  const nominal = parseCap($('#cNominal1').value);

  if (!Number.isFinite(good) || !Number.isFinite(bad) || good < 0 || bad <= 0) {
    return calcError('请填入两个 LBA（最后可用的、第一个失效的），都是十进制整数。');
  }
  if (bad <= good) {
    return calcError('「第一个失效的 LBA」必须大于「最后可用的 LBA」——二分结果是两个相邻扇区。');
  }
  const lo = (good + 1) * sector;   // 已确认可写
  const hi = bad * sector;          // 首个失效位置，真实容量不超过它
  const exact = hi - lo <= sector;
  const mid = Math.round((lo + hi) / 2);

  renderCalc({
    bytes: exact ? hi : mid,
    lo, hi,
    nominal,
    method: `二分结果：好 LBA ${fmtInt(good)}、坏 LBA ${fmtInt(bad)}，扇区 ${sector} B`,
    note: exact
      ? `两个 LBA 相邻，容量边界已锁定在一个 ${sector} B 扇区内，真实容量就是 ${fmtInt(hi)} 字节。`
      : `两个 LBA 不相邻，真实容量落在 ${fmtInt(lo)} ~ ${fmtInt(hi)} 字节之间，这里取中值。`,
  });
}

function calcFromJSON(text) {
  let j;
  try { j = JSON.parse(text); }
  catch (e) { return calcError('JSON 解析失败：' + e.message); }
  if (!j || typeof j !== 'object') return calcError('这不是一个 JSON 对象。');

  const nominal = parseCap(j['上报容量']) || parseCap($('#cNominal1').value);
  const sector = Number(j['扇区大小']) || 4096;

  // ① 二分报告
  if (Number.isFinite(Number(j['真实容量字节'])) && j['真实容量字节'] > 0) {
    const b = Number(j['真实容量字节']);
    return renderCalc({
      bytes: b, lo: b, hi: b, nominal,
      method: `报告直读：${j['设备'] || '未知设备'} · 物理磁盘 ${j['物理磁盘'] ?? '?'} · ${j['时间'] || ''}`,
      note: `报告里已经给出精确边界（LBA ${fmtInt(j['首个失效LBA'] ?? 0)}），无需再算。建议分区 ${fmtInt(j['建议分区MB'] || suggestMB(b))} MB。`,
    });
  }

  // ② 抽样报告
  const n = Number(j['抽样扇区数']);
  const ok = Number(j['一致扇区数']);
  if (n > 0 && Number.isFinite(ok)) return calcFromSample(n, ok, nominal, j);

  // ③ 其他：尝试用 LBA 推
  const g = Number(j['最大成功LBA'] ?? j['最大有效LBA']);
  const b = Number(j['首个异常LBA'] ?? j['首个失效LBA']);
  if (Number.isFinite(g) && Number.isFinite(b) && b > g) {
    const lo = (g + 1) * sector, hi = b * sector;
    return renderCalc({
      bytes: hi, lo, hi, nominal,
      method: `报告中的边界：好 LBA ${fmtInt(g)}、坏 LBA ${fmtInt(b)}`,
      note: '真实容量 ≤ 首个异常位置，这是硬上界。',
    });
  }

  return calcError('报告里没有可识别的字段。支持 usb_quickprobe.py 的抽样报告与二分报告。');
}

function calcFromSample(n, ok, nominal, report) {
  if (!(n > 0) || ok < 0 || ok > n) return calcError('抽样数必须为正，且一致数不能大于抽样数。');
  if (!nominal) return calcError('抽样估算需要一个标称容量作为基准。');
  if (ok === 0) {
    return calcError(
      `${n} 个采样点里没有一个能读回自己写入的标记——这不是「容量为 0」，`
      + '而是采样区整体无法正常读写，或主控把这些位置全部丢弃了。'
      + '<br>这种情况下统计估算失效，请直接用二分定位（<code>--locate</code>）确定边界。'
    );
  }

  const p = ok / n;
  let [plo, phi] = wilson(ok, n);
  const est = p * nominal;
  let lo = plo * nominal;
  let hi = phi * nominal;

  // 若有首个异常位置，它是硬上界
  const firstBad = Number(report && (report['首个异常LBA'] ?? report['首个失效LBA']));
  const hardCap = Number.isFinite(firstBad) && firstBad > 0 ? firstBad * 4096 : null;
  if (hardCap) hi = Math.min(hi, hardCap);

  const note = `命中率 ${ok}/${n} = ${(p * 100).toFixed(2)}%，95% 置信区间 ${(plo * 100).toFixed(2)}% ~ ${(phi * 100).toFixed(2)}%`
    + (hardCap ? `；硬上界 ${fmtGB(hardCap)} GB（首个异常位置）。` : '。')
    + ' 样本越少区间越宽，这正说明单靠抽样无法给出精确值。';

  renderCalc({
    bytes: est, lo, hi, nominal,
    method: `抽样统计：${n} 个采样点，${ok} 个读到自己的标记`,
    note,
  });
}

function runCalc() {
  const pane = $('.tabpane.is-on').dataset.pane;
  if (pane === 'lba') return calcFromLBA();
  if (pane === 'json') {
    const text = $('#cJson').value.trim();
    if (!text) return calcError('请先粘贴报告 JSON，或点「选择 JSON 文件」。');
    return calcFromJSON(text);
  }
  return calcFromSample(
    Number($('#sTotal').value),
    Number($('#sOk').value),
    parseCap($('#cNominal2').value)
  );
}

/* ============================================================
   ④ 恢复方案
   ============================================================ */

function diskpartBody(c) {
  return [
    `select disk ${c.disk}`,
    'clean',
    `create partition primary size=${c.mb}`,
    'format fs=exfat quick label="USB"',
    `assign letter=${c.letter}`,
  ].join('\n');
}

function renderRestore() {
  const c = state.calc;
  if (!c) {
    $('#rScript').textContent = '等待第 ③ 步的计算结果…';
    $('#rCmd').textContent = '等待第 ③ 步的计算结果…';
    $('#rDownload').disabled = true;
    $('#rMeta').textContent = '建议分区大小将按真实容量 × 98% 计算';
    return;
  }
  $('#rScript').textContent = diskpartBody(c);
  $('#rCmd').textContent =
    `python usb_fake_capacity.py restore --disk ${c.disk} --size-mb ${c.mb} --letter ${c.letter} --dry-run\n` +
    `python usb_fake_capacity.py restore --disk ${c.disk} --size-mb ${c.mb} --letter ${c.letter} --yes`;
  $('#rDownload').disabled = false;
  $('#rMeta').textContent = `Disk ${c.disk} → ${fmtInt(c.mb)} MB，约 ${fmtGB(c.bytes * 0.98)} GB`;
}

$('#rDownload').addEventListener('click', () => {
  const c = state.calc;
  if (!c) return;
  const txt = [
    '# 由 U盘体检台 生成 —— 执行前请核对磁盘号，clean 会清空整块磁盘',
    `# 真实容量 ${fmtInt(c.bytes)} B (${fmtGB(c.bytes)} GB / ${fmtGiB(c.bytes)} GiB)`,
    `# 建议分区 ${fmtInt(c.mb)} MB = 真实容量 × 98%`,
    '',
    'diskpart',
    diskpartBody(c),
    'exit',
    '',
  ].join('\r\n');
  const blob = new Blob([txt], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `diskpart-restore-disk${c.disk}.txt`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast('已下载，双击前请再核对磁盘号');
});

/* ============================================================
   分享卡片
   ============================================================ */

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawShareCard() {
  const c = state.calc;
  const cv = $('#shareCanvas');
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;

  const g = ctx.createLinearGradient(0, 0, W, H);
  g.addColorStop(0, '#111c33');
  g.addColorStop(1, '#0b1220');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);

  const glow = ctx.createRadialGradient(W * 0.85, 60, 0, W * 0.85, 60, W * 0.9);
  glow.addColorStop(0, 'rgba(79,70,229,.35)');
  glow.addColorStop(1, 'rgba(79,70,229,0)');
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, W, H);

  const pad = 82;
  ctx.textBaseline = 'alphabetic';

  // 顶部标识
  ctx.fillStyle = '#a5b4fc';
  ctx.font = '600 30px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillText('U盘体检台', pad, 132);
  ctx.fillStyle = 'rgba(148,163,184,.85)';
  ctx.font = '400 24px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillText('扩容盘容量检测', pad + 176, 132);

  // 主标题
  ctx.fillStyle = '#f8fafc';
  ctx.font = '800 76px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillText('这支 U 盘', pad, 268);
  ctx.fillStyle = '#fca5a5';
  ctx.fillText('只有标称的', pad, 360);

  // 百分比
  const pct = c.nominal ? (c.bytes / c.nominal * 100) : null;
  ctx.font = '800 168px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillStyle = '#fecaca';
  ctx.fillText(pct !== null ? pct.toFixed(1) + '%' : '—', pad - 6, 540);

  // 数据行
  const line = (label, value, y, big) => {
    ctx.fillStyle = 'rgba(148,163,184,.9)';
    ctx.font = '400 27px "PingFang SC","Microsoft YaHei",sans-serif';
    ctx.fillText(label, pad, y);
    ctx.fillStyle = big ? '#f8fafc' : '#cbd5e1';
    ctx.font = `${big ? 700 : 500} 34px ui-monospace,Consolas,monospace`;
    ctx.fillText(value, pad + 240, y);
  };
  if (c.nominal) line('标称容量', fmtGB(c.nominal) + ' GB', 640);
  line('真实容量', fmtGB(c.bytes) + ' GB', 704, true);
  line('换算', fmtGiB(c.bytes) + ' GiB', 768);
  line('建议分区', fmtInt(c.mb) + ' MB', 832);

  // 比例条
  const barY = 892, barW = W - pad * 2, barH = 18;
  ctx.fillStyle = 'rgba(148,163,184,.18)';
  roundRect(ctx, pad, barY, barW, barH, barH / 2); ctx.fill();
  if (pct !== null) {
    const w = Math.max(barH, barW * Math.min(1, pct / 100));
    const bg = ctx.createLinearGradient(pad, 0, pad + w, 0);
    bg.addColorStop(0, '#dc2626'); bg.addColorStop(1, '#f97316');
    ctx.fillStyle = bg;
    roundRect(ctx, pad, barY, w, barH, barH / 2); ctx.fill();
  }

  // 结论句（拆成两行，避免标点被挤到新行）
  ctx.fillStyle = '#e2e8f0';
  ctx.font = '400 30px "PingFang SC","Microsoft YaHei",sans-serif';
  const tail1 = pct !== null
    ? `超出 ${fmtGB(c.bytes)} GB 的写入会被静默丢弃，`
    : `超出真实容量的写入会被静默丢弃，`;
  ctx.fillText(tail1, pad, 986);
  ctx.fillText('文件其实早已损坏，系统却不会报错。', pad, 1032);

  // 方法摘要：填充留白，同时说明结论不是猜的
  ctx.strokeStyle = 'rgba(148,163,184,.18)';
  ctx.beginPath(); ctx.moveTo(pad, 1088); ctx.lineTo(W - pad, 1088); ctx.stroke();

  const metrics = [
    ['约 30 次', '单扇区探测'],
    ['2 MB', '总写入量'],
    ['< 2 分钟', '拿到精确值'],
  ];
  metrics.forEach(([big, small], i) => {
    const x = pad + i * 306;
    ctx.fillStyle = '#f1f5f9';
    ctx.font = '700 40px ui-monospace,Consolas,monospace';
    ctx.fillText(big, x, 1150);
    ctx.fillStyle = 'rgba(148,163,184,.9)';
    ctx.font = '400 22px "PingFang SC","Microsoft YaHei",sans-serif';
    ctx.fillText(small, x, 1184);
  });

  // 页脚
  ctx.strokeStyle = 'rgba(148,163,184,.18)';
  ctx.beginPath(); ctx.moveTo(pad, H - 190); ctx.lineTo(W - pad, H - 190); ctx.stroke();

  ctx.fillStyle = '#a5b4fc';
  ctx.font = '600 28px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillText('#WorkBuddy  #1024程序员节', pad, H - 128);
  ctx.fillStyle = 'rgba(148,163,184,.8)';
  ctx.font = '400 24px "PingFang SC","Microsoft YaHei",sans-serif';
  ctx.fillText('十分钟测完，别再相信包装上的数字', pad, H - 82);

  $('#shareWrap').hidden = false;
}

$('#shareDl').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = $('#shareCanvas').toDataURL('image/png');
  a.download = 'usb-capacity-card.png';
  a.click();
});
$('#shareClose').addEventListener('click', () => { $('#shareWrap').hidden = true; });
$('#shareBtn').addEventListener('click', () => {
  if (!state.calc) return toast('先在第 ③ 步算出真实容量');
  drawShareCard();
  $('#shareWrap').scrollIntoView({ behavior: 'smooth', block: 'center' });
});

/* ============================================================
   ⑤ 浏览器初筛（File System Access API）
   ============================================================ */

const BT = {
  supported: typeof window.showDirectoryPicker === 'function',
  running: false,
  cancel: false,
  sizeMB: 1024,
  CHUNK: 64 * MIB,
  PREFIX: '_wbweb_chk_',
};

function initBrowserTest() {
  const box = $('#fsaSupport');
  if (!BT.supported) {
    box.className = 'alert alert-warn';
    box.innerHTML = '<strong>这个浏览器不支持所需接口。</strong> 请用桌面版 Chrome / Edge 打开本页（且必须是 https 或 localhost）。Safari 与 Firefox 不支持目录写入。';
    $('#bStart').disabled = true;
  } else if (!window.isSecureContext) {
    box.className = 'alert alert-warn';
    box.innerHTML = '<strong>当前不是安全上下文。</strong> 浏览器只在 https 或 localhost 下开放目录写入权限，请用发布后的链接打开。';
    $('#bStart').disabled = true;
  } else {
    box.className = 'alert alert-info';
    box.innerHTML = '<strong>可以开始。</strong> 会请你选择一个 U 盘上的文件夹，只在该文件夹内创建以 <code>_wbweb_chk_</code> 开头的临时文件，测完自动删除。';
  }

  $$('#bSizeChips button').forEach(b => {
    b.addEventListener('click', () => {
      BT.sizeMB = Number(b.dataset.mb);
      $$('#bSizeChips button').forEach(x => x.classList.toggle('is-on', x === b));
      updateSizeHint();
    });
  });
  updateSizeHint();
}

function updateSizeHint() {
  const ram = navigator.deviceMemory || 0;
  const mb = BT.sizeMB;
  const eta = Math.ceil(mb / 12 / 60 * 2.1); // 写 12MiB/s + 校验 20MiB/s 的粗估
  let hint = `写入 ${mb >= 1024 ? (mb / 1024).toFixed(mb % 1024 ? 1 : 0) + ' GB' : mb + ' MB'}，预计 ${eta}–${eta * 2} 分钟。`;
  if (ram && mb < ram * 1024) {
    hint += ` 本机内存约 ${ram} GB，测试量小于内存，回读可能命中系统缓存——只能识破「真实容量比 ${mb} MB 还小」的盘。`;
  }
  $('#bSizeHint').textContent = hint;
}

function fillPattern(buf, seed) {
  const u32 = new Uint32Array(buf.buffer, buf.byteOffset, buf.byteLength >> 2);
  let x = (seed >>> 0) || 0x9e3779b9;
  for (let i = 0; i < u32.length; i++) {
    x ^= x << 13; x >>>= 0;
    x ^= x >>> 17;
    x ^= x << 5;  x >>>= 0;
    u32[i] = x;
  }
  const dv = new DataView(buf.buffer, buf.byteOffset, 16);
  dv.setUint32(0, 0x5742484b, false); // 'WBHK'
  dv.setUint32(4, seed, false);
  dv.setUint32(8, buf.byteLength, false);
  return buf;
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  const ua = new Uint32Array(a.buffer, a.byteOffset, a.length >> 2);
  const ub = new Uint32Array(b.buffer, b.byteOffset, b.length >> 2);
  for (let i = 0; i < ua.length; i++) if (ua[i] !== ub[i]) return false;
  for (let i = ua.length << 2; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function btLog(msg) {
  const el = $('#bLog');
  el.hidden = false;
  el.textContent += msg + '\n';
  el.scrollTop = el.scrollHeight;
}

function btProgress(ratio, phase, stat) {
  $('#bBar').style.width = (ratio * 100).toFixed(1) + '%';
  if (phase) $('#bPhase').textContent = phase;
  if (stat !== undefined) $('#bStat').textContent = stat;
}

async function runBrowserTest() {
  if (!BT.supported || BT.running) return;
  let dir;
  try {
    dir = await window.showDirectoryPicker({ id: 'usb-truth-check', mode: 'readwrite' });
  } catch (e) {
    if (e.name !== 'AbortError') btLog('选择文件夹失败：' + e.message);
    return;
  }

  BT.running = true;
  BT.cancel = false;
  $('#bStart').disabled = true;
  $('#bStop').disabled = false;
  $('#bProgWrap').hidden = false;
  $('#bLog').textContent = '';
  $('#bLog').hidden = false;
  $('#bVerdict').innerHTML = '';
  btProgress(0, '准备', '');

  const total = BT.sizeMB * MIB;
  const files = [];
  let written = 0, verifyBytes = 0;
  let firstBadOffset = null;
  const t0 = performance.now();
  let writeSpeed = 0, readSpeed = 0;

  try {
    btLog(`目标文件夹：${dir.name}`);
    btLog(`测试量：${(total / MIB).toFixed(0)} MB，单文件 ${BT.CHUNK / MIB} MB`);

    /* ---- 写入 ---- */
    let idx = 0;
    while (written < total && !BT.cancel) {
      const size = Math.min(BT.CHUNK, total - written);
      const name = BT.PREFIX + String(idx).padStart(3, '0') + '.bin';
      const fh = await dir.getFileHandle(name, { create: true });
      const w = await fh.createWritable();
      const buf = fillPattern(new Uint8Array(size), 0x1000 + idx);
      await w.write(buf);
      await w.close();
      files.push({ name, size, idx });
      written += size; idx++;
      const el = (performance.now() - t0) / 1000;
      writeSpeed = written / el;
      btProgress(written / total * 0.5, '写入',
        `${(written / MIB).toFixed(0)}/${(total / MIB).toFixed(0)} MB · ${(writeSpeed / MIB).toFixed(1)} MiB/s`);
    }

    if (BT.cancel) throw new Error('__cancel__');

    /* ---- 回读校验 ---- */
    const t1 = performance.now();
    for (const f of files) {
      if (BT.cancel) throw new Error('__cancel__');
      const fh = await dir.getFileHandle(f.name);
      const file = await fh.getFile();
      const got = new Uint8Array(await file.arrayBuffer());
      const exp = fillPattern(new Uint8Array(f.size), 0x1000 + f.idx);
      if (!sameBytes(got, exp)) {
        firstBadOffset = verifyBytes;
        btLog(`✗ 第 ${f.idx + 1} 个文件内容不符（累计 ${(verifyBytes / MIB).toFixed(0)} MB 处）`);
        break;
      }
      verifyBytes += f.size;
      const el = (performance.now() - t1) / 1000;
      readSpeed = verifyBytes / el;
      btProgress(0.5 + verifyBytes / total * 0.5, '回读校验',
        `${(verifyBytes / MIB).toFixed(0)}/${(total / MIB).toFixed(0)} MB · ${(readSpeed / MIB).toFixed(1)} MiB/s`);
    }

    const secs = (performance.now() - t0) / 1000;
    const wMiB = writeSpeed / MIB, rMiB = readSpeed / MIB;
    const cacheSuspect = rMiB > 100 && wMiB > 0 && rMiB > wMiB * 3;

    btLog('');
    btLog(`写入 ${wMiB.toFixed(1)} MiB/s，回读 ${rMiB.toFixed(1)} MiB/s，用时 ${secs.toFixed(0)} 秒`);

    if (firstBadOffset !== null) {
      btLog(`首个异常位置：约 ${(firstBadOffset / MIB).toFixed(0)} MB`);
      $('#bVerdict').innerHTML = `<div class="alert alert-bad">
        <strong>疑似扩容盘。</strong> 在约 ${(firstBadOffset / 1e6).toFixed(0)} MB 之后写入的数据没能原样读回，
        真实容量大约在 ${(firstBadOffset / 1e9).toFixed(2)} GB 以内。建议用第 ② 步的二分定位拿到精确值。
      </div>`;
    } else if (cacheSuspect) {
      $('#bVerdict').innerHTML = `<div class="alert alert-warn">
        <strong>结论不可信。</strong> 回读速度 ${rMiB.toFixed(0)} MiB/s 是写入的 ${(rMiB / Math.max(wMiB, 0.1)).toFixed(1)} 倍，
        数据几乎肯定来自系统缓存，虚标盘在这种条件下也会全部通过。
        请把测试量提到「超过物理内存」的量级，或直接用命令行工具。
      </div>`;
    } else {
      $('#bVerdict').innerHTML = `<div class="alert alert-ok">
        <strong>在这 ${(total / MIB).toFixed(0)} MB 范围内未发现虚标（初筛）。</strong>
        这只能证明「前 ${(total / MIB).toFixed(0)} MB 是真实的」，不能证明整盘足量。
        真实容量若是 64 GB，写 1 GB 当然一切正常。要给结论请用二分定位。
      </div>`;
    }

    /* ---- 清理 ---- */
    let removed = 0;
    for (const f of files) {
      try { await dir.removeEntry(f.name); removed++; } catch (_) { /* 忽略 */ }
    }
    btLog(`已清理 ${removed}/${files.length} 个测试文件`);
  } catch (e) {
    if (e.message === '__cancel__') {
      btLog('已中断。');
      for (const f of files) { try { await dir.removeEntry(f.name); } catch (_) {} }
      btLog('临时文件已尝试清理。');
    } else {
      btLog('出错：' + (e.message || e));
      $('#bVerdict').innerHTML = `<div class="alert alert-bad">执行失败：${e.message || e}</div>`;
    }
  } finally {
    BT.running = false;
    $('#bStart').disabled = false;
    $('#bStop').disabled = true;
    btProgress(1, '完成', '');
  }
}

/* ============================================================
   演示数据（来自真实检测记录）
   ============================================================ */

function loadDemo(auto) {
  $('#dModel').value = 'Flash Disk 3.0';
  $('#dCap').value = '250GiB';
  $('#dChannel').value = 'third';
  $('#dPrice').value = '68';
  $$('#dSymptoms input').forEach(i => { i.checked = false; });
  runDiagnose();

  $('#mLetter').value = 'D';
  $('#mDisk').value = '2';
  $('#mMode').value = 'locate';
  refreshCommands();

  $$('#calcTabs .tab').forEach(t => t.classList.toggle('is-on', t.dataset.tab === 'lba'));
  $$('.tabpane').forEach(p => p.classList.toggle('is-on', p.dataset.pane === 'lba'));
  $('#cGood').value = '15513443';
  $('#cBad').value = '15513444';
  $('#cNominal1').value = '250GiB';
  runCalc();

  toast('已载入 Disk 2 的真实检测记录');
  if (!auto) $('#calc').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ============================================================
   事件绑定
   ============================================================ */

$('#dRun').addEventListener('click', runDiagnose);
$('#demoBtn').addEventListener('click', () => loadDemo(false));
$('#cRun').addEventListener('click', runCalc);
$('#cReset').addEventListener('click', () => {
  ['#cGood', '#cBad', '#cNominal1', '#cJson', '#sTotal', '#sOk', '#cNominal2'].forEach(s => { $(s).value = ''; });
  $('#cEmpty').hidden = false;
  $('#cOut').hidden = true;
  $('#cOut').innerHTML = '';
  state.calc = null;
  renderRestore();
  $('#shareBtn').disabled = true;
  $('#shareWrap').hidden = true;
});
$('#mLetter').addEventListener('input', refreshCommands);
$('#mMode').addEventListener('change', refreshCommands);
$('#mDisk').addEventListener('input', () => {
  if (!state.calc) return;
  state.calc.disk = fieldVal('#mDisk', '3');
  const kv = $('#kvDisk');
  if (kv) kv.textContent = `Disk ${state.calc.disk} · ${state.calc.letter}:`;
  renderRestore();
});
$('#cReadFile').addEventListener('click', () => $('#cFile').click());
$('#cFile').addEventListener('change', async e => {
  const f = e.target.files[0];
  if (!f) return;
  $('#cJson').value = await f.text();
  $$('.tabpane').forEach(p => p.classList.toggle('is-on', p.dataset.pane === 'json'));
  $$('#calcTabs .tab').forEach(t => t.classList.toggle('is-on', t.dataset.tab === 'json'));
  runCalc();
});
$('#bStart').addEventListener('click', runBrowserTest);
$('#bStop').addEventListener('click', () => { BT.cancel = true; btLog('正在中断…'); });

$$('.copy-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const el = document.getElementById(btn.dataset.copy);
    if (el) copyText(el.textContent.trim(), btn);
  });
});

// 回车即算
['#cGood', '#cBad', '#cNominal1', '#sTotal', '#sOk', '#cNominal2'].forEach(s => {
  $(s).addEventListener('keydown', e => { if (e.key === 'Enter') runCalc(); });
});
['#dModel', '#dCap', '#dPrice'].forEach(s => {
  $(s).addEventListener('keydown', e => { if (e.key === 'Enter') runDiagnose(); });
});

/* 初始化 */
initBrowserTest();
refreshCommands();
renderRestore();

/* ?demo=1 自动载入真实案例（便于分享与截图）；?demo=1&jump=calc 直接落到指定区块 */
const qp = new URLSearchParams(location.search);
if (qp.get('demo') === '1') {
  loadDemo(true);
  const jump = qp.get('jump');
  if (jump) {
    const go = () => {
      const el = document.getElementById(jump);
      if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 66);
    };
    requestAnimationFrame(go);
    setTimeout(go, 260);
  }
}
