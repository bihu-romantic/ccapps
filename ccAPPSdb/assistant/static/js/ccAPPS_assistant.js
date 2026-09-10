/**
 * ccAPPS AI 助手 - VSCode 风格分栏布局
 */
var assistantHistory = [];
var assistantLoading = false;
var assistantAbortController = null;
var assistantRequestId = null;
var assistantPanelWidth = 420;
var assistantMinPanelWidth = 320;
var assistantMaxPanelRatio = 0.6;
var assistantStorageKey = 'ccAPPS_assistant_state';
var assistantSessionsKey = 'ccAPPS_assistant_sessions';
var assistantCurrentSessionId = null;
var assistantSessions = [];
var assistantMaxSessions = 20;
var assistantPendingFiles = [];
var MAX_FILE_SIZE = 5 * 1024 * 1024; // 5MB
var assistantWelcomeMessage = '你好！我是AI助手。我可以查看当前页面数据，帮你解答问题、导航操作和分析数据。';

(function() {

// 高风险操作关键词 - 需要用户确认
var highRiskPatterns = [
  /sudo\b/i,
  /rm\s+-rf/i,
  /DROP\s+(TABLE|DATABASE)/i,
  /DELETE\s+FROM/i,
  /TRUNCATE/i,
  /ALTER\s+(TABLE|SYSTEM)/i,
  /shutdown/i,
  /reboot/i,
  /kill\s+-9/i,
  /chmod\s+777/i,
  /chown\s+-R/i,
  /FORMAT\s+(C:|disk)/i,
  /dd\s+if=/i,
  /mkfs/i,
  /\bgit\s+push\s+--force\b/i,
  /\bgit\s+reset\s+--hard\b/i,
];

function isHighRisk(message) {
  for (var i = 0; i < highRiskPatterns.length; i++) {
    if (highRiskPatterns[i].test(message)) {
      return true;
    }
  }
  return false;
}

// ---- 跨页面状态持久化 ----
// 同时写 sessionStorage 和 localStorage，确保跨页面跳转不丢失

function saveState() {
  try {
    var state = {
      history: assistantHistory,
      currentSessionId: assistantCurrentSessionId,
      panelWidth: assistantPanelWidth,
      panelHidden: document.getElementById('assistant-panel') ?
        document.getElementById('assistant-panel').classList.contains('assistant-hidden') : false,
      confirmed: localStorage.getItem('assistant_confirmed') === '1',
      savedAt: Date.now()
    };
    var json = JSON.stringify(state);
    sessionStorage.setItem(assistantStorageKey, json);
    // 同时写 localStorage 作为备份（sessionStorage 在某些导航场景可能丢失）
    localStorage.setItem(assistantStorageKey, json);
  } catch(e) {}
}

function loadState() {
  try {
    // 优先从 sessionStorage 读取（同标签页），失败则回退到 localStorage
    var raw = sessionStorage.getItem(assistantStorageKey);
    if (!raw) {
      raw = localStorage.getItem(assistantStorageKey);
    }
    if (!raw) return null;

    var state = JSON.parse(raw);

    // 超过 30 分钟的旧数据视为过期
    if (state.savedAt && (Date.now() - state.savedAt) > 30 * 60 * 1000) {
      localStorage.removeItem(assistantStorageKey);
      sessionStorage.removeItem(assistantStorageKey);
      return null;
    }

    return state;
  } catch(e) {
    return null;
  }
}

function loadSessions() {
  try {
    var raw = localStorage.getItem(assistantSessionsKey);
    assistantSessions = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(assistantSessions)) {
      assistantSessions = [];
    }
  } catch(e) {
    assistantSessions = [];
  }
}

function saveSessions() {
  try {
    localStorage.setItem(assistantSessionsKey, JSON.stringify(assistantSessions.slice(0, assistantMaxSessions)));
  } catch(e) {}
}

function assistantCreateSessionId() {
  return 'session_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
}

function assistantGetSessionTitle(history) {
  for (var i = 0; i < history.length; i++) {
    if (history[i].role === 'user' && history[i].content) {
      return history[i].content.replace(/\s+/g, ' ').slice(0, 32);
    }
  }
  return '新会话';
}

function assistantFormatSessionTime(ts) {
  if (!ts) return '';
  var date = new Date(ts);
  var now = new Date();
  var sameDay = date.toDateString() === now.toDateString();
  var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
  if (sameDay) {
    return pad(date.getHours()) + ':' + pad(date.getMinutes());
  }
  return (date.getMonth() + 1) + '/' + date.getDate() + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

function assistantArchiveCurrentSession() {
  if (!assistantHistory || assistantHistory.length === 0) return;
  if (!assistantCurrentSessionId) {
    assistantCurrentSessionId = assistantCreateSessionId();
  }

  var now = Date.now();
  var session = {
    id: assistantCurrentSessionId,
    title: assistantGetSessionTitle(assistantHistory),
    history: assistantHistory.slice(),
    updatedAt: now,
    createdAt: now
  };

  var existingIndex = -1;
  for (var i = 0; i < assistantSessions.length; i++) {
    if (assistantSessions[i].id === assistantCurrentSessionId) {
      existingIndex = i;
      session.createdAt = assistantSessions[i].createdAt || now;
      break;
    }
  }
  if (existingIndex >= 0) {
    assistantSessions.splice(existingIndex, 1);
  }
  assistantSessions.unshift(session);
  assistantSessions = assistantSessions.slice(0, assistantMaxSessions);
  saveSessions();
  assistantRenderHistoryPanel();
}

function assistantRenderWelcome() {
  var container = document.getElementById('assistant-messages');
  if (!container) return;
  container.innerHTML = '';
  assistantAddMessageToContainer(container, 'system', assistantWelcomeMessage);
  container.scrollTop = container.scrollHeight;
}

function restoreMessages(container) {
  for (var i = 0; i < assistantHistory.length; i++) {
    var msg = assistantHistory[i];
    assistantAddMessageToContainer(container, msg.role, msg.content);
  }
  container.scrollTop = container.scrollHeight;
}

function assistantRenderHistoryPanel() {
  var panel = document.getElementById('assistant-history-panel');
  var list = document.getElementById('assistant-history-list');
  var count = document.getElementById('assistant-history-count');
  if (!panel || !list) return;

  if (count) {
    count.textContent = assistantSessions.length ? assistantSessions.length + ' 条' : '';
  }
  if (assistantSessions.length === 0) {
    list.innerHTML = '<div class="assistant-history-empty">暂无历史会话</div>';
    return;
  }

  var html = '';
  for (var i = 0; i < assistantSessions.length; i++) {
    var session = assistantSessions[i];
    var active = session.id === assistantCurrentSessionId ? ' assistant-history-item-active' : '';
    html += '<button type="button" class="assistant-history-item' + active + '" data-session-id="' + escapeHtml(session.id) + '">';
    html += '<span class="assistant-history-item-title">' + escapeHtml(session.title || '未命名会话') + '</span>';
    html += '<span class="assistant-history-item-time">' + assistantFormatSessionTime(session.updatedAt) + '</span>';
    html += '</button>';
  }
  list.innerHTML = html;
}

function assistantToggleHistoryPanel(forceOpen) {
  var panel = document.getElementById('assistant-history-panel');
  if (!panel) return;
  var shouldOpen = typeof forceOpen === 'boolean' ? forceOpen : !panel.classList.contains('assistant-history-open');
  panel.classList.toggle('assistant-history-open', shouldOpen);
  assistantRenderHistoryPanel();
}

function assistantNewSession() {
  if (assistantLoading) {
    alert('AI正在回复，请先停止生成或等待完成后再新建会话。');
    return;
  }
  assistantArchiveCurrentSession();
  assistantHistory = [];
  assistantCurrentSessionId = assistantCreateSessionId();
  assistantPendingFiles = [];
  assistantRenderFilePreview();
  assistantRenderWelcome();
  assistantToggleHistoryPanel(false);
  saveState();
  var input = document.getElementById('assistant-input');
  if (input) input.focus();
}

function assistantLoadSession(sessionId) {
  if (assistantLoading) {
    alert('AI正在回复，请先停止生成或等待完成后再切换历史会话。');
    return;
  }
  if (sessionId === assistantCurrentSessionId) {
    assistantToggleHistoryPanel(false);
    return;
  }
  var session = null;
  for (var i = 0; i < assistantSessions.length; i++) {
    if (assistantSessions[i].id === sessionId) {
      session = assistantSessions[i];
      break;
    }
  }
  if (!session) return;

  assistantArchiveCurrentSession();
  assistantCurrentSessionId = session.id;
  assistantHistory = (session.history || []).slice();
  var container = document.getElementById('assistant-messages');
  if (container) {
    container.innerHTML = '';
    restoreMessages(container);
  }
  assistantToggleHistoryPanel(false);
  saveState();
}

function setAssistantPanelExpanded(expanded) {
  var body = document.body;
  if (!body) return;
  body.classList.toggle('assistant-panel-open', expanded);
  body.classList.toggle('assistant-panel-closed', !expanded);
}

function clampAssistantPanelWidth(width) {
  var maxWidth = Math.max(assistantMinPanelWidth, Math.floor(window.innerWidth * assistantMaxPanelRatio));
  return Math.max(assistantMinPanelWidth, Math.min(width, maxWidth));
}

function applyAssistantPanelWidth(width) {
  assistantPanelWidth = clampAssistantPanelWidth(width || assistantPanelWidth);
  document.documentElement.style.setProperty('--assistant-panel-width', assistantPanelWidth + 'px');
}

// ---- 布局初始化 ----

var _initRetries = 0;
function initAssistantLayout() {
  var panel = document.getElementById('assistant-panel');
  if (!panel) {
    // DOM may not be ready yet, retry up to 20 times (2 seconds)
    if (_initRetries < 20) {
      _initRetries++;
      setTimeout(initAssistantLayout, 100);
    }
    return;
  }

  loadSessions();

  // Restore saved state
  var saved = loadState();
  if (saved) {
    assistantHistory = saved.history || [];
    assistantCurrentSessionId = saved.currentSessionId || assistantCurrentSessionId;
    if (saved.panelWidth) {
      assistantPanelWidth = saved.panelWidth;
    }
  }
  applyAssistantPanelWidth(assistantPanelWidth);

  // Create main content wrapper
  var wrapper = document.createElement('div');
  wrapper.id = 'assistant-main-wrapper';

  // Move all body children into the wrapper EXCEPT assistant elements
  var body = document.body;
  var children = Array.from(body.children);
  for (var i = 0; i < children.length; i++) {
    var el = children[i];
    if (el.id === 'assistant-panel' || el.id === 'assistant-splitter'
        || el.id === 'assistant-reopen') {
      continue;
    }
    wrapper.appendChild(el);
  }

  // Create splitter
  var splitter = document.createElement('div');
  splitter.id = 'assistant-splitter';

  // Create reopen button (hidden by default)
  var reopen = document.createElement('div');
  reopen.id = 'assistant-reopen';
  reopen.textContent = 'AI助手';
  reopen.title = '打开AI助手';
  reopen.addEventListener('click', function() {
    panel.classList.remove('assistant-hidden');
    setAssistantPanelExpanded(true);
    saveState();
    // Scroll to bottom when re-opening
    var container = document.getElementById('assistant-messages');
    if (container) container.scrollTop = container.scrollHeight;
  });

  // Rebuild body: wrapper, splitter, panel, reopen
  body.appendChild(wrapper);
  body.appendChild(splitter);
  body.appendChild(panel);
  body.appendChild(reopen);
  body.classList.add('assistant-layout');

  // Restore panel visibility - if previously hidden, keep it hidden
  if (saved && saved.panelHidden) {
    panel.classList.add('assistant-hidden');
  }
  setAssistantPanelExpanded(!panel.classList.contains('assistant-hidden'));

  // Restore chat messages - with retry in case DOM isn't ready yet
  if (saved && saved.history && saved.history.length > 0) {
    var retries = 0;
    var maxRetries = 10;
    function tryRestore() {
      var container = document.getElementById('assistant-messages');
      if (container) {
        // Clear default greeting message
        container.innerHTML = '';
        restoreMessages(container);
      } else if (retries < maxRetries) {
        retries++;
        setTimeout(tryRestore, 100);
      }
    }
    tryRestore();
  }
  assistantRenderHistoryPanel();

  // Splitter drag behavior
  var dragState = null;
  splitter.addEventListener('mousedown', function(e) {
    if (panel.classList.contains('assistant-hidden')) return;
    if (window.innerWidth <= 768) return;
    e.preventDefault();
    dragState = {
      startX: e.clientX,
      startWidth: panel.getBoundingClientRect().width
    };
    body.classList.add('assistant-resizing');
  });

  document.addEventListener('mousemove', function(e) {
    if (!dragState) return;
    applyAssistantPanelWidth(dragState.startWidth + dragState.startX - e.clientX);
  });

  document.addEventListener('mouseup', function() {
    if (!dragState) return;
    dragState = null;
    body.classList.remove('assistant-resizing');
    saveState();
  });

  window.addEventListener('blur', function() {
    if (!dragState) return;
    dragState = null;
    body.classList.remove('assistant-resizing');
    saveState();
  });

  window.addEventListener('resize', function() {
    applyAssistantPanelWidth(assistantPanelWidth);
  });

  // Close button
  var closeBtn = document.getElementById('assistant-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', function() {
      panel.classList.add('assistant-hidden');
      setAssistantPanelExpanded(false);
      saveState();
    });
  }

  // Confirmation overlay
  var confirmOverlay = document.getElementById('assistant-confirm');
  if (confirmOverlay && localStorage.getItem('assistant_confirmed') === '1') {
    confirmOverlay.classList.add('assistant-confirmed');
  }
}

// ---- 文件上传 ----

function assistantHandleFile(event) {
  var files = event.target.files;
  if (!files || files.length === 0) return;

  for (var i = 0; i < files.length; i++) {
    var file = files[i];

    // Size limit check
    if (file.size > MAX_FILE_SIZE) {
      var fMb = (file.size / 1024 / 1024).toFixed(1);
      var limitMb = (MAX_FILE_SIZE / 1024 / 1024).toFixed(0);
      var sizeMsg = '⚠️ 文件 "' + file.name + '"（' + fMb + 'MB）超过 ' + limitMb + 'MB 限制，已跳过。';
      assistantAddMessage('system', sizeMsg);
      alert(sizeMsg);
      continue;
    }

    // Duplicate check
    var dup = false;
    for (var j = 0; j < assistantPendingFiles.length; j++) {
      if (assistantPendingFiles[j].name === file.name && assistantPendingFiles[j].size === file.size) {
        dup = true;
        break;
      }
    }
    if (dup) continue;

    assistantPendingFiles.push(file);
  }

  // Reset input so same file can be re-selected
  event.target.value = '';
  assistantRenderFilePreview();
}

function assistantRemoveFile(index) {
  assistantPendingFiles.splice(index, 1);
  assistantRenderFilePreview();
}

function assistantRenderFilePreview() {
  var preview = document.getElementById('assistant-file-preview');
  if (!preview) return;

  if (assistantPendingFiles.length === 0) {
    preview.style.display = 'none';
    preview.innerHTML = '';
    return;
  }

  var html = '';
  for (var i = 0; i < assistantPendingFiles.length; i++) {
    var f = assistantPendingFiles[i];
    var icon = 'fa-file-text-o';
    var sizeStr = formatFileSize(f.size);
    if (/\.(xlsx|xls)$/i.test(f.name)) icon = 'fa-file-excel-o';
    else if (/\.csv$/i.test(f.name)) icon = 'fa-table';
    else if (/\.(json|xml|yaml|yml)$/i.test(f.name)) icon = 'fa-code';
    else if (/\.log$/i.test(f.name)) icon = 'fa-list-alt';

    html += '<span class="fa ' + icon + ' file-icon"></span>';
    html += '<span class="file-name">' + escapeHtml(f.name) + '</span>';
    html += '<span class="file-size">' + sizeStr + '</span>';
    html += '<span class="file-remove fa fa-times" onclick="assistantRemoveFile(' + i + ')" title="移除"></span>';
    if (i < assistantPendingFiles.length - 1) html += '<br>';
  }
  preview.innerHTML = html;
  preview.style.display = 'flex';
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function assistantReadFilesWithList(fileList) {
  return Promise.all(fileList.map(function(file) {
    return new Promise(function(resolve) {
      // Handle Excel files differently - send filename only
      if (/\.(xlsx|xls)$/i.test(file.name)) {
        resolve({
          name: file.name,
          size: file.size,
          type: file.type,
          content: '[二进制文件: ' + file.name + ' (Excel格式，AI将基于文件名推断内容)]',
          isBinary: true
        });
        return;
      }

      var reader = new FileReader();
      reader.onload = function(e) {
        var content = e.target.result;
        if (content.length > 5000000) {
          content = content.substring(0, 5000000) + '\n\n... [文件过长，已截断，显示前 5000000 字符]';
        }
        resolve({
          name: file.name,
          size: file.size,
          type: file.type,
          content: content,
          isBinary: false
        });
      };
      reader.onerror = function() {
        resolve({
          name: file.name,
          size: file.size,
          type: file.type,
          content: '[文件读取失败: ' + file.name + ']',
          isBinary: false
        });
      };
      reader.readAsText(file);
    });
  }));
}

function assistantReadFiles() {
  return assistantReadFilesWithList(assistantPendingFiles);
}

// Expose to global scope
window.assistantHandleFile = assistantHandleFile;
window.assistantRemoveFile = assistantRemoveFile;

// ---- 聊天功能 ----

function assistantSend() {
  if (assistantLoading) return;
  var input = document.getElementById('assistant-input');
  if (!input) return;
  var msg = input.value.trim();
  var hasFiles = assistantPendingFiles.length > 0;

  // Need at least text or files
  if (!msg && !hasFiles) return;

  // 高风险操作确认
  if (msg && isHighRisk(msg)) {
    var confirmed = confirm(
      '⚠️ 高风险操作警告\n\n' +
      '你的请求包含高风险操作（如 sudo、删除数据库、强制推送等）。\n\n' +
      '请确认你了解此操作的后果。AI助手不对系统级操作负责。\n\n' +
      '点击"确定"继续发送，点击"取消"放弃。'
    );
    if (!confirmed) return;
  }

  // Build display message with file info
  var displayMsg = msg;
  if (hasFiles) {
    var fileNames = [];
    for (var fi = 0; fi < assistantPendingFiles.length; fi++) {
      fileNames.push(assistantPendingFiles[fi].name);
    }
    if (displayMsg) {
      displayMsg = displayMsg + '\n\n📎 附件: ' + fileNames.join(', ');
    } else {
      displayMsg = '📎 请分析以下文件: ' + fileNames.join(', ');
    }
  }

  assistantAddMessage('user', displayMsg);
  input.value = '';
  input.style.height = 'auto';

  // Save pending files and clear (for async operations)
  var filesToSend = assistantPendingFiles.slice();
  assistantPendingFiles = [];
  assistantRenderFilePreview();

  // Collect page context
  var pageContext = {};
  try {
    var tables = document.querySelectorAll('#assistant-main-wrapper table');
    if (tables.length > 0) {
      var tableData = [];
      for (var i = 0; i < Math.min(tables.length, 3); i++) {
        var rows = tables[i].querySelectorAll('tr');
        var tdata = [];
        for (var j = 0; j < Math.min(rows.length, 20); j++) {
          var cells = rows[j].querySelectorAll('td, th');
          var row = [];
          for (var k = 0; k < cells.length; k++) {
            row.push(cells[k].textContent.trim());
          }
          if (row.length > 0) tdata.push(row);
        }
        if (tdata.length > 0) tableData.push(tdata);
      }
      if (tableData.length > 0) pageContext.tables = tableData;
    }

    var forms = document.querySelectorAll('#assistant-main-wrapper form');
    if (forms.length > 0) {
      var formInfo = [];
      for (var i = 0; i < Math.min(forms.length, 2); i++) {
        var inputs = forms[i].querySelectorAll('input:not([type="hidden"]), select, textarea');
        var finfo = {};
        for (var j = 0; j < inputs.length; j++) {
          var inp = inputs[j];
          if (inp.name && inp.value) {
            finfo[inp.name] = inp.value;
          }
        }
        if (Object.keys(finfo).length > 0) formInfo.push(finfo);
      }
      if (formInfo.length > 0) pageContext.forms = formInfo;
    }
  } catch (e) {}

  try {
    var crumbs = document.querySelectorAll('#assistant-main-wrapper #breadcrumbs .breadcrumb-item');
    var crumbTexts = [];
    for (var i = 0; i < crumbs.length; i++) {
      crumbTexts.push(crumbs[i].textContent.trim());
    }
    if (crumbTexts.length > 0) pageContext.breadcrumbs = crumbTexts;
  } catch (e) {}

  assistantHistory.push({role: 'user', content: displayMsg});
  assistantArchiveCurrentSession();
  saveState();
  assistantLoading = true;
  assistantShowLoading(true, '正在准备请求...');
  assistantToggleButtons(true);

  // Create AbortController for this request
  assistantAbortController = new AbortController();

  var sendPayload = function(fileContents) {
    var payload = {
      message: msg,
      page_url: window.location.href,
      page_title: document.title,
      context_data: JSON.stringify(pageContext),
      history: assistantHistory.slice(-20)
    };

    if (fileContents && fileContents.length > 0) {
      payload.files = fileContents;
    }

    assistantShowLoading(true, '正在思考...');
    assistantStreamChat(payload).then(function() {
      assistantResetLoading();
    }).catch(function(err) {
      assistantResetLoading();
      if (err && (err.name === 'AbortError' || err.aborted)) {
        assistantAddMessage('system', '⏹ 已停止生成');
      } else {
        assistantAddMessage('system', assistantFormatChatError(err));
      }
    });
  };

  if (filesToSend.length > 0) {
    assistantReadFilesWithList(filesToSend).then(sendPayload).catch(function(err) {
      sendPayload([{name: 'error', content: '文件读取失败: ' + err.message}]);
    });
  } else {
    sendPayload(null);
  }
}

function assistantStop() {
  if (!assistantLoading) return;

  // Notify backend first, then abort local stream reading.
  if (assistantRequestId) {
    var rid = assistantRequestId;
    assistantRequestId = null;
    fetch(url_prefix + '/assistant/chat/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': assistantGetCookie('csrftoken'),
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({action: 'stop', request_id: rid})
    }).catch(function() {});
  }

  if (assistantAbortController) {
    assistantAbortController.abort();
    assistantAbortController = null;
  }

  assistantResetLoading();  // 立即复位到发送态，不依赖 fetch 回调
}

function assistantToggleButtons(isLoading) {
  var sendBtn = document.getElementById('assistant-send');
  var stopBtn = document.getElementById('assistant-stop');
  if (sendBtn) sendBtn.style.display = isLoading ? 'none' : 'flex';
  if (stopBtn) stopBtn.style.display = isLoading ? 'flex' : 'none';
}

function assistantAddMessage(role, content) {
  var container = document.getElementById('assistant-messages');
  if (!container) return;
  assistantAddMessageToContainer(container, role, content);
  container.scrollTop = container.scrollHeight;
}

function assistantAddMessageToContainer(container, role, content) {
  var div = document.createElement('div');
  div.className = 'assistant-message assistant-message-' + role;

  var contentDiv = document.createElement('div');
  contentDiv.className = 'assistant-message-content';
  contentDiv.innerHTML = assistantFormatMessage(content);

  div.appendChild(contentDiv);
  container.appendChild(div);
}

function assistantFormatMessage(text) {
  // First escape HTML
  var escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Convert URLs to clickable links (before other formatting)
  // Match http/https URLs and internal paths like /scenario3/output/...
  escaped = escaped.replace(
    /(https?:\/\/[^\s<>"]+[^\s<>".,;:!?)}\]])/g,
    '<a href="$1" class="assistant-link assistant-link-external" target="_blank" rel="noopener">$1</a>'
  );
  // Match internal absolute paths (like /scenario3/output/buffer/)
  escaped = escaped.replace(
    /(^|[\s(])(\/(?:scenario\d+|data|execute|output|input|forecast)\/[^\s<>"]*[^\s<>".,;:!?)}\]])/gm,
    '$1<a href="$2" class="assistant-link assistant-link-internal">$2</a>'
  );

  // Code blocks
  escaped = escaped.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  // Inline code
  escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Line breaks
  escaped = escaped.replace(/\n/g, '<br>');

  return escaped;
}

