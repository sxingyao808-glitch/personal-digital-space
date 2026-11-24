'use strict';

/**
 * Black-box verification of the desktop shell.
 *
 * Launches the real Electron app with an isolated profile, waits for the
 * archive page to finish loading inside the window, asserts the rendered UI,
 * captures a screenshot through the DevTools protocol, then closes the window
 * and proves that (a) the app exits, (b) the backend it started exits, and
 * (c) no unrelated Python process was touched.
 *
 * Usage:
 *   node scripts/verify-shell.js --electron <electron.exe> --app <desktop dir>
 *        --data-dir <dir> --user-data-dir <dir> --shot <png>
 *        [--process-image python.exe] [--keep-open]
 *
 * Exit code 0 only when every check passes.
 */

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    switch (token) {
      case '--electron':
        args.electron = argv[++index];
        break;
      case '--app':
        args.app = argv[++index];
        break;
      case '--data-dir':
        args.dataDir = argv[++index];
        break;
      case '--user-data-dir':
        args.userDataDir = argv[++index];
        break;
      case '--shot':
        args.shot = argv[++index];
        break;
      case '--process-image':
        args.processImage = argv[++index];
        break;
      case '--keep-open':
        args.keepOpen = true;
        break;
      case '--debug-port':
        args.debugPort = Number(argv[++index]);
        break;
      case '--mock-pages-file':
        args.pagesFixture = argv[++index];
        break;
      case '--publish-cycle':
        args.publishCycle = true;
        break;
      case '--publish-reopen':
        args.publishReopen = true;
        break;
      case '--ui-cycle':
        args.uiCycle = true;
        break;
      case '--packaged':
        args.packaged = true;
        break;
      case '--no-env-override':
        // Let the app resolve its own data directory (config file / default).
        args.noEnvOverride = true;
        break;
      case '--expect-items':
        args.expectItems = Number(argv[++index]);
        break;
      default:
        throw new Error(`unknown argument: ${token}`);
    }
  }
  for (const required of ['electron', 'app', 'dataDir', 'userDataDir', 'shot']) {
    if (!args[required]) throw new Error(`missing --${required}`);
  }
  return args;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function fetchJson(url, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
      });
      response.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error(`invalid JSON from ${url}`));
        }
      });
    });
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.on('error', reject);
  });
}

function listPidsByImage(imageName) {
  const result = spawnSync('tasklist', ['/FI', `IMAGENAME eq ${imageName}`, '/FO', 'CSV', '/NH'], {
    windowsHide: true,
    encoding: 'utf8',
  });
  const pids = [];
  for (const line of (result.stdout || '').split(/\r?\n/)) {
    const match = line.match(/^"([^"]+)","(\d+)"/);
    if (match && match[1].toLowerCase() === imageName.toLowerCase()) {
      pids.push(Number(match[2]));
    }
  }
  return pids;
}

function findBackendPids(dataDir, imageName) {
  const script =
    `Get-CimInstance Win32_Process -Filter "Name='${imageName}'" | ` +
    `Where-Object { $_.CommandLine -and $_.CommandLine.Contains('${dataDir}') } | ` +
    `ForEach-Object { $_.ProcessId }`;
  const result = spawnSync(
    'powershell',
    ['-NoProfile', '-NonInteractive', '-Command', script],
    { windowsHide: true, encoding: 'utf8' }
  );
  return (result.stdout || '')
    .split(/\r?\n/)
    .map((line) => Number(line.trim()))
    .filter((value) => Number.isInteger(value) && value > 0);
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === 'EPERM';
  }
}

async function evaluate(client, expression) {
  const result = await client.send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(`page script error: ${result.exceptionDetails.text || 'unknown'}`);
  }
  return result.result.value;
}

/**
 * Create, edit, search and delete one throwaway entry through the real UI.
 * Used against a disposable copy of an archive, never against the only copy of
 * real data.
 */
