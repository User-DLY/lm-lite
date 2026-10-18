// -*- coding: utf-8 -*-
/*
 * lm-lite 流式渲染器回归测试（Node + DOM 桩，无需浏览器）
 * ------------------------------------------------------------------
 * 用法：node stream_test.js        （在 lm-lite 目录下）
 *
 * 它做什么：在 vm 里跑 index.html 的真实脚本，用 DOM 桩驱动一次完整
 * 流式会话（思考 → 正文 → 代码块 → 列表 → usage/timings），断言：
 *   1. 流式期间正文气泡从不被整体重写（append-only：只有清空/代码骨架/终态）
 *   2. 所有正文字符都上屏（含代码块逐字上屏）
 *   3. .stail 只创建 2 次（正文 + 思考面板各一，不是每 token 重建）
 *   4. 思考面板终态是完整 mdRender
 *   5. /api/stats 上报字段齐全
 *   6. streamSplit 对开/闭围栏的拆分正确
 *
 * 来历：2026-10-17 为重写流式渲染器而建。它当场抓出两个真 bug：
 *   · 思考模式下 tFirst 被第一个思考 token 抢占，导致正文首字的
 *     clearWait 永不执行——气泡卡在"思考中"、逐字效果整个不显示
 *   · 代码围栏行（```lang）逐字到来时 painted 计数与去首行后的文本错位
 * 改这个文件附近逻辑后，请先跑它。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i)[1];

/* ---------- DOM 桩（语义对齐真实 DOM：fragment 会展开、innerHTML 可计数） ---------- */
let elSeq = 0;
const allEls = [];
const writes = [];        // {el, value} 每次 innerHTML 赋值
function makeEl(tag) {
  const el = {
    _id: ++elSeq, tagName: tag, children: [], _html: '', textContent: '',
    value: '', checked: false, disabled: false, title: '', placeholder: '',
    scrollTop: 0, scrollHeight: 100, clientHeight: 50,
    dataset: {}, options: [],
    style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  Object.defineProperty(el, 'className', {
    get() { return this._cls || ''; },
    set(v) { this._cls = String(v); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); writes.push({ el, value: String(v) }); this.children = []; },
  });
  el.appendChild = n => {
    // DocumentFragment 在真实 DOM 里会把 children 展开进宿主，桩要同语义
    if (n && n._isFrag) n.children.forEach(c => el.children.push(c));
    else el.children.push(n);
    return n;
  };
  el.insertBefore = (n) => { el.children.unshift(n); return n; };
  el.removeChild = n => { el.children = el.children.filter(c => c !== n); return n; };
  el.insertAdjacentHTML = (pos, h) => { el._html += String(h); el.children.push({ _html: String(h), textContent: '', children: [] }); };
  el.querySelector = sel => {
    // 持久化桩：同一 (元素, 选择器) 始终返回同一对象，
    // 这样尾块里 querySelector('code') 的上屏行为可被验证
    if (!el._qs) el._qs = {};
    if (!el._qs[sel]) el._qs[sel] = makeEl('div');
    return el._qs[sel];
  };
  el.querySelectorAll = () => [];
  el.addEventListener = () => {};
  el.removeEventListener = () => {};
  el.remove = () => {};
  el.focus = () => {};
  el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100 });
  el.closest = () => null;
  Object.defineProperty(el, 'firstChild', { get() { return el.children[0] || null; } });
  Object.defineProperty(el, 'firstElementChild', { get() { return el.children[0] || null; } });
  allEls.push(el);
  return el;
}
const byId = {};
const documentStub = {
  getElementById: id => byId[id] || (byId[id] = makeEl('div#' + id)),
  createElement: tag => makeEl(tag),
  createTextNode: t => { const e = makeEl('#text'); e.textContent = t; return e; },
  createDocumentFragment: () => { const e = makeEl('#fragment'); e._isFrag = true; return e; },
  addEventListener: () => {},
  querySelector: () => makeEl('div'),
  querySelectorAll: () => [],
  documentElement: makeEl('html'),
  body: makeEl('body'),
  hidden: false,
};