// ===== 流式 SSE 读取（思考过程 + 正文） =====

function assistantParseSseFrame(rawFrame) {
  rawFrame = rawFrame.replace(/\r/g, '');
  var event = null;
  var dataLines = [];
  var lines = rawFrame.split('\n');
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    if (line.indexOf('event: ') === 0) event = line.slice(7);
    else if (line.indexOf('data: ') === 0) dataLines.push(line.slice(6));
    else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5));
  }
  if (!event) return null;
  return {event: event, data: dataLines.join('\n')};
}

function assistantFormatChatError(err) {
  var msg = err && err.message ? err.message : String(err || '未知错误');
  var networkLike = err && (
    err.name === 'TypeError'
    || /failed to fetch|network\s*error|load failed|networkerror/i.test(msg)
  );
  if (networkLike) {
    return '网络连接失败：请确认 8001 服务正在运行，然后刷新页面重试。'
      + ' 如果服务刚重启，请重新登录后再发送。';
  }
  if (err && err.backend) {
    return '后端返回错误：' + msg;
  }
  return '请求失败：' + msg;
}

// ===== 工具卡片（agentic）=====

// 智能滚动：仅当用户在底部附近时才跟随，避免打断查看上方内容/表格
function assistantAutoScroll() {
  var c = document.getElementById('assistant-messages');
  if (!c) return;
  if (c.scrollHeight - c.scrollTop - c.clientHeight < 100) {
    c.scrollTop = c.scrollHeight;
  }
}

function assistantEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// 把工具返回结果渲染成 HTML：有 rows 数组→表格；否则→JSON 代码块。
function assistantRenderResult(result) {
  if (!result) return '';
  if (result.error) {
    return '<div class="assistant-tool-reject-msg">错误：' + assistantEscape(result.error) + '</div>';
  }
  if (result.rows && result.rows.length) {
    var keys = [];
    var first = result.rows[0];
    for (var k in first) { if (first.hasOwnProperty(k)) keys.push(k); }
    var head = '<thead><tr>' + keys.map(function(k) {
      return '<th>' + assistantEscape(k) + '</th>';
    }).join('') + '</tr></thead>';
    var body = '<tbody>';
    for (var i = 0; i < result.rows.length; i++) {
      var r = result.rows[i];
      body += '<tr>' + keys.map(function(k) {
        var v = r[k];
        return '<td>' + assistantEscape(v === null || v === undefined ? '' : v) + '</td>';
      }).join('') + '</tr>';
    }
    body += '</tbody>';
    var total = (result.count != null ? result.count : result.rows.length);
    var summary = '<div class="assistant-tool-summary">共 ' + assistantEscape(total)
      + ' 条，显示 ' + result.rows.length + ' 条</div>';
    return summary + '<div class="assistant-tool-table-wrap"><table class="assistant-tool-table">'
      + head + body + '</table></div>';
  }
  return '<pre class="assistant-tool-pre">' + assistantEscape(JSON.stringify(result, null, 2)) + '</pre>';
}

