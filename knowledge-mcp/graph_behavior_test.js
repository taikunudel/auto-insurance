// Behavior regression suite for the graph.html reading card.
// Usage:  node graph_behavior_test.js /path/to/graph.html
// Needs:  npm i playwright  (uses the installed Google Chrome via channel:'chrome')
// The suite copies the target to <name>.test.html with a window._kg instrumentation
// hook (the page itself is never modified) and deletes the copy afterwards.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const SRC = process.argv[2];
if (!SRC || !fs.existsSync(SRC)) { console.error('usage: node graph_behavior_test.js /path/to/graph.html'); process.exit(2); }
const TEST_COPY = SRC.replace(/\.html$/, '') + '.test.html';
fs.writeFileSync(TEST_COPY, fs.readFileSync(SRC, 'utf8').replace('new KG().run()', '(window._kg = new KG()).run()'));
const TARGET_URL = 'file://' + path.resolve(TEST_COPY);
const HEADLESS = process.env.HEADFUL ? false : true;

const results = [];
const t = (name, fn) => ({ name, fn });

(async () => {
  const browser = await chromium.launch({ headless: HEADLESS, channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });

  const consoleErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));

  const kg = expr => page.evaluate(`(() => { const k = window._kg; return ${expr}; })()`);
  const drawerOpen = async () =>
    (await page.$eval('#kg-drawer', el => getComputedStyle(el).transform)) === 'matrix(1, 0, 0, 1, 0, 0)';
  const openNode = async (pred = 'true') => {   // select a node via the app API (deterministic), then verify DOM
    await page.evaluate(`(() => { const k = window._kg; const n = k.nodes.find(n => ${pred}) || k.nodes[0]; k.select(n.id); })()`);
    await page.waitForTimeout(450);
  };
  const closeIfOpen = async () => { if (await drawerOpen()) { await page.click('#kg-d-close'); await page.waitForTimeout(450); } };

  await page.goto(TARGET_URL);
  await page.waitForSelector('.kg-node', { timeout: 10000 });
  await page.waitForTimeout(1500); // fade-in

  const tests = [
    t('T1 click a graph node opens the card', async () => {
      const node = await page.evaluateHandle(() => {
        const els = [...document.querySelectorAll('.kg-node')];
        return els.find(el => { const r = el.getBoundingClientRect(); return r.left > 420 && r.right < innerWidth - 80 && r.top > 80 && r.bottom < innerHeight - 80; });
      });
      await node.asElement().click({ force: true }); // nodes drift by design; skip stability wait
      await page.waitForTimeout(500);
      if (!(await drawerOpen())) throw new Error('drawer did not open after node click');
      const title = await page.$eval('#kg-d-title', el => el.textContent);
      if (!title.trim()) throw new Error('empty title');
    }),

    t('T2 click on empty area INSIDE the card keeps it open', async () => {
      if (!(await drawerOpen())) await openNode();
      const r = await page.$eval('#kg-d-scroll', el => el.getBoundingClientRect().toJSON());
      await page.mouse.click(r.x + r.width * 0.5, r.y + 10); // top padding strip: no links there
      await page.waitForTimeout(450);
      if (!(await drawerOpen())) throw new Error('card closed on an in-card click');
    }),

    t('T3 text-selection drag inside the card keeps it open + does not pan graph', async () => {
      if (!(await drawerOpen())) await openNode();
      const [tx0, ty0] = [await kg('k.tx'), await kg('k.ty')];
      const r = await page.$eval('#kg-d-scroll', el => el.getBoundingClientRect().toJSON());
      await page.mouse.move(r.x + 60, r.y + 60);
      await page.mouse.down();
      await page.mouse.move(r.x + 260, r.y + 64, { steps: 8 });
      await page.mouse.up();
      await page.waitForTimeout(450);
      if (!(await drawerOpen())) throw new Error('card closed after drag-select inside card');
      if (await kg('k.selected') === null) throw new Error('selection cleared by in-card drag');
      if (await kg('k.tx') !== tx0 || await kg('k.ty') !== ty0) throw new Error('graph panned during in-card drag');
    }),

    t('T4 wheel over the card scrolls the body, not the graph zoom', async () => {
      if (!(await drawerOpen())) await openNode();
      await openNode('(n.markdown||"").length > 1500'); // long page so the body can scroll
      const scale0 = await kg('k.scale');
      const r = await page.$eval('#kg-d-scroll', el => el.getBoundingClientRect().toJSON());
      await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
      await page.mouse.wheel(0, 400);
      await page.waitForTimeout(250);
      const scrolled = await page.$eval('#kg-d-scroll', el => el.scrollTop);
      if (scrolled === 0) throw new Error('card body did not scroll on wheel');
      if (await kg('k.scale') !== scale0) throw new Error('graph zoomed while wheeling over the card');
    }),

    t('T5 wheel over the stage (card closed) zooms the graph', async () => {
      await closeIfOpen();
      const scale0 = await kg('k.scale');
      await page.mouse.move(800, 500);
      await page.mouse.wheel(0, -300);
      await page.waitForTimeout(250);
      if (await kg('k.scale') === scale0) throw new Error('zoom did not change');
    }),

    t('T6 drag on empty stage pans without opening anything', async () => {
      await closeIfOpen();
      const tx0 = await kg('k.tx');
      const pt = await page.evaluate(() => {   // nodes drift: find a press point clear of every node
        const rects = [...document.querySelectorAll('.kg-node')].map(el => el.getBoundingClientRect());
        for (let y = innerHeight - 60; y > 400; y -= 37)
          for (let x = 420; x < innerWidth - 80; x += 53)
            if (!rects.some(r => x > r.left - 30 && x < r.right + 30 && y > r.top - 30 && y < r.bottom + 30)) return { x, y };
        return { x: innerWidth - 80, y: innerHeight - 50 };
      });
      await page.mouse.move(pt.x, pt.y);
      await page.mouse.down();
      await page.mouse.move(pt.x + 150, pt.y - 40, { steps: 6 });
      await page.mouse.up();
      await page.waitForTimeout(300);
      if (await kg('k.tx') === tx0) throw new Error('pan did not move camera');
      if (await drawerOpen()) throw new Error('drawer opened from a pan');
      if (await kg('k.selected') !== null) throw new Error('selection set by a pan');
    }),

    t('T7 click on exposed stage margin closes an open card', async () => {
      await openNode();
      const m = await page.evaluate(() => {    // node-free x in the 26px strip above the card
        const rects = [...document.querySelectorAll('.kg-node')].map(el => el.getBoundingClientRect());
        for (let x = 420; x < innerWidth - 80; x += 41)
          if (!rects.some(r => r.top < 40 && x > r.left - 30 && x < r.right + 30)) return x;
        return 420;
      });
      await page.mouse.click(m, 12);
      await page.waitForTimeout(450);
      if (await drawerOpen()) throw new Error('card did not close on outside/stage click');
    }),

    t('T8 wikilink inside the body navigates to that page', async () => {
      await openNode('(n.markdown||"").includes("[[") && [...(n.markdown.matchAll(/\\[\\[([^\\]|]+)/g))].some(m => k.idx[k.norm(m[1])])');
      const before = await kg('k.selected');
      const link = await page.$('#kg-d-body .wikilink');
      if (!link) throw new Error('no rendered wikilink found on a page whose source has one');
      await link.click();
      await page.waitForTimeout(450);
      const after = await kg('k.selected');
      if (after === before || after === null) throw new Error('wikilink click did not switch pages');
      if (!(await drawerOpen())) throw new Error('card not open after wikilink nav');
      if (await page.$eval('#kg-d-scroll', el => el.scrollTop) !== 0) throw new Error('scroll not reset after nav');
    }),

    t('T9 related chip navigates', async () => {
      await openNode('k.adj[n.id] && k.adj[n.id].size > 0');
      const before = await kg('k.selected');
      const chip = await page.$('#kg-d-related .kg-chip');
      if (!chip) throw new Error('no related chips on a connected node');
      await chip.click();
      await page.waitForTimeout(450);
      if (await kg('k.selected') === before) throw new Error('chip click did not switch pages');
    }),

    t('T10 the X button closes the card and clears selection', async () => {
      if (!(await drawerOpen())) await openNode();
      await page.click('#kg-d-close');
      await page.waitForTimeout(450);
      if (await drawerOpen()) throw new Error('card still open after X');
      if (await kg('k.selected') !== null) throw new Error('selection not cleared');
    }),

    t('T11 left-panel page list opens/switches the card', async () => {
      const rows = await page.$$('#kg-nodelist [data-id]');
      if (rows.length < 2) throw new Error('page list rows not found');
      await rows[0].click(); await page.waitForTimeout(350);
      const t1 = await page.$eval('#kg-d-title', el => el.textContent);
      await rows[1].click(); await page.waitForTimeout(350);
      const t2 = await page.$eval('#kg-d-title', el => el.textContent);
      if (!(await drawerOpen())) throw new Error('card not open from page list');
      if (t1 === t2) throw new Error('card did not switch pages from list');
    }),

    t('T12 at 900px width the card must not cover the left panel', async () => {
      await page.setViewportSize({ width: 900, height: 800 });
      await page.waitForTimeout(300);
      if (!(await drawerOpen())) await openNode();
      const d = await page.$eval('#kg-drawer', el => el.getBoundingClientRect().toJSON());
      const p = await page.$eval('#kg-panel', el => el.getBoundingClientRect().toJSON());
      await page.setViewportSize({ width: 1500, height: 950 });
      await page.waitForTimeout(300);
      if (d.left < p.right) throw new Error(`card (left=${d.left}) overlaps panel (right=${p.right})`);
    }),

    t('T13 hidden card intercepts nothing and adds no horizontal scroll', async () => {
      await closeIfOpen();
      const hit = await page.evaluate(() => {
        const el = document.elementFromPoint(innerWidth - 40, innerHeight / 2);
        return el ? (el.closest('#kg-drawer') ? 'drawer' : 'other') : 'none';
      });
      if (hit === 'drawer') throw new Error('hidden card still intercepts pointer at right edge');
      const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (scroll > 1) throw new Error('horizontal overflow present: ' + scroll);
    }),

    t('T14 theme toggle restyles the open card', async () => {
      await openNode();
      const bg0 = await page.$eval('#kg-drawer', el => getComputedStyle(el).backgroundColor);
      await page.click('#kg-theme'); await page.waitForTimeout(250);
      const bg1 = await page.$eval('#kg-drawer', el => getComputedStyle(el).backgroundColor);
      await page.click('#kg-theme'); await page.waitForTimeout(250);
      if (bg0 === bg1) throw new Error('card background did not change with theme');
    }),

    t('T15 one scroll surface: title/frontmatter scroll away, X stays and closes', async () => {
      await openNode('n === k.nodes.reduce((a, b) => (a.markdown || "").length > (b.markdown || "").length ? a : b)');
      const top0 = await page.$eval('#kg-d-title', el => el.getBoundingClientRect().top);
      const moved = await page.evaluate(() => { const sc = document.getElementById('kg-d-scroll'); sc.scrollTop = 99999; return sc.scrollTop; });
      if (moved < 120) throw new Error('longest page leaves only ' + moved + 'px of scroll; cannot exercise the test');
      await page.waitForTimeout(200);
      const top1 = await page.$eval('#kg-d-title', el => el.getBoundingClientRect().top);
      if (!(top1 <= top0 - moved + 30)) throw new Error('title did not scroll with the content (still a split header)');
      const xOnTop = await page.evaluate(() => {
        const b = document.getElementById('kg-d-close').getBoundingClientRect();
        const el = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
        return !!(el && el.closest('#kg-d-close'));
      });
      if (!xOnTop) throw new Error('X not clickable after scrolling');
      await page.click('#kg-d-close');
      await page.waitForTimeout(450);
      if (await drawerOpen()) throw new Error('X did not close the scrolled card');
    }),

    t('T16 Net|Grid chooser: Grid fills the screen as a rectangular grid, labels never overlap, Net restores', async () => {
      await closeIfOpen();
      const snap = () => page.evaluate(() => window._kg.nodes.map(n => [n.bx, n.by]));
      const before = await snap();
      await page.click('#kg-view [data-v="grid"]'); await page.waitForTimeout(550);
      if (!await page.$eval('#kg-view [data-v="grid"]', el => el.classList.contains('on'))) throw new Error('Grid option not marked active');
      const g = await page.evaluate(() => {
        const k = window._kg, N = k.nodes, vis = N.filter(n => n.el.style.display !== 'none');
        const xs = vis.map(n => n.bx), ys = vis.map(n => n.by);
        const cols = new Set(xs.map(x => Math.round(x))).size;
        const sx = vis.map(n => n.bx * k.scale + k.tx), sy = vis.map(n => n.by * k.scale + k.ty);
        const gw = Math.max(...sx) - Math.min(...sx), gh = Math.max(...sy) - Math.min(...sy);
        const canvasW = innerWidth - 340 - 70, canvasH = innerHeight - 192;
        const L = vis.map(n => n.lab.getBoundingClientRect());
        const hit = (a, b) => !(a.right <= b.left + 1 || b.right <= a.left + 1 || a.bottom <= b.top + 1 || b.bottom <= a.top + 1);
        let ov = 0; for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) if (hit(L[i], L[j])) ov++;
        return { cols, fillW: gw / canvasW, fillH: gh / canvasH, labelOverlap: ov, allLabels: N.every(n => n.lab.classList.contains('vis')) };
      });
      if (g.cols < 3) throw new Error('not a multi-column grid (cols=' + g.cols + ')');
      if (g.fillW < 0.6 || g.fillH < 0.6) throw new Error('grid does not fill the screen: ' + g.fillW.toFixed(2) + 'x / ' + g.fillH.toFixed(2) + 'x of canvas');
      if (g.labelOverlap > 0) throw new Error(g.labelOverlap + ' overlapping label pair(s)');
      if (!g.allLabels) throw new Error('labels not all visible in grid mode');
      await page.click('#kg-view [data-v="net"]'); await page.waitForTimeout(550);
      if (!await page.$eval('#kg-view [data-v="net"]', el => el.classList.contains('on'))) throw new Error('Net option not marked active');
      const after = await snap(), drift = after.reduce((s, [x, y], i) => s + Math.hypot(x - before[i][0], y - before[i][1]), 0);
      if (drift > before.length * 3) throw new Error('Net did not restore positions: avg drift ' + (drift / before.length).toFixed(1) + 'px');
    }),

    t('T17 hovering a node fully hides non-neighbors and their labels (not just dims)', async () => {
      await closeIfOpen();
      const target = await page.evaluate(() => { const k = window._kg; return k.nodes.filter(n => !n.hero).sort((a, b) => b.deg - a.deg)[0].id; });
      await page.evaluate(t => window._kg.highlight(t), target);
      await page.waitForTimeout(650);
      const res = await page.evaluate(t => {
        const k = window._kg, on = new Set([t, ...k.adj[t]]);
        let bad = 0;
        k.nodes.forEach(n => { const op = +getComputedStyle(n.el).opacity, lop = +getComputedStyle(n.lab).opacity;
          if (on.has(n.id) ? (op < 0.99 || lop < 0.99) : (op > 0.02 || lop > 0.02)) bad++; });
        return { bad, neighbors: on.size };
      }, target);
      if (res.neighbors < 3) throw new Error('hover target has too few neighbors to exercise the test');
      if (res.bad > 0) throw new Error(res.bad + ' node/label(s) not fully hidden or shown on hover');
      await page.evaluate(() => window._kg.highlight(null));
      await page.waitForTimeout(650);
      const stuck = await page.evaluate(() => window._kg.nodes.filter(n => +getComputedStyle(n.el).opacity < 0.99).length);
      if (stuck > 0) throw new Error(stuck + ' node(s) stayed hidden after moving away');
    }),
  ];

  for (const { name, fn } of tests) {
    const errBefore = consoleErrors.length;
    try {
      await fn();
      const newErr = consoleErrors.slice(errBefore);
      if (newErr.length) throw new Error('console error(s): ' + newErr.join(' | '));
      results.push([name, 'PASS', '']);
      console.log('PASS  ' + name);
    } catch (e) {
      results.push([name, 'FAIL', e.message]);
      console.log('FAIL  ' + name + '  ->  ' + e.message);
    }
  }

  const fails = results.filter(r => r[1] === 'FAIL');
  console.log(`\n${results.length - fails.length}/${results.length} passed` + (fails.length ? `; ${fails.length} FAILED` : ''));
  await browser.close();
  try { fs.unlinkSync(TEST_COPY); } catch (e) {}
  process.exit(fails.length ? 1 : 0);
})();