async function runUiCycle(client, record, report) {
  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
  const title = `DESKTOP 验收条目 ${stamp}`;
  const body = '这条内容由桌面验收脚本创建，随后会被删除。';

  // HOME lists every space, so a new entry is visible wherever it lands.
  await evaluate(client, `(() => {
    const home = document.querySelector('#space-nav [data-space="HOME"]');
    if (home) home.click();
    return 'ok';
  })()`);
  await delay(600);

  const opened = await evaluate(client, `(() => {
    const emptyButton = document.getElementById('empty-new-button');
    const target = emptyButton && !emptyButton.closest('.hidden') ? emptyButton : document.getElementById('new-button');
    if (!target) return 'missing';
    target.click();
    return 'ok';
  })()`);
  record('cycle.editor_opened', opened === 'ok', opened);
  await delay(400);

  await evaluate(client, `(() => {
    const set = (id, value) => {
      const el = document.getElementById(id);
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    };
    set('title-input', ${JSON.stringify(title)});
    set('summary-input', '桌面端创建与编辑验证');
    set('body-input', ${JSON.stringify(body)});
    const priv = document.querySelector('.visibility-button[data-visibility="private"]');
    if (priv) priv.click();
    return 'ok';
  })()`);
  const filledTitle = await evaluate(client, `document.getElementById('title-input').value`);
  record('cycle.form_filled', filledTitle === title, filledTitle);

  await evaluate(client, `document.getElementById('save-button').click()`);
  let saved = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await delay(250);
    const status = await evaluate(client, `document.getElementById('save-status').textContent || ''`);
    if (status.includes('已保存')) {
      saved = true;
      break;
    }
  }
  record('cycle.created', saved);

  const detail = JSON.parse(
    await evaluate(client, `JSON.stringify({
      id: (document.getElementById('content-id').textContent || '').trim(),
      readerTitle: (document.getElementById('reader-title').textContent || '').trim(),
      state: (document.getElementById('content-state').textContent || '').trim(),
      visibility: document.querySelector('.visibility-button.active')
        ? document.querySelector('.visibility-button.active').dataset.visibility
        : ''
    })`)
  );
  record('cycle.detail_identity', detail.id.startsWith('c_'), detail.id);
  record('cycle.detail_title', detail.readerTitle === title, detail.readerTitle);
  record('cycle.visibility_private', detail.visibility === 'private', detail.visibility);
  report.createdContentId = detail.id;

  await evaluate(client, `document.getElementById('edit-button').click()`);
  await delay(300);
  await evaluate(client, `(() => {
    const el = document.getElementById('body-input');
    el.value = ${JSON.stringify(`${body} 已编辑。`)};
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  })()`);
  await evaluate(client, `document.getElementById('save-button').click()`);
  let edited = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await delay(250);
    const status = await evaluate(client, `document.getElementById('save-status').textContent || ''`);
    if (status.includes('已保存 · 版本 2')) {
      edited = true;
      break;
    }
  }
  record('cycle.edited', edited);

  await evaluate(client, `(() => {
    const el = document.getElementById('search-input');
    el.value = ${JSON.stringify(title)};
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  })()`);
  await delay(1000);
  const searchState = JSON.parse(
    await evaluate(client, `JSON.stringify({
      count: document.querySelectorAll('#content-list .content-item').length,
      firstTitle: (document.querySelector('#content-list .item-title') || {}).textContent || ''
    })`)
  );
  record(
    'cycle.search_filters',
    searchState.count === 1 && searchState.firstTitle === title,
    JSON.stringify(searchState)
  );

  await evaluate(client, `(() => {
    const item = document.querySelector('#content-list .content-item');
    if (item) item.click();
    return 'ok';
  })()`);
  await delay(800);
  const reopened = await evaluate(client, `(document.getElementById('reader-title').textContent || '').trim()`);
  record('cycle.detail_reopen', reopened === title, reopened);

  await evaluate(client, `document.getElementById('delete-button').click()`);
  await delay(400);
  const dialogOpen = await evaluate(client, `document.getElementById('confirm-dialog').open === true`);
  record('cycle.delete_confirm_shown', dialogOpen === true);

  await evaluate(client, `document.getElementById('confirm-ok').click()`);
  let deleted = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await delay(250);
    const titles = await evaluate(
      client,
      `JSON.stringify([...document.querySelectorAll('#content-list .item-title')].map((node) => node.textContent.trim()))`
    );
    if (!JSON.parse(titles).includes(title)) {
      deleted = true;
      break;
    }
  }
  record('cycle.deleted', deleted);

  await evaluate(client, `(() => {
    const el = document.getElementById('search-input');
    el.value = '';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  })()`);
  await delay(500);
}