function assistantMakeToolCard(id, name) {
  var container = document.getElementById('assistant-messages');
  if (!container) return null;
  assistantShowLoading(false);
  var card = document.createElement('div');
  card.className = 'assistant-message assistant-tool-card';
  card.setAttribute('data-tool-id', id || '');
  card.innerHTML =
    '<div class="assistant-tool-card-inner">' +
      '<div class="assistant-tool-header">' +
        '<span class="assistant-tool-icon">🔧</span>' +
        '<span class="assistant-tool-name"></span>' +
        '<span class="assistant-tool-status">调用中…</span>' +
      '</div>' +
      '<div class="assistant-tool-body"></div>' +
    '</div>';
  card.querySelector('.assistant-tool-name').textContent = name || '';
  container.appendChild(card);
  return card;
}

function assistantToolRunning(card) {
  if (!card) return;
  var st = card.querySelector('.assistant-tool-status');
  if (st) st.textContent = '执行中…';
  card.classList.add('assistant-tool-running');
}

function assistantToolProgress(card, progress) {
  if (!card) return;
  var st = card.querySelector('.assistant-tool-status');
  var elapsed = progress && progress.elapsed_seconds != null ? progress.elapsed_seconds : null;
  if (st) {
    st.textContent = elapsed == null ? '仍在执行…' : '仍在执行… ' + elapsed + ' 秒';
  }
  card.classList.add('assistant-tool-running');
  assistantAutoScroll();
}

