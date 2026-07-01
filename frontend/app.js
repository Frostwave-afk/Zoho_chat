/* ─── State ──────────────────────────────────────────────────────────────── */
const state = {
  gmailConnected: false,
  zohoConnected:  false,
};

/* ─── DOM refs ───────────────────────────────────────────────────────────── */
const setupScreen      = document.getElementById('setup-screen');
const chatScreen       = document.getElementById('chat-screen');
const messagesList     = document.getElementById('messages-list');
const messagesContainer= document.getElementById('messages-container');
const chatInput        = document.getElementById('chat-input');
const sendBtn          = document.getElementById('send-btn');
const logoutBtn        = document.getElementById('logout-btn');
const headerStatus     = document.getElementById('header-status');
const statusDot        = headerStatus.querySelector('.status-dot');

/* ─── Utilities ──────────────────────────────────────────────────────────── */
function formatTime() {
  return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scrollToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function setStatus(text, type = 'idle') {
  statusDot.className = 'status-dot' + (type === 'loading' ? ' loading' : ' active');
  headerStatus.lastChild.textContent = ' ' + text;
}

/* ─── Auth status check ──────────────────────────────────────────────────── */
async function checkAuthStatus() {
  try {
    const res  = await fetch('/auth/status');
    const data = await res.json();
    state.gmailConnected = data.gmail;
    state.zohoConnected  = data.zoho;
    updateSetupUI();
    if (data.gmail && data.zoho) {
      showChatScreen();
    } else {
      showSetupScreen();
    }
  } catch (e) {
    console.error('Auth check failed:', e);
    showSetupScreen();
  }
}

function updateSetupUI() {
  // Gmail step
  const gmailStep   = document.getElementById('step-gmail');
  const gmailStatus = document.getElementById('gmail-status');
  const gmailBtn    = document.getElementById('connect-gmail-btn');
  const gmailCheck  = document.getElementById('gmail-check');
  if (state.gmailConnected) {
    gmailStep.classList.add('connected');
    gmailStatus.textContent = 'Connected ✓';
    gmailBtn.classList.add('done');
    gmailBtn.textContent = 'Connected';
    gmailCheck.classList.remove('hidden');
  }
  // Zoho step
  const zohoStep   = document.getElementById('step-zoho');
  const zohoStatus = document.getElementById('zoho-status');
  const zohoBtn    = document.getElementById('connect-zoho-btn');
  const zohoCheck  = document.getElementById('zoho-check');
  if (state.zohoConnected) {
    zohoStep.classList.add('connected');
    zohoStatus.textContent = 'Connected ✓';
    zohoBtn.classList.add('done');
    zohoBtn.textContent = 'Connected';
    zohoCheck.classList.remove('hidden');
  }
  // Hint
  const hint = document.getElementById('setup-hint');
  if (state.gmailConnected && state.zohoConnected) {
    hint.textContent = 'Both accounts connected — launching chat…';
    hint.classList.add('ready');
  } else if (state.gmailConnected || state.zohoConnected) {
    hint.textContent = 'Almost there — connect the remaining account.';
  }

  // Sidebar tags
  document.getElementById('nav-gmail-tag').className =
    'account-tag' + (state.gmailConnected ? '' : ' offline');
  document.getElementById('nav-zoho-tag').className =
    'account-tag' + (state.zohoConnected ? '' : ' offline');
}

function showSetupScreen() {
  setupScreen.classList.remove('hidden');
  chatScreen.classList.add('hidden');
}

function showChatScreen() {
  setupScreen.classList.add('hidden');
  chatScreen.classList.remove('hidden');
  updateSetupUI();
}

/* ─── Handle OAuth redirects ─────────────────────────────────────────────── */
const params = new URLSearchParams(window.location.search);
if (params.get('connected')) {
  // Clean URL without reloading
  window.history.replaceState({}, '', '/');
}

/* ─── Message rendering ──────────────────────────────────────────────────── */
function appendUserMessage(text) {
  const el = document.createElement('div');
  el.className = 'message user-message';
  el.innerHTML = `
    <div class="message-avatar">👤</div>
    <div class="message-body">
      <div class="message-bubble">${escapeHtml(text).replace(/\n/g, '<br>')}</div>
      <span class="message-time">${formatTime()}</span>
    </div>`;
  messagesList.appendChild(el);
  scrollToBottom();
}

/* ─── Live status bubble (replaces typing dots) ──────────────────────────── */
function showStatusBubble(initialText) {
  const el = document.createElement('div');
  el.className = 'message agent-message status-bubble-msg';
  el.id = 'status-bubble';
  el.innerHTML = `
    <div class="message-avatar">⚡</div>
    <div class="message-body">
      <div class="message-bubble status-bubble">
        <span class="status-spinner"></span>
        <span class="status-bubble-text">${escapeHtml(initialText)}</span>
      </div>
    </div>`;
  messagesList.appendChild(el);
  scrollToBottom();
  return el;
}

function updateStatusBubble(text) {
  const el = document.getElementById('status-bubble');
  if (el) el.querySelector('.status-bubble-text').textContent = text;
  scrollToBottom();
}

function removeStatusBubble() {
  document.getElementById('status-bubble')?.remove();
}

// Keep for backward compat (approve flow still uses these)
function appendTypingIndicator() { showStatusBubble('Working…'); }
function removeTypingIndicator() { removeStatusBubble(); }


function renderMarkdown(text) {
  // Preserve links as tokens while escaping the rest of the message. This keeps
  // invoice/customer data from being interpreted as HTML.
  const links = [];
  const withLinkTokens = String(text ?? '').replace(
    /\[([^\]]+)]\((https?:\/\/[^\s)]+)\)/g,
    (_, label, url) => {
      const token = `__CHAT_LINK_${links.length}__`;
      links.push({ label, url });
      return token;
    },
  );

  let html = escapeHtml(withLinkTokens)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^• (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .replace(/\n/g, '<br>');

  links.forEach(({ label, url }, index) => {
    const link = `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="chat-action-link">${escapeHtml(label)} <span aria-hidden="true">→</span></a>`;
    html = html.replace(`__CHAT_LINK_${index}__`, link);
  });

  return html;
}