async function waitForUi(client, expression) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await evaluate(client, expression)) return true;
    await delay(150);
  }
  return false;
}

async function publishCycle(client, record, report, reopen, pagesFixture) {
  const origin = await evaluate(client, 'location.origin');
  const openPage = async (route) => {
    await client.send('Page.navigate', { url: origin + route });
    return waitForUi(client, 'location.pathname === ' + JSON.stringify(route) + ' && document.readyState === "complete"');
  };
  const click = (selector) => evaluate(client, 'document.querySelector(' + JSON.stringify(selector) + ').click()');
  const textIncludes = (selector, text) => waitForUi(client,
    '(document.querySelector(' + JSON.stringify(selector) + ')?.textContent || "").includes(' + JSON.stringify(text) + ')'
  );
  const publishEnabled = () => waitForUi(client, 'document.getElementById("one-click-publish")?.disabled === false');
  const confirmPublish = async () => {
    await click('#one-click-publish');
    record('publish.confirm_dialog', await waitForUi(client, 'document.getElementById("release-confirm-dialog").open'));
    await click('#release-confirm-ok');
  };

  if (reopen) {
    record('publish.reopened_page', await openPage('/releases'));
    record('publish.reopened_withdrawn', await textIncludes('#release-current-chip', '已从本机撤回'));
    record('publish.reopened_history', await waitForUi(client, 'document.querySelectorAll("#release-history-list .release-row").length >= 2'));
    if (pagesFixture) {
      record("publish.reopened_hosted_state", await textIncludes("#hosting-state", "线上部署成功"));
      record("publish.reopened_hosted_history", await waitForUi(client, 'document.querySelectorAll("#hosting-history-list details").length >= 4'));
    }
    return;
  }

  const item = await evaluate(client, "(async () => {"
    + "const response = await fetch('/api/content', {method:'POST', headers:{"
    + "'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},"
    + "body:JSON.stringify({title:'Windows 发布验收',body:'合成的首次发布内容',visibility:'public'})});"
    + "return (await response.json()).data;})()");
  record('publish.seeded_synthetic_item', Boolean(item?.id));
  report.publishedContentId = item.id;
  record('publish.opened_page', await openPage('/releases'));
  record('publish.first_ready', await publishEnabled());
  record('publish.hosting_settings_available', await waitForUi(client, 'Boolean(document.querySelector("#hosting-settings"))'));
  record('publish.only_local_before_connection', await textIncludes('#hosting-state', '线上托管尚未连接'));
  if (pagesFixture) {
    const fixture = await evaluate(client, 'fetch("/__pages_test_fixture").then(response => response.text())');
    record("publish.mock_transport_installed", fixture === "synthetic-pages-fixture");
    await click("#hosting-settings > summary");
    await evaluate(client, 'document.getElementById("hosting-account").value = "cccccccccccccccccccccccccccccccc";'
      + 'document.getElementById("hosting-token").value = "synthetic-not-a-real-cloudflare-token";');
    await click("#hosting-list");
    record("publish.project_choice", await waitForUi(client, 'document.getElementById("hosting-project").value === "synthetic-space"'));
    await click("#hosting-save");
    record("publish.hosting_connected", await textIncludes("#hosting-state", "等待线上部署"));
  }
  await click('#one-click-preview-draft');
  record('publish.preview_link_generated', await waitForUi(client, 'Boolean(document.querySelector("#one-click-preview-draft-result a"))'));
  const preview = await evaluate(client, "(async () => {"
    + "const link = document.querySelector('#one-click-preview-draft-result a');"
    + "const response = await fetch(link.href); return response.ok && (await response.text()).includes('Windows 发布验收');})()");
  record('publish.draft_site_rendered', preview === true);
  await confirmPublish();
  record('publish.first_local_success', await textIncludes('#one-click-result', '本机发布成功'));
  if (pagesFixture) {
    record("publish.first_mock_online_success", await textIncludes("#one-click-result", "线上部署成功"));
    record("publish.public_url_displayed", await waitForUi(client, 'document.querySelector("#hosting-state a")?.href === "https://synthetic-space.pages.dev/"'));
  }
  record('publish.no_change_disabled', await waitForUi(client,
    'document.getElementById("one-click-publish").disabled && document.getElementById("one-click-chip").textContent.includes("一致")'
  ));

  const openEditor = async () => {
    record('publish.return_to_content', await openPage('/'));
    record('publish.synthetic_item_listed', await textIncludes('#content-list', 'Windows 发布验收'));
    await click('#content-list .content-item');
    record('publish.content_opened', await textIncludes('#reader-title', 'Windows 发布验收'));
    await click('#edit-button');
  };
  await openEditor();
  await evaluate(client, "(() => {const field = document.getElementById('body-input');"
    + "field.value = '合成的更新内容'; field.dispatchEvent(new Event('input',{bubbles:true}));})()");
  await click('#save-button');
  record('publish.edit_saved', await textIncludes('#save-status', '版本 2'));
  record('publish.update_page', await openPage('/releases'));
  record('publish.edit_detected', await textIncludes('#one-click-chip', '等待更新'));
  if (pagesFixture) fs.writeFileSync(pagesFixture, "upload");
  await confirmPublish();
  if (pagesFixture) {
    record("publish.mock_failure_visible", await textIncludes("#one-click-result", "线上部署失败"));
    record("publish.old_online_copy_warning", await textIncludes("#one-click-result", "线上公开副本仍可能可访问"));
    fs.writeFileSync(pagesFixture, "");
    record("publish.safe_retry_available", await textIncludes("#one-click-publish", "重试上线"));
    await confirmPublish();
    record("publish.mock_retry_success", await textIncludes("#one-click-result", "线上部署成功"));
  }
  record('publish.updated_local_site', await textIncludes('#one-click-result', '本机发布成功'));
  const updated = await evaluate(client, "(async () => {const response = await fetch('/p/" + item.id
    + "'); return response.ok && (await response.text()).includes('合成的更新内容');})()");
  record('publish.updated_public_text_verified', updated === true);

  await openEditor();
  await click('.visibility-button[data-visibility="private"]');
  await click('#save-button');
  record('publish.privacy_saved', await textIncludes('#save-status', '版本 3'));
  record('publish.privacy_warning_shown', await textIncludes('#publication-drift-note', '从公开网站移除'));
  await click('#publication-drift-note a');
  record('publish.removal_entry', await textIncludes('#one-click-publish', '从公开网站移除'));
  await confirmPublish();
  record('publish.local_withdrawal_succeeded', await textIncludes('#one-click-result', '本机网站已撤回'));
  if (pagesFixture) record("publish.mock_online_removal_verified", await textIncludes("#one-click-result", "线上部署成功"));
  const removed = await evaluate(client, "fetch('/p/" + item.id + "').then(response => response.status)");
  record('publish.public_content_404', removed === 404, removed);
}

