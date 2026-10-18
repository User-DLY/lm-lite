// -*- coding: utf-8 -*-
/*
 * lm-lite 设置项持久化回归测试（Node + DOM 桩，无需浏览器）
 * ------------------------------------------------------------------
 * 用法：node kv_stick_test.js        （在 lm-lite 目录下）
 *
 * 背景（用户实报 bug）：KV 精度选了 f16，三秒后被轮询改回 q8_0。
 * 根因：refresh() 每 3 秒把后端现状无条件写回 CFG.kv/CFG.ctx，
 * 用户"待加载的选择"被当前已加载模型的状态覆盖。
 * 修法：用户手动选过之后轮询不再覆盖（_ctxTouched/_kvTouched），
 * 只有加载成功才把偏好同步成实际值；设置页并显示"后端现状 vs 你的选择"。
 *
 * 本测试断言：
 *   1. 用户没选过时，轮询仍然镜像后端状态（新开页面/换机器的场景）
 *   2. 用户选过 f16 后，轮询【不能】改回 q8_0（bug 本体）
 *   3. 提示行明确告知"后端是 q8_0、你的选择是 f16、要重新加载"
 *   4. 上下文选择同理不被覆盖
 *   5. 加载成功后，偏好同步为实际值且解除"选过"标记（之后轮询不折腾）
 * 改 refresh()/applyCfg()/ctxSel/kvSel 接线后，请先跑它。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i)[1];

/* ---------- DOM 桩（带事件监听捕获，能真的 fire change） ---------- */
let elSeq = 0;
function makeEl(tag) {
  const el = {
    _id: ++elSeq, tagName: tag, children: [], _html: '', textContent: '',
    value: '', checked: false, disabled: false, title: '', placeholder: '',
    scrollTop: 0, scrollHeight: 100, clientHeight: 50,
    dataset: {}, options: [], _ls: {},
    style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  Object.defineProperty(el, 'className', {
    get() { return this._cls || ''; },
    set(v) { this._cls = String(v); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); this.children = []; },
  });
  el.appendChild = n => { el.children.push(n); return n; };
  el.insertBefore = n => { el.children.unshift(n); return n; };
  el.removeChild = n => { el.children = el.children.filter(c => c !== n); return n; };
  el.insertAdjacentHTML = (pos, h) => { el._html += String(h); };
  el.querySelector = () => makeEl('div');
  el.querySelectorAll = () => [];
  el.addEventListener = (t, fn) => { el._ls[t] = fn; };
  el.removeEventListener = () => {};
  el.remove = () => {};
  el.focus = () => {};
  el.closest = () => null;
  allEls.push(el);
  return el;
}
const allEls = [];
const byId = {};
const documentStub = {
  getElementById: id => byId[id] || (byId[id] = makeEl('div#' + id)),
  createElement: tag => makeEl(tag),
  createTextNode: t => { const e = makeEl('#text'); e.textContent = t; return e; },
  createDocumentFragment: () => makeEl('#fragment'),
  addEventListener: () => {},
  querySelector: () => makeEl('div'),
  querySelectorAll: () => [],
  documentElement: makeEl('html'),
  body: makeEl('body'),
  hidden: false,
};

/* ---------- 后端状态：可变，模拟"当前加载的是 q8_0 / 100K" ---------- */
const BACKEND = { alive: true, model: 'C:\\m\\Spark-X2.5-1.7B-Q4_K_M.gguf',
                  ctx: 100096, kv: 'q8_0', loading: false, error: '' };
let loadResp = { ok: true, ctx: 100096, kv: 'q8_0' };   // /api/load 的响应（加载完成后改）