function appendAgentMessage(text, extra = null) {
  const el = document.createElement('div');
  el.className = 'message agent-message';
  el.innerHTML = `
    <div class="message-avatar">⚡</div>
    <div class="message-body">
      <div class="message-bubble">${renderMarkdown(text)}</div>
      <span class="message-time">${formatTime()}</span>
    </div>`;

  messagesList.appendChild(el);

  if (extra) {
    messagesList.appendChild(extra);
  }
  scrollToBottom();
}

/* ─── Draft card ─────────────────────────────────────────────────────────── */
function buildDraftCard(draft) {
  const d    = draft.data;
  const isNew = draft.is_new_contact;
  const card = document.createElement('div');
  card.className = 'draft-card' + (isNew ? ' new-contact-card' : '');
  card.id = `draft-${draft.draft_id}`;

  const criticalMissing = (d.missing_fields || []).filter(f => f === 'amount' || f === 'task_description');
  const missing = (d.confidence === 'low' && criticalMissing.length)
    ? `<div class="draft-missing">⚠ Some details are unclear — please review: ${criticalMissing.join(', ')}</div>`
    : '';

  const badge = isNew
    ? `<span class="draft-badge new-contact-badge">👤 New Contact + Invoice</span>`
    : `<span class="draft-badge">Needs Approval</span>`;

  const emailField = isNew ? `
      <div class="draft-field">
        <label>Email <span class="field-hint">(for new Zoho contact)</span></label>
        <input type="email" id="draft-email-${draft.draft_id}" value="${escapeHtml(d.client_email || '')}" placeholder="client@example.com" />
      </div>` : '';

  const btnLabel    = isNew ? '👤 Create Contact &amp; Invoice' : '✓ Create Invoice';
  const btnSendLabel = isNew ? '👤 Create Contact &amp; Send Invoice' : '📧 Create &amp; Send Invoice';

  card.innerHTML = `
    <div class="draft-header">
      ${badge}
      <span class="draft-subject">${escapeHtml(draft.email_subject || 'Email')}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client Name</label>
        <input type="text" id="draft-client-${draft.draft_id}" value="${escapeHtml(d.client_name || '')}" placeholder="Client name" />
      </div>
      ${emailField}
      <div class="draft-field">
        <label>Amount (${d.currency || 'USD'})</label>
        <input type="number" id="draft-amount-${draft.draft_id}" value="${d.amount ?? ''}" placeholder="e.g. 1200" />
      </div>
      <div class="draft-field">
        <label>Item Name</label>
        <input type="text" id="draft-item-${draft.draft_id}" value="${escapeHtml(d.item_name || '')}" placeholder="e.g. Android App Development" />
      </div>
      <div class="draft-field full-width">
        <label>Description</label>
        <input type="text" id="draft-desc-${draft.draft_id}" value="${escapeHtml(d.task_description || '')}" placeholder="Full description of the work" />
      </div>
    </div>
    ${missing}
    <div class="draft-actions">
      <button class="btn-approve ${isNew ? 'btn-new-contact' : ''}" id="approve-${draft.draft_id}" onclick="approveDraft('${draft.draft_id}', false)">
        ${btnLabel}
      </button>
      <button class="btn-approve-send" id="approve-send-${draft.draft_id}" onclick="approveDraft('${draft.draft_id}', true)">
        ${btnSendLabel}
      </button>
      <button class="btn-decline" onclick="declineDraft('${draft.draft_id}')">
        Dismiss
      </button>
    </div>`;
  return card;
}