async function connectCdp(webSocketUrl) {
  const socket = new WebSocket(webSocketUrl);
  const pending = new Map();
  const events = [];
  let nextId = 1;

  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('devtools websocket failed')), {
      once: true,
    });
  });

  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    } else if (message.method) {
      events.push(message);
    }
  });

  return {
    events,
    send(method, params = {}) {
      const id = nextId;
      nextId += 1;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      try {
        socket.close();
      } catch (error) {
        // Already closed.
      }
    },
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const dataDir = path.resolve(args.dataDir);
  const userDataDir = path.resolve(args.userDataDir);
  const debugPort = args.debugPort || 9333;
  const processImage = args.processImage || 'python.exe';

  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(userDataDir, { recursive: true });

  const beforePids = listPidsByImage(processImage);
  const report = { checks: [], screenshot: args.shot, backendPids: [], electronPid: null };

  const record = (name, ok, detail) => {
    report.checks.push({ name, ok, detail: detail === undefined ? '' : String(detail) });
    if (!ok) throw new Error(`check failed: ${name} (${detail})`);
  };

  const launchArgs = args.packaged
    ? [`--user-data-dir=${userDataDir}`, `--remote-debugging-port=${debugPort}`]
    : [
        path.resolve(args.app),
        `--user-data-dir=${userDataDir}`,
        `--remote-debugging-port=${debugPort}`,
      ];

  const childEnv = { ...process.env };
  if (!args.noEnvOverride) {
    childEnv.ARCHIVE_DATA_DIR = dataDir;
  } else {
    delete childEnv.ARCHIVE_DATA_DIR;
  }

  const child = spawn(path.resolve(args.electron), launchArgs, {
    cwd: path.resolve(args.app),
    env: childEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  report.electronPid = child.pid;

  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => {
    stderr += chunk;
  });

  const exited = new Promise((resolve) => {
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });

  let client = null;
  try {
    // Wait for the app window target.
    let target = null;
    const targetDeadline = Date.now() + 60000;
    while (Date.now() < targetDeadline) {
      try {
        const targets = await fetchJson(`http://127.0.0.1:${debugPort}/json/list`);
        target = targets.find(
          (entry) => entry.type === 'page' && (entry.url || '').startsWith('http://127.0.0.1:')
        );
        if (target) break;
      } catch (error) {
        // DevTools endpoint not up yet.
      }
      if (child.exitCode !== null) break;
      await delay(250);
    }
    record(
      'window.target_found',
      Boolean(target),
      target ? target.url.replace(/token=[^&]+/, 'token=<redacted>') : `none; stderr=${stderr.slice(-600)}`
    );

    client = await connectCdp(target.webSocketDebuggerUrl);
    await client.send('Runtime.enable');
    await client.send('Page.enable');

    // Wait until the archive UI has rendered inside the window.
    const uiDeadline = Date.now() + 30000;
    let pageState = null;
    while (Date.now() < uiDeadline) {
      const result = await client.send('Runtime.evaluate', {
        expression: `(() => {
          const spaces = document.querySelectorAll('#space-nav [data-space]').length;
          const overview = document.getElementById('overview-panel');
          const empty = document.getElementById('empty-state');
          const list = document.getElementById('content-list');
          const brand = document.querySelector('.brand-name');
          return JSON.stringify({
            spaces,
            brand: brand ? brand.textContent.trim() : '',
            overviewVisible: overview ? !overview.classList.contains('hidden') : false,
            emptyVisible: empty ? !empty.classList.contains('hidden') : false,
            items: list ? list.querySelectorAll('li').length : -1,
            hasList: Boolean(list),
            readyState: document.readyState,
            title: document.title,
          });
        })()`,
        returnByValue: true,
      });
      pageState = JSON.parse(result.result.value);
      if (pageState.spaces === 7 && pageState.brand && pageState.readyState === 'complete'
          && (pageState.overviewVisible || pageState.emptyVisible)) break;
      await delay(300);
    }

    record('ui.brand', pageState.brand === 'Archive', pageState.brand);
    record('ui.seven_spaces', pageState.spaces === 7, pageState.spaces);
    record(
      'ui.home_state',
      pageState.overviewVisible === true || pageState.emptyVisible === true,
      `overview=${pageState.overviewVisible} empty=${pageState.emptyVisible}`
    );
    report.homeState = pageState.emptyVisible ? 'empty' : 'overview';
    report.visibleItems = pageState.items;
    record('ui.content_list_present', pageState.hasList === true);
    record('ui.document_title', pageState.title.includes('Archive'), pageState.title);

    if (args.expectItems !== undefined) {
      record('ui.expected_items', pageState.items === args.expectItems, `${pageState.items} != ${args.expectItems}`);
    }

    if (args.shot) {
      const shot = await client.send('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(path.dirname(args.shot), { recursive: true });
      fs.writeFileSync(args.shot, Buffer.from(shot.data, 'base64'));
      record('ui.screenshot_saved', fs.existsSync(args.shot), `${fs.statSync(args.shot).size} bytes`);
    }

    const interaction = await client.send('Runtime.evaluate', {
      expression: `(() => {
        const buttons = [...document.querySelectorAll('#space-nav [data-space]')];
        const target = buttons.find((button) => button.dataset.space === 'BUILD');
        if (!target) return 'not-found';
        target.click();
        return 'clicked';
      })()`,
      returnByValue: true,
    });
    record('ui.space_click', interaction.result.value === 'clicked', interaction.result.value);
    await delay(900);

    const navigation = await client.send('Runtime.evaluate', {
      expression: `(() => {
        const target = document.querySelector('#space-nav [data-space="BUILD"]');
        return JSON.stringify({
          active: target ? target.classList.contains('active') : false,
          pressed: target ? target.getAttribute('aria-pressed') : null,
          listLabel: (document.getElementById('list-label') || {}).textContent || '',
        });
      })()`,
      returnByValue: true,
    });
    const navigated = JSON.parse(navigation.result.value);
    record('ui.space_navigation', navigated.active === true, JSON.stringify(navigated));

    if (args.uiCycle) {
      await runUiCycle(client, record, report);
    }

    if (args.publishCycle || args.publishReopen) {
      if (args.publishCycle && pageState.items !== 0) throw new Error('Publish cycle requires an empty test archive');
      await publishCycle(client, record, report, args.publishReopen, args.pagesFixture);
      const shot = await client.send('Page.captureScreenshot', { format: 'png' });
      const publishShot = args.shot.replace(/\.png$/i, '-publish.png');
      fs.writeFileSync(publishShot, Buffer.from(shot.data, 'base64'));
      report.publishScreenshot = publishShot;
    }

    // Backend process must be alive and must belong to this data directory.
    record('backend.lock_file_present', fs.existsSync(path.join(dataDir, '.app.lock')));
    const backendPids = findBackendPids(dataDir, processImage);
    report.backendPids = backendPids;
    record('backend.process_found', backendPids.length === 1, backendPids.join(','));
    const backendPid = backendPids[0];
    record('backend.process_alive', isAlive(backendPid), backendPid);
    record('backend.database_present', fs.existsSync(path.join(dataDir, 'archive.db')));

    const rendererErrors = client.events.filter(
      (event) => event.method === 'Runtime.exceptionThrown'
    );
    record('ui.no_renderer_exceptions', rendererErrors.length === 0, rendererErrors.length);

    if (args.keepOpen) {
      console.log(JSON.stringify({ ok: true, keptOpen: true, ...report }, null, 2));
      return;
    }

    // Close the window exactly like the user would.
    await client.send('Runtime.evaluate', { expression: 'window.close()' });
    client.close();
    client = null;

    const exit = await Promise.race([
      exited,
      delay(25000).then(() => ({ code: 'timeout' })),
    ]);
    record('shell.exited_after_close', exit.code === 0, `${exit.code}`);

    const backendDeadline = Date.now() + 15000;
    let backendAlive = isAlive(backendPid);
    while (Date.now() < backendDeadline && backendAlive) {
      await delay(200);
      backendAlive = isAlive(backendPid);
    }
    record('backend.stopped_with_shell', backendAlive === false);

    const afterPids = listPidsByImage(processImage);
    const unrelatedKilled = beforePids.filter((pid) => !afterPids.includes(pid));
    record('backend.no_unrelated_process_killed', unrelatedKilled.length === 0, unrelatedKilled.join(','));

    console.log(JSON.stringify({ ok: true, ...report }, null, 2));
  } finally {
    if (client) client.close();
    if (child.exitCode === null && !args.keepOpen) {
      child.kill();
    }
  }
}

main()
  .catch((error) => {
    console.error(JSON.stringify({ ok: false, error: String(error && error.message) }, null, 2));
    process.exitCode = 1;
  })
  .finally(() => {
    setTimeout(() => process.exit(process.exitCode || 0), 300);
  });
