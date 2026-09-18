/* ===========================================================================
   半结构化文件入库平台 —— 前端交互
   流程：上传文件 → 解析预览 → 推断表结构 → 执行入库 → 查看结果
   =========================================================================== */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    fileId: null,
    meta: null,
    parse: null,
    schema: null,
  };

  /* ─────────────────────────── 工具函数 ─────────────────────────── */
  const escapeHtml = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));

  const formatCell = (value) => {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  };

  const formatBytes = (bytes) => {
    if (bytes === null || bytes === undefined) return '—';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = Number(bytes);
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
  };

  const baseName = (path) => String(path || '').split(/[\\/]/).pop() || '—';

  const resolveEl = (target) => (typeof target === 'string' ? $(target) : target);

  let toastTimer = null;
  function toast(message, type = 'error') {
    const el = $('toast');
    el.textContent = message;
    el.className = `toast show${type === 'error' ? ' error' : ''}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, 4600);
  }

  async function api(path, { method = 'POST', body, isForm = false } = {}) {
    const options = { method };
    if (body !== undefined) {
      if (isForm) {
        options.body = body;
      } else {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(body);
      }
    }

    const res = await fetch(path, options);
    const text = await res.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text.slice(0, 400) };
      }
    }
    if (!res.ok) {
      throw new Error(data.detail || `请求失败（HTTP ${res.status}）`);
    }
    return data;
  }

  /* ─────────────────────────── 卡片状态 ─────────────────────────── */
  const unlockCard = (id) => $(id).classList.remove('locked');
  const lockCard = (id) => $(id).classList.add('locked');

  function setBusy(cardId, busy, text = '处理中…') {
    const card = $(cardId);
    const existing = card.querySelector('.busy');
    if (!busy) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const overlay = document.createElement('div');
    overlay.className = 'busy';
    overlay.innerHTML =
      `<div class="busy-inner"><span class="spinner"></span><span>${escapeHtml(text)}</span></div>`;
    card.appendChild(overlay);
  }

  /* ─────────────────────────── 渲染 ─────────────────────────── */
  function renderTable(target, columns, records, headers = null) {
    const container = resolveEl(target);
    if (!records || !records.length) {
      container.innerHTML = '<p class="empty">没有可展示的数据</p>';
      return;
    }

    const head = columns
      .map((col, index) => `<th>${escapeHtml(headers ? headers[index] : col)}</th>`)
      .join('');

    const body = records
      .map((row) => {
        const cells = columns
          .map((col) => {
            const raw = row ? row[col] : undefined;
            const text = formatCell(raw);
            const cls = raw === null || raw === undefined ? ' class="null"' : '';
            return `<td${cls} title="${escapeHtml(text)}">${escapeHtml(text)}</td>`;
          })
          .join('');
        return `<tr>${cells}</tr>`;
      })
      .join('');

    container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function renderFileCard() {
    const meta = state.meta;
    if (!meta) {
      $('fileCard').innerHTML = '';
      return;
    }
    const ext = (meta.extension || '').replace('.', '').toUpperCase() || 'FILE';
    $('fileCard').innerHTML = `
      <div class="file-card">
        <div class="file-icon">${escapeHtml(ext.slice(0, 4))}</div>
        <div>
          <div class="file-name">${escapeHtml(meta.name)}</div>
          <div class="file-meta">
            ${formatBytes(meta.size_bytes)} · 编码 ${escapeHtml(meta.encoding)} · ${escapeHtml(meta.modified_at)}
          </div>
        </div>
      </div>`;
  }

  function renderResult(report) {
    const load = report.load;
    const ok = report.success !== false;
    const title = report.dry_run ? '⚑ 演练完成（未写入）' : (ok ? '✓ 入库成功' : '✕ 存在失败行');
    const message = load
      ? load.message
      : '演练模式：仅生成建表语句与统计，未写入 Doris。';

    const stats = [
      ['来源文件', baseName(report.source), 'sm'],
      ['目标表', report.table, 'sm'],
      ['解析行数', report.parsed ?? 0],
      ['抽取行数', report.extracted ?? 0],
      ['入库成功', load ? load.loaded : '—'],
      ['入库失败', load ? load.failed : '—'],
      ['写入方式', load ? load.mode : (report.dry_run ? 'dry-run' : '—'), 'sm'],
    ];

    const statsHtml = stats
      .map(([key, value, cls]) =>
        `<div class="stat"><div class="k">${escapeHtml(key)}</div>` +
        `<div class="v ${cls || ''}">${escapeHtml(value)}</div></div>`)
      .join('');

    $('resultBox').innerHTML = `
      <div class="result-box ${ok ? 'ok' : 'fail'}">
        <div class="result-title">${title}</div>
        <div class="result-msg">${escapeHtml(message)}</div>
        <div class="stats">${statsHtml}</div>
        ${report.dry_run ? '' : '<div class="result-actions"><button class="btn ghost" id="btnVerify">查询前 10 行验证</button></div>'}
      </div>`;

    $('resultMeta').textContent = report.dry_run ? '演练模式' : (ok ? '写入完成' : '存在失败行');

    const verify = $('btnVerify');
    if (verify) verify.addEventListener('click', () => verifyData(report.table));
  }

  async function verifyData(table) {
    const box = $('resultBox');
    try {
      const data = await api('/api/query', {
        body: { sql: `SELECT * FROM \`${table}\` LIMIT 10`, limit: 10 },
      });
      const rows = data.rows || [];
      const columns = rows.length ? Object.keys(rows[0]) : [];
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      wrap.style.marginTop = '14px';
      box.appendChild(wrap);
      renderTable(wrap, columns, rows);
    } catch (err) {
      toast(err.message);
    }
  }

  /* ─────────────────────────── 流程 ─────────────────────────── */
  async function onFileReady(payload) {
    state.fileId = payload.file_id;
    state.meta = payload.meta;
    state.parse = null;
    state.schema = null;

    renderFileCard();
    lockCard('step3');
    lockCard('step4');
    lockCard('step5');
    $('previewTable').innerHTML = '<p class="empty">正在解析…</p>';
    $('schemaTable').innerHTML = '<p class="empty">等待推断</p>';
    $('ddlBox').textContent = '';
    $('resultBox').innerHTML = '<p class="empty">执行入库后展示结果</p>';
    $('resultMeta').textContent = '尚未执行';
    $('btnIngest').disabled = true;

    unlockCard('step2');
    await doParse();
  }

  async function uploadFile(file) {
    if (!file) return;
    setBusy('step1', true, '上传中…');
    try {
      const form = new FormData();
      form.append('file', file);
      const data = await api('/api/upload', { body: form, isForm: true });
      await onFileReady(data);
    } catch (err) {
      toast(err.message);
    } finally {
      setBusy('step1', false);
    }
  }

  async function doParse() {
    if (!state.fileId) return;
    setBusy('step2', true, '解析中…');
    try {
      const data = await api('/api/parse', {
        body: { file_id: state.fileId, limit: 20 },
      });
      state.parse = data;
      renderTable('previewTable', data.columns, data.records);

      const errText = data.errors && data.errors.length ? ` · ${data.errors.length} 个错误` : '';
      $('parseMeta').textContent =
        `解析器 ${data.parser} · 共 ${data.record_count} 条记录${errText}`;
      $('btnReparse').disabled = false;

      unlockCard('step3');
      await doSchema();
    } catch (err) {
      $('previewTable').innerHTML = `<p class="empty">解析失败：${escapeHtml(err.message)}</p>`;
      $('parseMeta').textContent = '解析失败';
      toast(err.message);
    } finally {
      setBusy('step2', false);
    }
  }

  async function doSchema() {
    if (!state.fileId) return;
    setBusy('step3', true, '推断表结构中…');
    try {
      const engine = document.querySelector('input[name="engine"]:checked').value;
      const data = await api('/api/schema', {
        body: {
          file_id: state.fileId,
          table: $('tableSelect').value,
          use_llm: engine === 'rule' ? false : null,
        },
      });
      state.schema = data;

      const rows = data.spec.columns.map((column) => ({
        name: column.name,
        dtype: column.dtype,
        strict: column.nullable ? '可空' : '必填',
        comment: column.comment || '',
      }));
      renderTable('schemaTable', ['name', 'dtype', 'strict', 'comment'], rows,
        ['字段', '类型', '约束', '注释']);

      const sourceLabel = {
        llm: 'LLM 推断',
        heuristic: '规则推断',
        'config/tables.yaml': '配置文件',
      }[data.inference] || data.inference;

      $('schemaMeta').textContent =
        `${data.table} · 来源：${sourceLabel} · ${data.spec.columns.length} 个字段`;
      $('ddlBox').textContent = data.ddl;
      $('ddlBox').parentElement.open = false;

      unlockCard('step4');
      $('btnIngest').disabled = false;
      updateIngestLabel();
    } catch (err) {
      $('schemaTable').innerHTML = `<p class="empty">推断失败：${escapeHtml(err.message)}</p>`;
      $('schemaMeta').textContent = '推断失败';
      toast(err.message);
    } finally {
      setBusy('step3', false);
    }
  }

  function updateIngestLabel() {
    $('btnIngest').textContent = $('dryRun').checked
      ? '预览入库结果（不写库）'
      : '开始入库';
  }

  async function doIngest() {
    if (!state.fileId) return;
    const dryRun = $('dryRun').checked;
    const btn = $('btnIngest');

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>执行中…';
    unlockCard('step5');
    setBusy('step5', true, dryRun ? '演练中…' : '写入 Doris 中…');

    try {
      const engine = document.querySelector('input[name="engine"]:checked').value;
      const report = await api('/api/ingest', {
        body: {
          file_id: state.fileId,
          table: $('tableSelect').value,
          mode: $('modeSelect').value,
          dry_run: dryRun,
          use_llm: engine === 'rule' ? false : null,
        },
      });
      renderResult(report);
    } catch (err) {
      $('resultBox').innerHTML =
        `<div class="result-box fail"><div class="result-title">✕ 执行失败</div>` +
        `<div class="result-msg">${escapeHtml(err.message)}</div></div>`;
      $('resultMeta').textContent = '执行失败';
      toast(err.message);
    } finally {
      btn.disabled = false;
      updateIngestLabel();
      setBusy('step5', false);
    }
  }

  /* ─────────────────────────── 环境与示例 ─────────────────────────── */
  function setPill(id, ok, text) {
    const el = $(id);
    el.className = `pill ${ok ? 'ok' : 'bad'}`;
    el.innerHTML = `<i class="dot"></i><span>${escapeHtml(text)}</span>`;
  }

  async function checkHealth() {
    try {
      const data = await api('/api/health', { method: 'GET' });

      setPill('pillLlm', data.llm.configured,
        data.llm.configured ? `LLM ${data.llm.model}` : 'LLM 未配置 key');
      setPill('pillDoris', data.doris.connected,
        data.doris.connected ? `Doris ${data.doris.database}` : 'Doris 连接失败');

      const tables = data.tables || [];
      $('tableSelect').innerHTML = '<option value="">（按文件名自动匹配）</option>' +
        tables.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join('');
    } catch {
      setPill('pillLlm', false, 'LLM 状态未知');
      setPill('pillDoris', false, 'Doris 状态未知');
    }
  }

  async function loadSamples() {
    try {
      const data = await api('/api/samples', { method: 'GET' });
      const files = data.files || [];
      if (!files.length) return;

      const box = $('samples');
      box.innerHTML = '<span class="samples-label">快速体验：</span>' +
        files.map((f) => `<button type="button" class="chip" data-sample="${escapeHtml(f.name)}">${escapeHtml(f.name)}</button>`).join('');

      box.addEventListener('click', async (event) => {
        const chip = event.target.closest('.chip');
        if (!chip) return;
        setBusy('step1', true, '加载示例…');
        try {
          const payload = await api('/api/sample', { body: { name: chip.dataset.sample } });
          await onFileReady(payload);
        } catch (err) {
          toast(err.message);
        } finally {
          setBusy('step1', false);
        }
      });
    } catch {
      /* 示例数据不可用时静默忽略 */
    }
  }

  /* ─────────────────────────── 智能助手对话 ─────────────────────────── */
  const CHAT_SUGGESTIONS = [
    '列出 examples/sample_data 下有哪些文件，说明各自格式',
    '把 orders.jsonl 解析并写入 Doris，完成后统计行数',
    '检查 Doris 连接是否正常，并列出已声明的目标表',
    '查询 ods_order_events 的前 5 行数据',
  ];

  const chat = { sessionId: '', busy: false, controller: null };

  function renderMarkdown(text) {
    let html = escapeHtml(text || '');

    // 代码块先抽出来占位，避免内部内容被后续规则误处理
    const blocks = [];
    html = html.replace(/```[a-zA-Z]*\n?([\s\S]*?)```/g, (_, code) => {
      blocks.push(`<pre class="md-code">${code.replace(/^\n+|\n+$/g, '')}</pre>`);
      return `\u0001${blocks.length - 1}\u0001`;
    });

    html = html.replace(/`([^`\n]+)`/g, '<code class="md-inline">$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/^\s*#{1,4}\s*(.+)$/gm, '<div class="md-h">$1</div>');
    html = html.replace(/^\s*[-*]\s+(.+)$/gm, '<div class="md-li">• $1</div>');

    html = html.replace(/\n/g, '<br>');
    html = html.replace(/<br>(\s*)<div class="md-/g, '$1<div class="md-');
    html = html.replace(/<\/(div|pre)><br>/g, '</$1>');
    html = html.replace(/\u0001(\d+)\u0001/g, (_, index) => blocks[Number(index)]);

    return html;
  }

  function formatToolArgs(args) {
    if (!args || typeof args !== 'object') return '';
    return Object.entries(args)
      .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
      .join('  ');
  }

  function scrollChat(force = false) {
    const log = $('chatLog');
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    if (force || nearBottom) log.scrollTop = log.scrollHeight;
  }

  /** 普通气泡（用户消息 / 简单提示），返回正文元素。 */
  function chatAppend(role, html) {
    const log = $('chatLog');
    const node = document.createElement('div');
    node.className = `msg ${role}`;
    node.innerHTML =
      `<div class="msg-avatar">${role === 'user' ? '我' : 'AI'}</div>` +
      `<div class="msg-body"><div class="msg-text">${html}</div></div>`;
    log.appendChild(node);
    scrollChat(true);
    return node.querySelector('.msg-text');
  }

  /** 助手气泡：正文与工具轨迹分开，便于流式增量更新。 */
  function chatBotBubble() {
    const log = $('chatLog');
    const node = document.createElement('div');
    node.className = 'msg bot';
    node.innerHTML =
      '<div class="msg-avatar">AI</div>' +
      '<div class="msg-body">' +
        '<div class="msg-text streaming"></div>' +
        '<div class="tool-trace" hidden></div>' +
      '</div>';
    log.appendChild(node);
    scrollChat(true);
    return {
      text: node.querySelector('.msg-text'),
      trace: node.querySelector('.tool-trace'),
    };
  }

  function renderTrace(steps) {
    if (!steps || !steps.length) return '';
    return `<div class="tool-trace-title">⚙ 本轮调用了 ${steps.length} 个工具</div>` +
      steps
        .map((step) =>
          `<div class="tool-item"><code>${escapeHtml(step.name)}</code>` +
          `<span class="tool-args">${escapeHtml(formatToolArgs(step.args))}</span></div>`)
        .join('');
  }

  function finishBubble(bubble, answer, steps, note) {
    bubble.text.classList.remove('streaming');
    let html = renderMarkdown(answer || '（没有返回内容）');
    if (note) html += `<div class="chat-note">${escapeHtml(note)}</div>`;
    bubble.text.innerHTML = html;
    if (steps && steps.length) {
      bubble.trace.hidden = false;
      bubble.trace.innerHTML = renderTrace(steps);
    }
  }

  function parseSseBlock(raw) {
    let event = 'message';
    const dataLines = [];
    for (const line of raw.split(/\r?\n/)) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try {
      return { event, data: JSON.parse(dataLines.join('\n')) };
    } catch {
      return { event, data: {} };
    }
  }

  /** 读取 SSE 流并逐事件回调。用 fetch 而非 EventSource，因为需要 POST。 */
  async function readSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    const dispatch = (raw) => {
      const parsed = parseSseBlock(raw);
      if (parsed) onEvent(parsed.event, parsed.data);
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let index = buffer.indexOf('\n\n');
      while (index !== -1) {
        dispatch(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf('\n\n');
      }
    }

    if (buffer.trim()) dispatch(buffer);
  }

  async function sendChat(message) {
    const text = (message || '').trim();
    if (!text || chat.busy) return;

    chat.busy = true;
    chat.controller = new AbortController();
    setComposerBusy(true);

    chatAppend('user', escapeHtml(text).replace(/\n/g, '<br>'));

    const bubble = chatBotBubble();
    bubble.text.innerHTML = '<span class="spinner"></span> 正在思考…';

    let answer = '';
    let steps = [];
    let streaming = false;

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, session_id: chat.sessionId }),
        signal: chat.controller.signal,
      });

      if (!response.ok || !response.body) {
        const raw = await response.text().catch(() => '');
        let detail = `请求失败（HTTP ${response.status}）`;
        try {
          detail = JSON.parse(raw).detail || detail;
        } catch {
          /* 保留默认提示 */
        }
        throw new Error(detail);
      }

      await readSse(response, (event, data) => {
        if (event === 'token') {
          if (!streaming) {
            bubble.text.innerHTML = '';
            streaming = true;
          }
          answer += data.text || '';
          bubble.text.textContent = answer;
          scrollChat();
        } else if (event === 'tool') {
          steps.push(data);
          bubble.trace.hidden = false;
          bubble.trace.innerHTML = renderTrace(steps);
          scrollChat();
        } else if (event === 'done') {
          chat.sessionId = data.session_id || chat.sessionId;
          if (data.answer) answer = data.answer;
          if (data.steps && data.steps.length) steps = data.steps;
        } else if (event === 'error') {
          throw new Error(data.message || '执行失败');
        }
      });

      finishBubble(bubble, answer, steps, '');
    } catch (err) {
      if (err && err.name === 'AbortError') {
        finishBubble(bubble, answer, steps, '已手动停止生成');
      } else {
        bubble.text.classList.remove('streaming');
        bubble.text.innerHTML =
          `<span class="msg-error">⚠ ${escapeHtml(err.message || String(err))}</span>`;
        if (steps.length) {
          bubble.trace.hidden = false;
          bubble.trace.innerHTML = renderTrace(steps);
        }
      }
    } finally {
      chat.busy = false;
      chat.controller = null;
      setComposerBusy(false);
      $('chatInput').focus();
      scrollChat();
    }
  }

  function setComposerBusy(busy) {
    $('chatInput').disabled = busy;
    $('btnSend').textContent = busy ? '停止' : '发送';
    $('btnSend').classList.toggle('stopping', busy);
  }

  async function clearChat() {
    if (chat.controller) chat.controller.abort();
    try {
      if (chat.sessionId) {
        await api('/api/chat/reset', { body: { session_id: chat.sessionId } });
      }
    } catch {
      /* 重置失败不影响本地清空 */
    }
    chat.sessionId = '';
    $('chatLog').innerHTML = '';
    chatAppend('bot', renderMarkdown('对话已清空。你可以继续让我侦察文件、解析入库或查询数据。'));
  }

  function autoGrow(input) {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  }

  function bindChat() {
    const input = $('chatInput');
    const form = $('chatForm');
    const suggest = $('chatSuggest');

    suggest.innerHTML = CHAT_SUGGESTIONS
      .map((text) =>
        `<button type="button" class="chip" data-q="${escapeHtml(text)}">${escapeHtml(text)}</button>`)
      .join('');

    suggest.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      input.value = chip.dataset.q;
      input.focus();
      autoGrow(input);
    });

    const submit = () => {
      if (chat.busy) return;
      const text = input.value;
      input.value = '';
      autoGrow(input);
      sendChat(text);
    };

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      submit();
    });

    $('btnSend').addEventListener('click', () => {
      // 生成中再点一次即中断
      if (chat.busy) {
        if (chat.controller) chat.controller.abort();
        return;
      }
      submit();
    });

    input.addEventListener('keydown', (event) => {
      // isComposing：避免中文输入法选词时回车误触发送
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!chat.busy) submit();
      }
    });

    input.addEventListener('input', () => autoGrow(input));
    $('btnClearChat').addEventListener('click', clearChat);

    chatAppend(
      'bot',
      renderMarkdown(
        '你好，我是入库智能体。可以直接用自然语言让我完成：文件侦察 → 解析 → 建表 → 入库 → 查询校验。'
      )
    );
  }

  /* ─────────────────────────── 事件绑定 ─────────────────────────── */
  function bindDropzone() {
    const zone = $('dropzone');
    const input = $('fileInput');

    ['dragenter', 'dragover'].forEach((evt) =>
      zone.addEventListener(evt, (event) => {
        event.preventDefault();
        zone.classList.add('drag');
      }));

    ['dragleave', 'drop'].forEach((evt) =>
      zone.addEventListener(evt, (event) => {
        event.preventDefault();
        zone.classList.remove('drag');
      }));

    zone.addEventListener('drop', (event) => {
      const file = event.dataTransfer && event.dataTransfer.files[0];
      if (file) uploadFile(file);
    });

    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (file) uploadFile(file);
      input.value = '';
    });
  }

  function init() {
    bindDropzone();
    bindChat();

    $('btnReparse').addEventListener('click', doParse);
    $('btnIngest').addEventListener('click', doIngest);
    $('dryRun').addEventListener('change', updateIngestLabel);

    document.querySelectorAll('input[name="engine"]').forEach((radio) =>
      radio.addEventListener('change', () => {
        if (state.parse) doSchema();
      }));

    $('tableSelect').addEventListener('change', () => {
      if (state.parse) doSchema();
    });

    updateIngestLabel();
    checkHealth();
    loadSamples();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