/* ─── Invoice created card ───────────────────────────────────────────────── */
function buildInvoiceCard(inv) {
  const card = document.createElement('div');
  card.className = 'invoice-card';
  card.id = `invoice-card-${inv.zoho_invoice_id}`;
  const viewBtn = inv.invoice_url
    ? `<a href="${inv.invoice_url}" target="_blank" class="btn-view-invoice">View →</a>`
    : '';
  const sendBtn = inv.email_sent
    ? `<span class="invoice-sent-badge">📧 Sent</span>`
    : `<button class="btn-send-invoice" onclick="sendInvoice('${inv.zoho_invoice_id}', '${escapeHtml(inv.client_name)}', this)">Send to Client</button>`;
  card.innerHTML = `
    <div class="invoice-icon">🧾</div>
    <div class="invoice-info">
      <div class="invoice-number">Invoice #${escapeHtml(inv.invoice_number || '—')}</div>
      <div class="invoice-detail">${escapeHtml(inv.client_name)} · ${inv.currency} ${Number(inv.amount).toLocaleString()}</div>
    </div>
    <div class="invoice-card-actions">
      ${viewBtn}
      ${sendBtn}
    </div>`;
  return card;
}

function buildManualInvoiceCard(draft) {
  const card = document.createElement('div');
  card.className = 'draft-card' + (draft.is_new_contact ? ' new-contact-card' : '');
  card.id = `manual-draft-${draft.draft_id}`;

  const itemsHtml = draft.line_items.map((item, index) => `
    <div class="draft-field full-width">
      <label>Item ${index + 1}</label>
      <div>${escapeHtml(item.item_name)} — ${escapeHtml(draft.currency)} ${Number(item.amount).toLocaleString()}</div>
      <div class="draft-inline-help">${escapeHtml(item.task_description)}</div>
    </div>
  `).join('');

  const total = draft.line_items.reduce((sum, item) => sum + Number(item.amount || 0), 0);
  const badge = draft.is_new_contact
    ? `<span class="draft-badge new-contact-badge">👤 New Contact + Invoice</span>`
    : `<span class="draft-badge">Manual Invoice Draft</span>`;

  card.innerHTML = `
    <div class="draft-header">
      ${badge}
      <span class="draft-subject">${escapeHtml(draft.client_name)}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client Name</label>
        <div>${escapeHtml(draft.client_name)}</div>
      </div>
      <div class="draft-field">
        <label>Email</label>
        <div>${escapeHtml(draft.client_email || '—')}</div>
      </div>
      ${itemsHtml}
      <div class="draft-field">
        <label>Total</label>
        <div>${escapeHtml(draft.currency)} ${Number(total).toLocaleString()}</div>
      </div>
    </div>
    <div class="draft-actions">
      <button class="btn-approve ${draft.is_new_contact ? 'btn-new-contact' : ''}" id="manual-approve-${draft.draft_id}" onclick="approveManualInvoice('${draft.draft_id}', false)">
        ${draft.is_new_contact ? '👤 Create Contact & Invoice' : '✓ Create Invoice'}
      </button>
      <button class="btn-approve-send" id="manual-approve-send-${draft.draft_id}" onclick="approveManualInvoice('${draft.draft_id}', true)">
        ${draft.is_new_contact ? '👤 Create Contact & Send Invoice' : '📧 Create & Send Invoice'}
      </button>
    </div>`;
  return card;
}