const sandbox = {
  console,
  document: documentStub,
  localStorage: (() => { const m = {}; return {
    getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); },
    removeItem: k => { delete m[k]; } }; })(),
  navigator: {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: cb => { cb(); return 1; },
  cancelAnimationFrame: () => {},
  performance, TextDecoder, AbortController, Blob: function(){},
  URL: { createObjectURL: () => '', revokeObjectURL() {} },
  setInterval, clearInterval, setTimeout, clearTimeout,
  fetch: async (url, opts) => {
    if (url === '/api/status')
      return { ok: true, json: async () => (Object.assign({}, BACKEND)) };
    if (url === '/api/load') {
      const body = JSON.parse(opts.body || '{}');
      // 模拟真实后端：加载成功后，后端状态变成请求里的值
      BACKEND.ctx = body.ctx; BACKEND.kv = body.kv;
      return { ok: true, json: async () => (Object.assign({}, loadResp)) };
    }
    if (url === '/api/models')
      return { ok: true, json: async () => ({ models: [], current: '' }) };
    if (url === '/api/model-caps')
      return { ok: true, json: async () => ({ loaded: true, supportsThinking: true, why: 'x' }) };
    if (url === '/api/prompts')
      return { ok: true, json: async () => ({ lines: ['问题甲'] }) };
    if (url === '/api/vram')
      return { ok: true, json: async () => ({ loaded: true, gpuTotalMB: 8192, gpuUsedMB: 4000, modelRealMB: 2000, otherMB: 2000 }) };
    if (url === '/api/stats')
      return { ok: true, json: async () => ({ avgTtft: 0, avgTps: 0, requests: 0, last: {} }) };
    if (url === '/api/api-cfg')
      return { ok: true, json: async () => ({ ok: true, cfg: { enabled: true } }) };
    return { ok: true, json: async () => ({}) };
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.innerWidth = 1280; sandbox.innerHeight = 800; sandbox.devicePixelRatio = 1;

/* 预置两个下拉框（带选项），其余元素惰性创建 */
byId['kvSel'] = makeEl('select#kvSel');
byId['kvSel'].options = [{ value: 'f16' }, { value: 'q8_0' }, { value: 'q5_1' }];
byId['ctxSel'] = makeEl('select#ctxSel');
byId['ctxSel'].options = [{ value: '4096' }, { value: '8192' }, { value: '16384' },
                          { value: '32768' }, { value: '65536' }, { value: '100096' }];

vm.createContext(sandbox);
vm.runInContext(script, sandbox, { filename: 'index.html<script>' });

const get = expr => vm.runInContext(expr, sandbox);
const set = (stmt) => vm.runInContext(stmt, sandbox);
const fire = (el, type) => { if (el._ls && el._ls[type]) el._ls[type]({ target: el }); };
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const fail = [], ok = [];
  const T = (cond, msg) => (cond ? ok : fail).push(msg);

  await wait(60);                       // 等启动时的 refresh() 落定

  /* 1. 没选过 → 镜像后端（q8_0 / 100096） */
  T(get('CFG.kv') === 'q8_0', '未选过时轮询镜像后端 kv=q8_0');
  T(byId['kvSel'].value === 'q8_0', '下拉框显示 q8_0');
  T(get('CFG.ctx') === '100096', '未选过时镜像后端 ctx=100096');

  /* 2. 用户选 f16 → 轮询不许改回 ★ bug 本体 */
  byId['kvSel'].value = 'f16';
  fire(byId['kvSel'], 'change');
  await wait(10);
  T(get('CFG.kv') === 'f16', '选 f16 后 CFG.kv=f16');
  await sandbox.refresh();              // 轮询：后端仍是 q8_0
  await wait(10);
  T(get('CFG.kv') === 'f16', '★ 轮询后 CFG.kv 仍是 f16（旧版这里被改回 q8_0）');
  T(byId['kvSel'].value === 'f16', '★ 轮询后下拉框仍是 f16');

  /* 3. 提示行说清楚"后端 vs 你的选择" */
  const hint = byId['kvNow'] ? byId['kvNow']._html : '';
  T(hint.includes('q8_0') && hint.includes('f16') && hint.includes('加载模型'),
    '提示行：后端 q8_0 · 你的选择 f16 · 重新加载后生效');

  /* 4. 上下文同样不被覆盖 */
  byId['ctxSel'].value = '32768';
  fire(byId['ctxSel'], 'change');
  await wait(10);
  await sandbox.refresh();
  await wait(10);
  T(get('CFG.ctx') === '32768', '★ 选 32K 后轮询不改回 100096');
  T(byId['ctxSel'].value === '32768', '★ 上下文下拉框保持 32K');

  /* 5. 走完整加载流程：选 f16 → 点加载 → 偏好同步成实际值、解除选过标记 */
  set("models = [{path:'C:\\\\m\\\\X.gguf', id:'X', sizeGB:1.0}]");
  byId['modelSel'].value = '0';
  byId['kvSel'].value = 'f16';
  fire(byId['kvSel'], 'change');
  byId['ctxSel'].value = '32768';
  fire(byId['ctxSel'], 'change');
  loadResp = { ok: true, ctx: 32768, kv: 'f16' };
  await sandbox.doLoad();
  await wait(20);
  T(get('CFG.kv') === 'f16' && get('CFG.ctx') === '32768',
    '加载成功后偏好=实际值（f16/32K）');
  T(get('_kvTouched') === false && get('_ctxTouched') === false,
    '加载成功后解除"选过"标记（之后轮询正常镜像）');
  await sandbox.refresh();
  await wait(10);
  T(get('CFG.kv') === 'f16', '加载后轮询不折腾（后端已是 f16）');

  console.log('通过 ' + ok.length + ' 项:');
  ok.forEach(m => console.log('  ✓ ' + m));
  if (fail.length) {
    console.log('失败 ' + fail.length + ' 项:');
    fail.forEach(m => console.log('  ✗ ' + m));
    process.exit(1);
  }
  console.log('\nALL_KV_STICK_TESTS_PASSED');
  process.exit(0);
})().catch(e => { console.error('运行异常:', e); process.exit(1); });