function assistantToolResult(card, bodyHtml, rejected) {
  if (!card) return;
  var st = card.querySelector('.assistant-tool-status');
  var body = card.querySelector('.assistant-tool-body');
  if (st) st.textContent = rejected ? '已拒绝' : '完成';
  card.classList.remove('assistant-tool-running');
  card.classList.add(rejected ? 'assistant-tool-rejected' : 'assistant-tool-done');
  if (body) body.innerHTML = bodyHtml;
  assistantAutoScroll();
}

function assistantToolConfirm(card, requestId, toolUseId, name, input) {
  if (!card) return;
  card.classList.add('assistant-tool-write');
  var st = card.querySelector('.assistant-tool-status');
  if (st) st.textContent = '等待确认';
  var body = card.querySelector('.assistant-tool-body');
  body.innerHTML =
    '<div class="assistant-tool-args">参数：<pre class="assistant-tool-pre">' +
      assistantEscape(JSON.stringify(input, null, 2)) + '</pre></div>' +
    '<div class="assistant-tool-warn">⚠ 这是写操作，会修改数据。确认执行？</div>' +
    '<div class="assistant-tool-actions">' +
      '<button class="assistant-tool-btn assistant-tool-btn-ok">✓ 确认执行</button>' +
      '<button class="assistant-tool-btn assistant-tool-btn-no">✗ 拒绝</button>' +
    '</div>';
  body.querySelector('.assistant-tool-btn-ok').onclick = function() {
    assistantSendConfirm(requestId, toolUseId, true, card);
  };
  body.querySelector('.assistant-tool-btn-no').onclick = function() {
    assistantSendConfirm(requestId, toolUseId, false, card);
  };
}