function appendExtraCards(extra, data) {
  if (data.manual_invoice_draft) {
    extra.appendChild(buildManualInvoiceCard(data.manual_invoice_draft));
    return;
  }
  if (data.batch_draft) {
    extra.appendChild(buildBatchCard(data.batch_draft));
    return;
  }
  if (data.drafts?.length) {
    data.drafts.forEach(d => extra.appendChild(buildDraftCard(d)));
    return;
  }
  if (data.invoices_created?.length) {
    data.invoices_created.forEach(inv => extra.appendChild(buildInvoiceCard(inv)));
  }
}

/* ─── Send message (SSE streaming) ──────────────────────────────────────── */
async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text) return;

  chatInput.value = '';
  chatInput.style.height = 'auto';
  sendBtn.disabled = true;
  setStatus('Thinking…', 'loading');

  appendUserMessage(text);
  showStatusBubble('🧠 Reading your request…');

  try {
    const res = await fetch('/chat/stream', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: text }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by \n\n
      const parts = buffer.split('\n\n');
      buffer = parts.pop();   // keep incomplete tail

      for (const part of parts) {
        const eventLine = part.split('\n').find(l => l.startsWith('event:'));
        const dataLine  = part.split('\n').find(l => l.startsWith('data:'));
        if (!dataLine) continue;

        const eventType = eventLine ? eventLine.replace('event:', '').trim() : 'message';
        let payload;
        try { payload = JSON.parse(dataLine.replace('data:', '').trim()); }
        catch { continue; }

        if (eventType === 'status') {
          updateStatusBubble(payload.text);

        } else if (eventType === 'done') {
          removeStatusBubble();
          const data  = payload;
          let   extra = null;
          if (data.manual_invoice_draft || data.batch_draft || data.drafts?.length || data.invoices_created?.length) {
            extra = document.createDocumentFragment();
            appendExtraCards(extra, data);
          }
          appendAgentMessage(data.reply, extra);
          setStatus('Ready', 'idle');

        } else if (eventType === 'error') {
          removeStatusBubble();
          appendAgentMessage(`Something went wrong — ${payload.text}`);
          setStatus('Ready', 'idle');
        }
      }
    }
  } catch (e) {
    removeStatusBubble();
    appendAgentMessage('Something went wrong — please try again.');
    setStatus('Ready', 'idle');
    console.error(e);
  } finally {
    sendBtn.disabled = false;
    chatInput.focus();
  }
}

/* ─── Approve / Decline draft ────────────────────────────────────────────── */
async function approveDraft(draftId, sendEmail = false) {
  const approveBtn  = document.getElementById(`approve-${draftId}`);
  const sendBtn     = document.getElementById(`approve-send-${draftId}`);
  const activeBtn   = sendEmail ? sendBtn : approveBtn;
  const item   = document.getElementById(`draft-item-${draftId}`)?.value?.trim();
  const desc   = document.getElementById(`draft-desc-${draftId}`)?.value?.trim();
  const amount = parseFloat(document.getElementById(`draft-amount-${draftId}`)?.value);
  const client = document.getElementById(`draft-client-${draftId}`)?.value?.trim();
  const email  = document.getElementById(`draft-email-${draftId}`)?.value?.trim();

  if (approveBtn)  approveBtn.disabled = true;
  if (sendBtn)     sendBtn.disabled    = true;
  if (activeBtn)   activeBtn.textContent = sendEmail ? 'Sending…' : 'Processing…';

  try {
    const res  = await fetch('/chat/approve', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        draft_id:         draftId,
        item_name:        item   || undefined,
        task_description: desc   || undefined,
        amount:           isNaN(amount) ? undefined : amount,
        client_name:      client || undefined,
        client_email:     email  || undefined,
        send_email:       sendEmail,
      }),
    });
    const data = await res.json();
    document.getElementById(`draft-${draftId}`)?.remove();

    let extra = null;
    if (data.invoices_created?.length) {
      extra = document.createDocumentFragment();
      data.invoices_created.forEach(inv => extra.appendChild(buildInvoiceCard(inv)));
    }
    appendAgentMessage(data.reply, extra);
    scrollToBottom();
  } catch (e) {
    if (approveBtn) { approveBtn.disabled = false; approveBtn.textContent = 'Create Invoice'; }
    if (sendBtn)    { sendBtn.disabled    = false; sendBtn.textContent    = 'Create & Send Invoice'; }
    appendAgentMessage('Failed to create invoice — please try again.');
    console.error(e);
  }
}