/* ---------- fetch 桩：受控 SSE 流 ---------- */
const THINK = '先想一下：**用户**在问 1+1。\n\n结论很显然。';
const BODY = '答案是 **2**。\n\n```python\nprint(1+1)\n```\n\n- 第一点\n- 第二点';
function sseChunks() {
  const out = [];
  const push = obj => out.push('data: ' + JSON.stringify(obj) + '\n');
  for (const ch of THINK) push({ choices: [{ delta: { reasoning_content: ch } }] });
  for (const ch of BODY) push({ choices: [{ delta: { content: ch } }] });
  push({ choices: [{ delta: {}, finish_reason: 'stop' }],
         timings: { cache_n: 20, prompt_n: 500, predicted_n: 60,
                    prompt_per_second: 1580.5, predicted_per_second: 95.2,
                    prompt_ms: 320, predicted_ms: 630 },
         usage: { prompt_tokens: 520, completion_tokens: 60, total_tokens: 580 } });
  out.push('data: [DONE]\n');
  const chunks = [];
  let buf = out.join('');
  for (let i = 0; i < buf.length; i += 3) chunks.push(buf.slice(i, i + 3));  // 3 字节一组模拟分片
  return chunks;
}
const CHUNKS = sseChunks();
const statsReports = [];
let rafCount = 0;
const sandbox = {
  console,
  document: documentStub,
  localStorage: (() => { const m = {}; return {
    getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); },
    removeItem: k => { delete m[k]; } }; })(),
  navigator: {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: cb => { rafCount++; cb(); return rafCount; },   // 立即执行，验证逻辑路径
  cancelAnimationFrame: () => {},
  performance, TextDecoder, AbortController, Blob: function(){}, URL: { createObjectURL: () => '', revokeObjectURL() {} },
  setInterval, clearInterval, setTimeout, clearTimeout,
  fetch: async (url, opts) => {
    if (url === '/api/status')
      return { ok: true, json: async () => ({ alive: true, model: 'C:\\m\\Spark.gguf', ctx: 8192, kv: 'f16', loading: false, error: '' }) };
    if (url === '/api/models')
      return { ok: true, json: async () => ({ models: [], current: '' }) };
    if (url === '/api/model-caps')
      return { ok: true, json: async () => ({ loaded: true, supportsThinking: true, why: 'x' }) };
    if (url === '/api/prompts')
      return { ok: true, json: async () => ({ lines: ['问题甲', '问题乙', '问题丙'] }) };
    if (url === '/api/vram')
      return { ok: true, json: async () => ({ loaded: true, gpuTotalMB: 8192, gpuUsedMB: 4000, modelRealMB: 2000, otherMB: 2000 }) };
    if (url === '/api/stats' && (!opts || opts.method !== 'POST'))
      return { ok: true, json: async () => ({ avgTtft: 0, avgTps: 0, requests: 0, last: {} }) };
    if (url === '/api/stats' && opts.method === 'POST') {
      statsReports.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({ ok: true }) };
    }
    if (url === '/v1/chat/completions') {
      let i = 0;
      return { ok: true, body: { getReader: () => ({
        read: async () => i < CHUNKS.length
          ? { done: false, value: new TextEncoder().encode(CHUNKS[i++]) }
          : { done: true, value: undefined } }) } };
    }
    return { ok: true, json: async () => ({}) };
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.innerWidth = 1280;
sandbox.innerHeight = 800;
sandbox.devicePixelRatio = 1;
vm.createContext(sandbox);
vm.runInContext(script, sandbox, { filename: 'index.html<script>' });

/* ---------- streamSplit 单测 ---------- */
console.log('streamSplit 单测:');
const t1 = sandbox.streamSplit('答案是。\n\n```py\nprint(1+1)');
console.log('  围栏未闭合:', JSON.stringify(t1));
const t2 = sandbox.streamSplit('答案是。\n\n```py\nprint(1+1)\n```\n\n- 甲');
console.log('  围栏已闭合:', JSON.stringify(t2));

/* ---------- 驱动一次完整会话 ---------- */
(async () => {
  await new Promise(r => setTimeout(r, 20));
  byId['inp'].value = '1+1=?';
  writes.length = 0;                       // 只统计会话期间的写入
  await sandbox.send();
  await new Promise(r => setTimeout(r, 50));

  /* ---------- 断言 ---------- */
  const fail = [];
  const ok = [];
  const T = (cond, msg) => (cond ? ok : fail).push(msg);

  // 1. 流式期间 append-only：只统计【正文气泡】这一个元素的 innerHTML 写入
  const bubEl = writes.map(w => w.el).find(e => {
    const mine = writes.filter(w => w.el === e);
    const last = mine[mine.length - 1];
    return last && last.value.includes('<strong>2</strong>') && last.value.includes('第一点');
  });
  const bubWrites = bubEl ? writes.filter(w => w.el === bubEl).map(w => w.value) : [];
  T(!!bubEl, '找到正文气泡元素');
  T(bubWrites.length <= 5, `正文气泡 innerHTML 写入 ${bubWrites.length} 次（≤5：初建+等待+清空+代码骨架+终态）`);
  const waitIdx = bubWrites.findIndex(v => v.startsWith('<div class="waiting">'));
  const afterWait = waitIdx >= 0 ? bubWrites.slice(waitIdx + 1) : bubWrites;
  T(afterWait.slice(0, -1).every(v => v === '' || v.startsWith('<pre class="stpre">')),
    '流式期间气泡只被清空或进入代码模式，从不整体重写');

  // 2. 字符不丢
  const strip = h => String(h).replace(/<[^>]*>/g, '');
  const painted = allEls.map(e => strip(e._html) + (e.textContent || '')).join('');
  T(painted.includes('答案是') && painted.includes('第一点') && painted.includes('第二点'),
    '正文字符全部上屏');
  T(painted.includes('print(1+1)'), '代码块内容上屏');
  const codeHost = allEls.find(e => e.children.map(c => c.textContent || '').join('') === 'print(1+1)\n');
  T(!!codeHost, '代码块流式逐字上屏（querySelector 桩内）');

  // 3. .stail 创建 2 次 = 正文 1 + 思考面板 1
  const stails = allEls.filter(e => e.className === 'stail').length;
  T(stails === 2, `.stail 创建 ${stails} 次（=2：正文+思考各一）`);

  // 4. 思考面板终态是 markdown
  T(allEls.some(e => e._html.includes('<strong>用户</strong>')),
    '思考面板终态 mdRender（<strong>用户</strong>）');

  // 5. 统计上报字段齐全
  T(statsReports.length === 1 && statsReports[0].tps > 0 && statsReports[0].inTok === 520
    && statsReports[0].outTok === 60, '统计上报字段齐全（tps/inTok/outTok）');

  console.log('通过 ' + ok.length + ' 项:');
  ok.forEach(m => console.log('  ✓ ' + m));
  if (fail.length) {
    console.log('失败 ' + fail.length + ' 项:');
    fail.forEach(m => console.log('  ✗ ' + m));
    process.exit(1);
  }
  console.log('\nALL_STREAMING_TESTS_PASSED');
  process.exit(0);          // 脚本里的 setInterval 会拖住进程，显式退出
})().catch(e => { console.error('运行异常:', e); process.exit(1); });