function assistantSendConfirm(requestId, toolUseId, approved, card) {
  if (card) {
    var st = card.querySelector('.assistant-tool-status');
    if (st) st.textContent = approved ? '已确认，执行中…' : '已拒绝';
    var actions = card.querySelector('.assistant-tool-actions');
    if (actions) actions.remove();
  }
  fetch(url_prefix + '/assistant/confirm/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': assistantGetCookie('csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: JSON.stringify({request_id: requestId, tool_use_id: toolUseId, approved: approved})
  }).catch(function() {});
}

function assistantEnsureAnswerBubble() {
  var container = document.getElementById('assistant-messages');
  if (!container) return null;
  var div = document.createElement('div');
  div.className = 'assistant-message assistant-message-assistant';
  var contentDiv = document.createElement('div');
  contentDiv.className = 'assistant-message-content';
  div.appendChild(contentDiv);
  container.appendChild(div);
  assistantAutoScroll();
  return contentDiv;
}

function assistantUpdateBubble(contentEl, text) {
  if (!contentEl) return;
  contentEl.innerHTML = assistantFormatMessage(text);
  assistantAutoScroll();
}

function assistantResetLoading() {
  assistantLoading = false;
  assistantAbortController = null;
  assistantRequestId = null;
  assistantShowLoading(false);
  assistantToggleButtons(false);
}

