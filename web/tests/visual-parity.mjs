// Read-only reference server + production-preview comparison. No reference is shipped.
// Usage: node tests/visual-parity.mjs <reference-directory> <runtime-node-modules> [preview-url]
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import { createRequire } from 'node:module';
import { preview as startPreview } from 'vite';
const [referenceDir, modules, previewArg] = process.argv.slice(2);
const preview = previewArg || 'http://127.0.0.1:4175';
const previewServer = previewArg ? null : await startPreview({ preview: { host: '127.0.0.1', port: 4175, strictPort: true } });
if (!referenceDir || !modules) throw new Error('Reference directory and test runtime modules are required');
const require = createRequire(path.join(modules, '_test.cjs'));
const { chromium } = require('playwright'), { PNG } = require('pngjs');
const out = await fs.mkdtemp(path.join(os.tmpdir(), 'wtb-visual-parity-'));
const files = ['01-path-loom-cyber.html', '02-branch-atlas-cyber.html', '03-execution-section-cyber.html', '04-variant-atlas-cyber.html'];
const server = http.createServer(async (req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).slice(1);
  if (![...files, 'WTB_Cyber_Black.png'].includes(name)) { res.writeHead(404).end(); return; }
  try { res.setHeader('Content-Type', name.endsWith('.png') ? 'image/png' : 'text/html; charset=utf-8'); res.end(await fs.readFile(path.join(referenceDir, name))); } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const errors = [], results = [], audits = [], failedRequests = [];
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1, reducedMotion: 'reduce' });
const ref = await context.newPage(), app = await context.newPage();
app.on('pageerror', error => errors.push(error.message));
app.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
app.on('response', response => { if (response.status() >= 400) failedRequests.push({ url: response.url(), status: response.status() }); });
await app.goto(preview);
await app.evaluate(() => localStorage.removeItem('wtb-react-structural-studio-v1'));
await app.reload();
async function settle(page) { await page.evaluate(async () => { await document.fonts.ready; await Promise.all([...document.images].map(img => img.decode().catch(() => {}))); }); }
function pixels(a, b) {
  a = PNG.sync.read(a); b = PNG.sync.read(b);
  if (a.width !== b.width || a.height !== b.height) return { size: [a.width, a.height, b.width, b.height], changed: -1 };
  const diff = new PNG({ width: a.width, height: a.height }); let changed = 0, maxDelta = 0;
  for (let i = 0; i < a.data.length; i += 4) {
    const d = Math.max(...[0, 1, 2].map(c => Math.abs(a.data[i + c] - b.data[i + c])));
    if (d) changed++; maxDelta = Math.max(maxDelta, d);
    diff.data[i] = d ? 255 : a.data[i] * .25; diff.data[i + 1] = d ? 0 : a.data[i + 1] * .25; diff.data[i + 2] = d ? 120 : a.data[i + 2] * .25; diff.data[i + 3] = 255;
  }
  return { changed, maxDelta, size: [a.width, a.height], diff: PNG.sync.write(diff) };
}
async function geometry(page) {
  return page.evaluate(() => ({
    layout: [...document.querySelectorAll('.masthead,.study-tabs,.hero,.work-surface,.view-toolbar,.stage,.view-footnote,.context-rail,.execution-dock,.bottom-line')].map(el => { const r = el.getBoundingClientRect(); return [el.className, r.x, r.y, r.width, r.height]; }),
    svg: [...document.querySelectorAll('#stage svg *')].filter(el => el.tagName !== 'title').map(el => ({ tag: el.tagName, text: el.tagName === 'text' ? el.textContent : undefined, attrs: Object.fromEntries([...el.attributes].filter(a => !a.name.startsWith('data-') && !['role', 'tabindex', 'aria-label'].includes(a.name)).map(a => [a.name, a.name === 'style' ? a.value.replace(/\s/g, '').replace(/;$/, '') : /^-?(?:\d+\.?\d*|\.\d+)$/.test(a.value) ? String(Number(a.value)) : a.value]).sort()) })),
    image: document.querySelector('.brand-logo').naturalWidth,
  }));
}
async function compare(label) {
  await settle(ref); await settle(app);
  const [a, b, ga, gb] = await Promise.all([ref.screenshot({ fullPage: true }), app.screenshot({ fullPage: true }), geometry(ref), geometry(app)]);
  await fs.writeFile(path.join(out, `${label}-reference.png`), a); await fs.writeFile(path.join(out, `${label}-react.png`), b);
  const { diff, ...pixelResult } = pixels(a, b); if (diff) await fs.writeFile(path.join(out, `${label}-diff.png`), diff);
  const differences = [];
  for (const key of ['layout', 'svg']) if (JSON.stringify(ga[key]) !== JSON.stringify(gb[key])) differences.push(key);
  if (differences.length) await fs.writeFile(path.join(out, `${label}-geometry.json`), JSON.stringify({ reference: ga, react: gb }, null, 2));
  const [sa, sb] = await Promise.all([ref.evaluate(() => window.WTBStudio.snapshot()), app.evaluate(() => JSON.parse(localStorage.getItem('wtb-react-structural-studio-v1')))]);
  const stateEqual = JSON.stringify(sa) === JSON.stringify(sb);
  if (!stateEqual) await fs.writeFile(path.join(out, `${label}-state.json`), JSON.stringify({ reference: sa, react: sb }, null, 2));
  results.push({ label, ...pixelResult, differences, stateEqual, svgElements: [ga.svg.length, gb.svg.length], imageLoaded: gb.image > 0 });
  console.log(JSON.stringify(results.at(-1)));
}
try {
  for (const width of (process.env.AUDIT_ONLY ? [] : process.env.QUICK ? [1440] : [1440, 899, 901, 1199, 1201, 1699, 1701])) {
    await ref.setViewportSize({ width, height: 1000 }); await app.setViewportSize({ width, height: 1000 });
    for (let i = 0; i < files.length; i++) {
      await ref.goto(`${origin}/${files[i]}`); await ref.evaluate(() => { localStorage.clear(); }); await ref.reload();
      await app.locator('#reset-btn').click(); await app.locator('.study-tab').nth(i).click();
      // Toast is not an initial-state element. Reset through a reload to clear transient UI.
      await app.reload();
      await compare(`${width}-${i + 1}-initial`);
    }
  }
  await ref.setViewportSize({ width: 1440, height: 1000 }); await app.setViewportSize({ width: 1440, height: 1000 });
  async function reset(view) {
    await ref.goto(`${origin}/${files[view]}`); await ref.evaluate(() => localStorage.clear()); await ref.reload();
    await app.locator('#reset-btn').click(); await app.locator('.study-tab').nth(view).click(); await app.reload();
  }
  async function both(fn) { await fn(ref); await fn(app); }
  async function stableCompare(name) {
    // Toast lifetimes are transient; inspect the settled state, without masking pixels.
    await Promise.all([ref.locator('#toast.visible').waitFor({ state: 'hidden' }), app.locator('#toast.visible').waitFor({ state: 'hidden' })]);
    await both(p => p.mouse.move(0, 0)); await compare(name);
  }
  if (!process.env.AUDIT_ONLY) {
  await reset(0);
  await both(p => p.getByRole('button', { name: '聚焦当前', exact: true }).click()); await stableCompare('loom-focus');
  await both(p => p.getByRole('button', { name: 'workflow10 · 上下文旁路 · 已完成', exact: true }).locator('text').nth(1).click()); await stableCompare('loom-bypass');
  await both(p => p.locator('#context-rail > button').click()); await stableCompare('loom-drawer');
  await both(p => p.getByRole('button', { name: '选择节点 D', exact: true }).click()); await stableCompare('drawer-unused-node');
  await both(p => p.getByRole('button', { name: '关闭执行上下文' }).click());
  await both(p => p.locator('#guide-btn').click()); await stableCompare('guide-modal');
  await both(p => p.locator('#close-guide').click());
  await reset(1);
  await both(p => p.getByRole('button', { name: '暂停', exact: true }).click());
  await both(p => p.getByRole('button', { name: '继续', exact: true }).waitFor()); await stableCompare('lineage-paused');
  await both(p => p.getByRole('button', { name: '选择检查点 cp-102-01', exact: true }).click());
  await both(p => p.getByRole('button', { name: '回退', exact: true }).click()); await stableCompare('rollback-modal');
  await both(p => p.getByRole('button', { name: '确认回退', exact: true }).click()); await stableCompare('lineage-rollback');
  await both(p => p.getByRole('button', { name: 'Fork', exact: true }).click()); await stableCompare('fork-modal');
  await both(p => p.getByRole('button', { name: '创建分支', exact: true }).click()); await stableCompare('lineage-fork');
  await both(p => p.getByRole('button', { name: '推进一步', exact: true }).click()); await stableCompare('lineage-fork-step');
  await both(p => p.locator('#run-select').selectOption('run-112')); await stableCompare('lineage-failed-run');
  await reset(2);
  await both(p => p.getByRole('button', { name: 'A 执行资源 · 未分配 Actor', exact: true }).click()); await stableCompare('section-inherited-drawer');
  await both(p => p.getByRole('button', { name: '关闭执行上下文' }).click());
  await both(p => p.locator('#run-select').selectOption('run-112')); await stableCompare('section-failed');
  await reset(3);
  await both(p => p.locator('#x-dim').selectOption('4')); await stableCompare('atlas-swapped-axes');
  await both(p => p.locator('#y-dim').selectOption('1')); await stableCompare('atlas-dynamic-rows');
  await both(p => p.locator('#baseline-select').selectOption('w07')); await stableCompare('atlas-baseline');
  await both(p => p.getByRole('button', { name: /^workflow04 · 直接生成/ }).click()); await stableCompare('atlas-unused');
  }
  await reset(0);
  await both(p => p.locator('#stage g[role="button"]').first().focus());
  await both(p => p.keyboard.press('2'));
  audits.push({ name: 'keyboard-view-switch-with-svg-focus', reference: await ref.evaluate(() => window.WTBStudio.snapshot().view), react: await app.evaluate(() => JSON.parse(localStorage.getItem('wtb-react-structural-studio-v1')).view) });
  await reset(1);
  await both(p => p.locator('#auto-check').check());
  await both(p => p.getByRole('button', { name: 'Fork', exact: true }).click());
  const modalBefore = await Promise.all([ref.locator('#action-dialog b').textContent(), app.locator('#action-dialog b').textContent()]);
  await ref.waitForFunction(() => window.WTBStudio.snapshot().selectedCp !== 'cp-102-02');
  await app.waitForFunction(() => JSON.parse(localStorage.getItem('wtb-react-structural-studio-v1')).selectedCp !== 'cp-102-02');
  const modalAfter = await Promise.all([ref.locator('#action-dialog b').textContent(), app.locator('#action-dialog b').textContent()]);
  audits.push({ name: 'fork-checkpoint-stable-while-auto-running', before: modalBefore, after: modalAfter });
  console.log(JSON.stringify({ audits, failedRequests }));
  await fs.writeFile(path.join(out, 'results.json'), JSON.stringify({ results, errors, audits, failedRequests }, null, 2));
  console.log(JSON.stringify({ outputDirectory: out, errors }));
} finally { await browser.close(); server.close(); previewServer?.httpServer.close(); }