async function approveManualInvoice(draftId, sendEmail = false) {
  const approveBtn = document.getElementById(`manual-approve-${draftId}`);
  const sendBtn = document.getElementById(`manual-approve-send-${draftId}`);
  const activeBtn = sendEmail ? sendBtn : approveBtn;

  if (approveBtn) approveBtn.disabled = true;
  if (sendBtn) sendBtn.disabled = true;
  if (activeBtn) activeBtn.textContent = sendEmail ? 'Sending…' : 'Processing…';

  try {
    const res = await fetch('/chat/manual-approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: draftId,
        send_email: sendEmail,
      }),
    });
    const data = await res.json();
    document.getElementById(`manual-draft-${draftId}`)?.remove();

    let extra = null;
    if (data.invoices_created?.length) {
      extra = document.createDocumentFragment();
      data.invoices_created.forEach(inv => extra.appendChild(buildInvoiceCard(inv)));
    }
    appendAgentMessage(data.reply, extra);
    scrollToBottom();
  } catch (e) {
    if (approveBtn) {
      approveBtn.disabled = false;
      approveBtn.textContent = '✓ Create Invoice';
    }
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = '📧 Create & Send Invoice';
    }
    appendAgentMessage('Failed to create manual invoice — please try again.');
    console.error(e);
  }
}

/* ─── Send invoice email ─────────────────────────────────────────────────── */
async function sendInvoice(invoiceId, clientName, btnEl) {
  btnEl.disabled    = true;
  btnEl.textContent = 'Sending…';
  try {
    const res  = await fetch('/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: `send the invoice for ${clientName}` }),
    });
    const data = await res.json();
    // Replace button with a sent badge
    btnEl.outerHTML = '<span class="invoice-sent-badge">📧 Sent</span>';
    appendAgentMessage(data.reply);
    scrollToBottom();
  } catch (e) {
    btnEl.disabled    = false;
    btnEl.textContent = 'Send to Client';
    appendAgentMessage('Could not send the invoice — please try again.');
    console.error(e);
  }
}

function declineDraft(draftId) {
  const card = document.getElementById(`draft-${draftId}`);
  if (card) {
    card.style.transition = 'opacity 0.3s, transform 0.3s';
    card.style.opacity = '0';
    card.style.transform = 'scale(0.97)';
    setTimeout(() => card.remove(), 300);
  }
  appendAgentMessage('Draft dismissed. Let me know if you need anything else.');
}

/* ─── Logout ─────────────────────────────────────────────────────────────── */
logoutBtn.addEventListener('click', async () => {
  await fetch('/auth/logout', { method: 'POST' });
  state.gmailConnected = false;
  state.zohoConnected  = false;
  messagesList.innerHTML = '';
  showSetupScreen();
  // Re-create welcome message for next session
  location.reload();
});

/* ─── Input events ───────────────────────────────────────────────────────── */
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

chatInput.addEventListener('input', () => {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
});

sendBtn.addEventListener('click', sendMessage);

/* ─── Escape helper ──────────────────────────────────────────────────────── */
function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ─── Boot ───────────────────────────────────────────────────────────────── */
checkAuthStatus();

/* ─── Batch Draft Handling ────────────────────────────────────────────────── */
function buildBatchCard(batch) {
  const card = document.createElement('div');
  card.className = 'batch-card';
  card.id = `batch-${batch.batch_id}`;

  let itemsHtml = batch.items.map(item => `
    <div class="batch-item">
      <input type="checkbox" id="batch-cb-${item.item_id}" class="batch-checkbox" data-id="${item.item_id}" checked onchange="updateBatchActions('${batch.batch_id}')">
      <div class="batch-item-content">
        <div class="batch-item-title">${escapeHtml(item.data.item_name || 'Service')}</div>
        <div class="batch-item-desc">${escapeHtml(item.data.task_description || '')}</div>
        <div class="batch-item-amount">${escapeHtml(item.data.currency || 'USD')} ${item.data.amount}</div>
      </div>
    </div>
  `).join('');

  card.innerHTML = `
    <div class="batch-header">
      <span>Grouped Invoices for <strong>${escapeHtml(batch.client_name)}</strong></span>
      <label class="batch-select-all">
        <input type="checkbox" id="batch-select-all-${batch.batch_id}" checked onchange="toggleBatchSelectAll('${batch.batch_id}', this.checked)">
        Select All
      </label>
    </div>
    <div class="batch-items" id="batch-items-${batch.batch_id}">
      ${itemsHtml}
    </div>
    <div class="batch-actions">
      <button class="btn-batch-draft" id="btn-batch-draft-${batch.batch_id}" onclick="approveBatch('${batch.batch_id}', 'draft')">💾 Save as Draft(s)</button>
      <button class="btn-batch-separate" id="btn-batch-separate-${batch.batch_id}" onclick="approveBatch('${batch.batch_id}', 'separate')">📤 Send Separately</button>
      <button class="btn-batch-combine-draft" id="btn-batch-combine-draft-${batch.batch_id}" onclick="approveBatch('${batch.batch_id}', 'combine-draft')">🔗 Combine &amp; Draft</button>
      <button class="btn-batch-combine" id="btn-batch-combine-${batch.batch_id}" onclick="approveBatch('${batch.batch_id}', 'combined')">🔗📤 Combine &amp; Send</button>
    </div>
  `;

  return card;
}