// 流式调用 chat 接口（agentic），逐帧解析 SSE：
// text(逐字) / tool_call / tool_running / tool_result / tool_confirm / done
// Promise 在 done 时 resolve；error 或网络异常时 reject。
function assistantStreamChat(payload) {
  var options = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': assistantGetCookie('csrftoken'),
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: JSON.stringify(payload)
  };
  if (assistantAbortController) {
    options.signal = assistantAbortController.signal;
  }
	  return fetch(url_prefix + '/assistant/chat/', options).then(function(resp) {
	    if (!resp.ok || !resp.body) {
	      return resp.text().then(function(t) {
	        var msg = t || '';
	        try {
	          var parsed = JSON.parse(t);
	          msg = parsed.error || parsed.detail || t;
	        } catch (e) {}
	        var err = new Error('HTTP ' + resp.status + (msg ? '：' + msg.slice(0, 300) : ''));
	        err.backend = true;
	        err.status = resp.status;
	        throw err;
	      });
	    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buf = '';
    var answerEl = null;     // 正文气泡 content
    var answerText = '';     // 累积正文（逐字）
    var sawText = false;
    var finished = false;
    var toolCards = {};      // tool_use_id → 卡片 DOM

    function pump() {
      return reader.read().then(function(result) {
        if (finished) return;
        if (result.done) {
          if (!sawText && answerText === '' && Object.keys(toolCards).length === 0) {
            throw new Error('回复流意外结束');
          }
          return;
        }
        buf += decoder.decode(result.value, {stream: true});
        var idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          var rawFrame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          var evt = assistantParseSseFrame(rawFrame);
          if (!evt) continue;

          if (evt.event === 'start') {
            try { assistantRequestId = JSON.parse(evt.data).request_id || null; } catch (e) {}
          } else if (evt.event === 'text') {
            answerText += evt.data;
            if (!sawText) { sawText = true; assistantShowLoading(false); }
            if (!answerEl) answerEl = assistantEnsureAnswerBubble();
            assistantUpdateBubble(answerEl, answerText);
          } else if (evt.event === 'tool_call') {
            var tc = {};
            try { tc = JSON.parse(evt.data); } catch (e) {}
            var card = assistantMakeToolCard(tc.id, tc.name);
            if (tc.id) toolCards[tc.id] = card;
            // 关键：下一段文字新建独立气泡（排在卡片之后），避免累积到上方气泡把卡片往下挤
            answerEl = null;
            answerText = '';
	          } else if (evt.event === 'tool_running') {
	            var tr = {};
	            try { tr = JSON.parse(evt.data); } catch (e) {}
	            assistantToolRunning(toolCards[tr.id]);
	          } else if (evt.event === 'tool_progress') {
	            var tp = {};
	            try { tp = JSON.parse(evt.data); } catch (e) {}
	            assistantToolProgress(toolCards[tp.id], tp);
	          } else if (evt.event === 'tool_confirm') {
            var cf = {};
            try { cf = JSON.parse(evt.data); } catch (e) {}
            assistantToolConfirm(toolCards[cf.tool_use_id], cf.request_id, cf.tool_use_id, cf.name, cf.input);
          } else if (evt.event === 'tool_result') {
            var rs = {};
            try { rs = JSON.parse(evt.data); } catch (e) {}
            var rejected = rs.status === 'rejected';
            var bodyHtml = rejected
              ? '<div class="assistant-tool-reject-msg">' + assistantEscape(rs.reason || '已拒绝') + '</div>'
              : assistantRenderResult(rs.result);
            assistantToolResult(toolCards[rs.id], bodyHtml, rejected);
          } else if (evt.event === 'done') {
            finished = true;
            var finalText = answerText;
            if (sawText && answerEl) {
              assistantUpdateBubble(answerEl, finalText);
            } else if (finalText) {
              assistantShowLoading(false);
              assistantAddMessage('assistant', finalText);
            }
            assistantHistory.push({role: 'assistant', content: finalText});
            assistantArchiveCurrentSession();
            saveState();
            return;
	          } else if (evt.event === 'error') {
	            finished = true;
	            var errMsg = evt.data;
	            try { errMsg = JSON.parse(evt.data).error || evt.data; } catch (e) {}
	            var streamErr = new Error(errMsg);
	            streamErr.backend = true;
	            throw streamErr;
	          }
        }
        return pump();
      });
    }
    return pump();
  });
}

