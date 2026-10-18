// -*- coding: utf-8 -*-
/*
 * lm-lite 图表渲染回归测试（Node + canvas 桩，无需浏览器）
 * ------------------------------------------------------------------
 * 用法：node chart_test.js        （在 lm-lite 目录下）
 *
 * 背景：2026-10-17 给 _paintLine 加了 Y 轴上限缓动（新点接入时刻度不跳变，
 * 消除"割裂感"）。canvas 代码一个运行时错误就会让所有图表全死，而维护者
 * 不一定有浏览器环境，故用桩上下文单测：
 *   1. 有数据/无数据/单点渲染均不抛异常
 *   2. Y 轴上限逐帧逼近目标（不瞬间跳变），最终收敛
 *   3. 悬停路径（竖线/圆点/气泡 + 动画推进）不抛异常
 *   4. 零尺寸 canvas（页面隐藏时）安全返回
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i)[1];

function elStub() {
  const el = {
    style: { setProperty() {} }, dataset: {}, value: '', checked: false,
    textContent: '', innerHTML: '', className: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild() {}, insertBefore() {}, removeChild() {}, remove() {},
    insertAdjacentHTML() {}, addEventListener() {}, querySelector: () => elStub(),
    querySelectorAll: () => [], focus() {}, getBoundingClientRect: () => ({ left: 0, top: 0 }),
    children: [], options: [],
  };
  return el;
}
const sandbox = {
  console, window: {},
  document: { documentElement: elStub(),
              addEventListener: () => {}, querySelector: () => elStub(),
              querySelectorAll: () => [], getElementById: () => elStub(),
              createElement: () => elStub(), createTextNode: () => elStub(),
              createDocumentFragment: () => elStub(), body: elStub(), hidden: false },
  localStorage: { getItem: () => null, setItem: () => {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  devicePixelRatio: 1,
  requestAnimationFrame: () => 1,
  setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  navigator: {}, performance: { now: () => 0 },
  TextDecoder, TextEncoder, AbortController: function () {},
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
sandbox.window = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.innerWidth = 1280; sandbox.innerHeight = 800;
vm.createContext(sandbox);
vm.runInContext(script, sandbox, { filename: 'index.html<script>' });

function ctxStub() {
  const grad = { addColorStop() {} };
  return new Proxy({
    createLinearGradient: () => grad,
    measureText: () => ({ width: 40 }),
    roundRect() {},
  }, {
    get(t, k) { if (k in t) return t[k]; return () => {}; },
    set(t, k, v) { t[k] = v; return true; },
  });
}
function cvStub(w, h) {
  return {
    clientWidth: w, clientHeight: h, width: 0, height: 0,
    _data: [], _hoverX: null,
    getContext: () => ctxStub(),
    style: {}, addEventListener() {},
  };
}

const paint = sandbox._paintLine;
if (typeof paint !== 'function') { console.error('取不到 _paintLine'); process.exit(1); }

const fail = [];
const T = (c, m) => { if (!c) fail.push(m); else console.log('  ✓ ' + m); };

const cv1 = cvStub(600, 160);
cv1._data = [100, 200, 150, 300]; cv1._color = '#818cf8'; cv1._unit = 'tok/s';
try { paint(cv1); T(true, '有数据渲染不抛异常'); } catch (e) { T(false, '有数据渲染抛异常: ' + e.message); }
const cv2 = cvStub(600, 160); cv2._data = [];
try { paint(cv2); T(true, '无数据渲染不抛异常'); } catch (e) { T(false, '无数据渲染抛异常: ' + e.message); }
const cv3 = cvStub(600, 160); cv3._data = [95.2];
try { paint(cv3); T(true, '单点渲染不抛异常'); } catch (e) { T(false, '单点渲染抛异常: ' + e.message); }

const cv4 = cvStub(600, 160);
cv4._data = [100, 100]; cv4._color = '#34d399'; cv4._unit = 'tok/s';
paint(cv4);
const first = cv4._showMax;
cv4._data = [100, 1000];
paint(cv4);
const mid = cv4._showMax;
T(mid > first && mid < 1000 * 1.15, `Y 轴上限缓动中（${first.toFixed(0)} → ${mid.toFixed(0)}，目标 ${(1000 * 1.15).toFixed(0)}）`);
for (let i = 0; i < 60; i++) paint(cv4);
T(Math.abs(cv4._showMax - 1150) < 1, `缓动收敛到目标（${cv4._showMax.toFixed(1)}）`);

const cv5 = cvStub(600, 160);
cv5._data = [10, 20, 30, 40]; cv5._color = '#fbbf24'; cv5._unit = 'ms';
try {
  cv5._hoverX = 300;
  for (let i = 0; i < 40; i++) paint(cv5);
  T(true, '悬停渲染 + 动画推进不抛异常');
} catch (e) { T(false, '悬停渲染抛异常: ' + e.message); }

const cv6 = cvStub(0, 0); cv6._data = [1, 2];
try { paint(cv6); T(true, '零尺寸 canvas 安全返回'); } catch (e) { T(false, '零尺寸抛异常: ' + e.message); }

if (fail.length) { console.log('失败:'); fail.forEach(m => console.log('  ✗ ' + m)); process.exit(1); }
console.log('\nPAINTLINE_TESTS_PASSED');
process.exit(0);