function toggleBatchSelectAll(batchId, checked) {
  const container = document.getElementById(`batch-items-${batchId}`);
  if (!container) return;
  const checkboxes = container.querySelectorAll('.batch-checkbox');
  checkboxes.forEach(cb => cb.checked = checked);
  updateBatchActions(batchId);
}

function updateBatchActions(batchId) {
  const container = document.getElementById(`batch-items-${batchId}`);
  if (!container) return;
  const checkboxes = container.querySelectorAll('.batch-checkbox:checked');
  const hasSelection = checkboxes.length > 0;
  
  document.getElementById(`btn-batch-draft-${batchId}`).disabled = !hasSelection;
  document.getElementById(`btn-batch-separate-${batchId}`).disabled = !hasSelection;
  document.getElementById(`btn-batch-combine-draft-${batchId}`).disabled = !hasSelection;
  document.getElementById(`btn-batch-combine-${batchId}`).disabled = !hasSelection;
  
  const selectAll = document.getElementById(`batch-select-all-${batchId}`);
  if (selectAll) {
    selectAll.checked = checkboxes.length === container.querySelectorAll('.batch-checkbox').length;
  }
}

async function approveBatch(batchId, mode) {
  const container = document.getElementById(`batch-items-${batchId}`);
  const checkboxes = container.querySelectorAll('.batch-checkbox:checked');
  const selectedIds = Array.from(checkboxes).map(cb => cb.dataset.id);

  if (selectedIds.length === 0) return;

  const btnDraft       = document.getElementById(`btn-batch-draft-${batchId}`);
  const btnSep         = document.getElementById(`btn-batch-separate-${batchId}`);
  const btnCombDraft   = document.getElementById(`btn-batch-combine-draft-${batchId}`);
  const btnComb        = document.getElementById(`btn-batch-combine-${batchId}`);
  
  [btnDraft, btnSep, btnCombDraft, btnComb].forEach(b => { if (b) b.disabled = true; });

  if (mode === 'draft')        btnDraft.textContent     = 'Saving...';
  if (mode === 'separate')     btnSep.textContent       = 'Sending...';
  if (mode === 'combine-draft') btnCombDraft.textContent = 'Combining...';
  if (mode === 'combined')     btnComb.textContent      = 'Combining...';

  // Map frontend mode to backend mode + send_email flag
  const backendMode = (mode === 'combine-draft') ? 'combined' : (mode === 'draft' ? 'separate' : mode);
  const sendEmail   = (mode === 'separate' || mode === 'combined');

  try {
    const res = await fetch('/chat/batch-approve', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        batch_draft_id:    batchId,
        mode:              backendMode,
        selected_item_ids: selectedIds,
        send_email:        sendEmail,
      })
    });
    const data = await res.json();
    
    document.getElementById(`batch-${batchId}`)?.remove();
    
    let extra = null;
    if (data.invoices_created?.length) {
      extra = document.createDocumentFragment();
      data.invoices_created.forEach(inv => extra.appendChild(buildInvoiceCard(inv)));
    }
    appendAgentMessage(data.reply, extra);
    
  } catch (err) {
    appendAgentMessage(`Failed to process batch: ${err.message}`);
    [btnDraft, btnSep, btnCombDraft, btnComb].forEach(b => { if (b) b.disabled = false; });
    if (btnDraft)     btnDraft.textContent     = '💾 Save as Draft(s)';
    if (btnSep)       btnSep.textContent       = '📤 Send Separately';
    if (btnCombDraft) btnCombDraft.textContent = '🔗 Combine & Draft';
    if (btnComb)      btnComb.textContent      = '🔗📤 Combine & Send';
  }
}