function assistantShowLoading(show, text) {
  var container = document.getElementById('assistant-messages');
  var existing = document.getElementById('assistant-loading');
  if (show) {
    if (!existing) {
      var div = document.createElement('div');
      div.id = 'assistant-loading';
      div.className = 'assistant-message assistant-message-system';
      div.innerHTML = '<div class="assistant-message-content"><span class="assistant-loading-text"></span><span class="assistant-loading-dots"><span class="assistant-dot"></span><span class="assistant-dot"></span><span class="assistant-dot"></span></span></div>';
      container.appendChild(div);
      assistantAutoScroll();
    }
    var textNode = document.querySelector('#assistant-loading .assistant-loading-text');
    if (textNode) {
      textNode.textContent = text || '正在生成回复';
    }
  } else {
    if (existing) existing.remove();
  }
}

function assistantKeyDown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    assistantSend();
  } else if (event.key === 'Escape' && assistantLoading) {
    event.preventDefault();
    assistantStop();
  }
}

function assistantAskSuggestion(question) {
  if (!question || assistantLoading) return;
  var input = document.getElementById('assistant-input');
  if (!input) return;
  input.value = question;
  assistantSend();
}

function assistantGetCookie(name) {
  var value = '; ' + document.cookie;
  var parts = value.split('; ' + name + '=');
  if (parts.length === 2) return parts.pop().split(';').shift();
  return '';
}

function assistantIsSameOriginUrl(href) {
  try {
    return new URL(href, window.location.href).origin === window.location.origin;
  } catch (e) {
    return false;
  }
}

function assistantConfirm() {
  var overlay = document.getElementById('assistant-confirm');
  if (overlay) {
    overlay.classList.add('assistant-confirmed');
    localStorage.setItem('assistant_confirmed', '1');
  }
  saveState();
  var input = document.getElementById('assistant-input');
  if (input) input.focus();
}

// Expose to global scope for onclick handlers
window.assistantSend = assistantSend;
window.assistantStop = assistantStop;
window.assistantNewSession = assistantNewSession;
window.assistantToggleHistoryPanel = assistantToggleHistoryPanel;
window.assistantKeyDown = assistantKeyDown;
window.assistantConfirm = assistantConfirm;
window.assistantAskSuggestion = assistantAskSuggestion;

// Initialize when DOM is ready
function bindInputEvents() {
  var input = document.getElementById('assistant-input');
  if (input) {
    input.addEventListener('input', function() {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    });
  }

  // Event delegation: handle clicks on links in chat messages
  var msgContainer = document.getElementById('assistant-messages');
  if (msgContainer) {
    msgContainer.addEventListener('click', function(e) {
      var link = e.target.closest('.assistant-link');
      if (!link) return;

      var href = link.getAttribute('href');
      if (!href) return;
      var isOwnUiLink = link.classList.contains('assistant-link-internal') || assistantIsSameOriginUrl(href);

      if (isOwnUiLink) {
        e.preventDefault();
        saveState();
        window.location.href = href;
      }
    });
  }

  var historyToggle = document.getElementById('assistant-history-toggle');
  if (historyToggle) {
    historyToggle.addEventListener('click', function() {
      assistantToggleHistoryPanel();
    });
  }

  var newSessionBtn = document.getElementById('assistant-new-session');
  if (newSessionBtn) {
    newSessionBtn.addEventListener('click', assistantNewSession);
  }

  var historyList = document.getElementById('assistant-history-list');
  if (historyList) {
    historyList.addEventListener('click', function(e) {
      var item = e.target.closest('.assistant-history-item');
      if (!item) return;
      assistantLoadSession(item.getAttribute('data-session-id'));
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function() {
    initAssistantLayout();
    bindInputEvents();
  });
} else {
  initAssistantLayout();
  bindInputEvents();
}

})();
