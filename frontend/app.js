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

function cancelManualInvoice(draftId) {
  const card = document.getElementById(`manual-draft-${draftId}`);
  if (card) {
    card.style.transition = 'opacity 0.25s, transform 0.25s';
    card.style.opacity = '0';
    card.style.transform = 'scale(0.97)';
    setTimeout(() => card.remove(), 260);
  }
  appendAgentMessage('Invoice cancelled. Let me know if you need anything else.');
}


function buildEstimateCard(draft) {
  const card = document.createElement('div');
  card.className = 'draft-card manual-invoice-card estimate-card' + (draft.is_new_contact ? ' new-contact-card' : '');
  card.id = `estimate-draft-${draft.draft_id}`;

  const badge = draft.is_new_contact
    ? `<span class="draft-badge new-contact-badge">👤 New Contact + Estimate</span>`
    : `<span class="draft-badge">Estimate Draft</span>`;

  const itemsHtml = draft.line_items.map((item, idx) => `
    <div class="manual-item-row" id="estimate-item-row-${draft.draft_id}-${idx}">
      <div class="manual-item-header">
        <span class="manual-item-label">Item ${idx + 1}</span>
      </div>
      <div class="draft-fields manual-item-fields">
        <div class="draft-field">
          <label>Service / Name</label>
          <input type="text"
            class="estimate-item-name"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${escapeHtml(item.item_name)}"
            placeholder="e.g. Website Redesign" />
        </div>
        <div class="draft-field">
          <label>Description</label>
          <input type="text"
            class="estimate-item-desc"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${escapeHtml(item.description || '')}"
            placeholder="Full description" />
        </div>
        <div class="draft-field draft-field-amount">
          <label>Amount (${escapeHtml(draft.currency || 'INR')})</label>
          <input type="number"
            class="estimate-item-amount"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${Number(item.amount)}"
            placeholder="0"
            oninput="recalcEstimateTotal('${draft.draft_id}')" />
        </div>
      </div>
    </div>
  `).join('');

  card.innerHTML = `
    <div class="draft-header">
      ${badge}
      <span class="draft-subject">${escapeHtml(draft.client_name)}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client Name</label>
        <input type="text" id="estimate-client-name-${draft.draft_id}"
          value="${escapeHtml(draft.client_name || '')}"
          placeholder="Client name" />
      </div>
      <div class="draft-field">
        <label>Email</label>
        <input type="email" id="estimate-client-email-${draft.draft_id}"
          value="${escapeHtml(draft.client_email || '')}"
          placeholder="client@example.com" />
      </div>
    </div>
    <div class="manual-items-section">
      ${itemsHtml}
    </div>

    <div class="draft-actions">
      <button class="btn-approve ${draft.is_new_contact ? 'btn-new-contact' : ''}"
        id="estimate-approve-${draft.draft_id}"
        onclick="approveEstimate('${draft.draft_id}', false)">
        ${draft.is_new_contact ? '👤 Create Contact & Estimate' : '✓ Create Estimate'}
      </button>
      <button class="btn-approve-send"
        id="estimate-approve-send-${draft.draft_id}"
        onclick="approveEstimate('${draft.draft_id}', true)">
        ${draft.is_new_contact ? '👤 Create Contact & Send' : '📧 Create & Send Estimate'}
      </button>
      <button class="btn-close-action" title="Cancel" onclick="cancelEstimate('${draft.draft_id}')">✕</button>
    </div>`;
  return card;
}

function recalcEstimateTotal(draftId) {
  const amounts = document.querySelectorAll(`.estimate-item-amount[data-draft="${draftId}"]`);
  const total = Array.from(amounts).reduce((s, el) => s + (parseFloat(el.value) || 0), 0);
  const el = document.getElementById(`estimate-total-${draftId}`);
  if (el) {
    const cur = amounts[0]?.closest('.draft-field')?.querySelector('label')?.textContent?.match(/\(([^)]+)\)/)?.[1] || 'INR';
    el.textContent = `${cur} ${total.toLocaleString('en-IN')}`;
  }
}

function cancelEstimate(draftId) {
  const card = document.getElementById(`estimate-draft-${draftId}`);
  if (card) {
    card.style.transition = 'opacity 0.25s, transform 0.25s';
    card.style.opacity = '0';
    card.style.transform = 'scale(0.97)';
    setTimeout(() => card.remove(), 260);
  }
  appendAgentMessage('Estimate cancelled. Let me know if you need anything else.');
}

async function approveEstimate(draftId, sendEmail = false) {
  const approveBtn = document.getElementById(`estimate-approve-${draftId}`);
  const sendBtn    = document.getElementById(`estimate-approve-send-${draftId}`);
  const activeBtn  = sendEmail ? sendBtn : approveBtn;

  if (approveBtn) approveBtn.disabled = true;
  if (sendBtn)    sendBtn.disabled    = true;
  if (activeBtn)  activeBtn.textContent = sendEmail ? 'Sending…' : 'Processing…';

  const clientName  = document.getElementById(`estimate-client-name-${draftId}`)?.value?.trim()  || '';
  const clientEmail = document.getElementById(`estimate-client-email-${draftId}`)?.value?.trim() || '';

  const nameInputs   = document.querySelectorAll(`.estimate-item-name[data-draft="${draftId}"]`);
  const descInputs   = document.querySelectorAll(`.estimate-item-desc[data-draft="${draftId}"]`);
  const amountInputs = document.querySelectorAll(`.estimate-item-amount[data-draft="${draftId}"]`);
  const line_items = Array.from(nameInputs).map((el, i) => ({
    item_name:        el.value.trim() || 'Service',
    description:      descInputs[i]?.value?.trim() || el.value.trim(),
    amount:           parseFloat(amountInputs[i]?.value) || 0,
  }));

  try {
    const res = await fetch('/chat/estimate-approve', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id:     draftId,
        send_email:   sendEmail,
        client_name:  clientName,
        client_email: clientEmail,
        line_items,
      }),
    });
    const data = await res.json();
    document.getElementById(`estimate-draft-${draftId}`)?.remove();

    let extra = null;
    if (data.estimates_created?.length) {
      extra = document.createDocumentFragment();
      data.estimates_created.forEach(est => extra.appendChild(buildEstimateCreatedCard(est)));
    }
    appendAgentMessage(data.reply, extra);
  } catch (err) {
    console.error(err);
    appendAgentMessage('Error approving estimate: ' + err.message);
  }
}

function buildEstimateCreatedCard(est) {
  const card = document.createElement('div');
  card.className = 'invoice-card estimate-created-card';
  card.id = `estimate-card-${est.zoho_estimate_id}`;
  const viewBtn = est.estimate_url
    ? `<a href="${est.estimate_url}" target="_blank" class="btn-view-invoice">View →</a>`
    : '';
  const sendBtn = est.email_sent
    ? `<span class="invoice-sent-badge">📧 Sent</span>`
    : `<button class="btn-send-invoice" onclick="sendEstimateEmailDirect('${est.zoho_estimate_id}', '${escapeHtml(est.client_name)}', this)">Send to Client</button>`;
  card.innerHTML = `
    <div class="invoice-icon">📄</div>
    <div class="invoice-info">
      <div class="invoice-number">Estimate #${escapeHtml(est.estimate_number || '—')}</div>
      <div class="invoice-detail">${escapeHtml(est.client_name)} · ${est.currency} ${Number(est.amount).toLocaleString()}</div>
    </div>
    <div class="invoice-card-actions">
      ${viewBtn}
      ${sendBtn}
    </div>`;
  return card;
}

async function sendEstimateEmailDirect(estimateId, clientName, btnEl) {
  btnEl.disabled    = true;
  btnEl.textContent = 'Sending…';
  try {
    const res  = await fetch('/api/estimates/send', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ estimate_id: estimateId }),
    });
    const data = await res.json();
    if (data.success) {
      btnEl.outerHTML = '<span class="invoice-sent-badge">📧 Sent</span>';
      appendAgentMessage(`Estimate email sent successfully to ${clientName}.`);
    } else {
      throw new Error(data.error);
    }
    scrollToBottom();
  } catch (e) {
    btnEl.disabled    = false;
    btnEl.textContent = 'Send to Client';
    appendAgentMessage('Could not send the estimate — please try again.');
    console.error(e);
  }
}

function buildAmbiguousEstimatesCard(estimates, mode) {
  const cardId = `est-pick-${Date.now()}`;
  const card   = document.createElement('div');
  card.className   = 'draft-card est-pick-card';
  card.id          = cardId;
  card.dataset.mode = mode || 'accept';

  const statusColor = { sent:'#06b6d4', accepted:'#10b981', declined:'#ef4444', draft:'#64748b', invoiced:'#7c3aed' };
  const statusIcon  = { sent:'📤', accepted:'✅', declined:'✗', draft:'📝', invoiced:'🧾' };

  const itemsHtml = estimates.map((est, idx) => {
    const amt = Number(est.amount).toLocaleString('en-IN', {
      style: 'currency', currency: est.currency || 'INR', maximumFractionDigits: 0,
    });
    const st  = (est.status || 'sent').toLowerCase();
    const clr = statusColor[st] || '#94a3b8';
    const ico = statusIcon[st]  || '📄';
    const stLabel = est.status ? est.status.charAt(0).toUpperCase() + est.status.slice(1) : 'Sent';
    return `
      <label class="est-pick-item" for="est-chk-${cardId}-${idx}">
        <input type="checkbox" class="est-pick-chk" id="est-chk-${cardId}-${idx}"
          data-est-id="${escapeHtml(est.estimate_id)}" data-card="${cardId}" checked />
        <div class="est-pick-info">
          <div class="est-pick-number">#${escapeHtml(est.estimate_number)}</div>
          <div class="est-pick-customer">${escapeHtml(est.customer_name)}</div>
        </div>
        <div class="est-pick-right">
          <span class="est-pick-amount">${escapeHtml(amt)}</span>
          <span class="est-pick-badge" style="color:${clr};border-color:${clr}33;background:${clr}18;">${ico} ${stLabel}</span>
        </div>
      </label>`;
  }).join('');

  const modeLabel = { accept:'Accept', reject:'Decline', convert:'Convert to Invoice' }[card.dataset.mode] || 'Confirm';
  const modeColor = { accept:'var(--success)', reject:'var(--error)', convert:'var(--primary)' }[card.dataset.mode] || 'var(--primary)';

  card.innerHTML = `
    <div class="est-pick-header">
      <span class="draft-badge" style="background:rgba(124,58,237,0.15);color:#a78bfa;border-color:rgba(124,58,237,0.3);">📋 Select Estimates</span>
      <label class="est-pick-select-all">
        <input type="checkbox" id="est-all-${cardId}" checked onchange="toggleAllEstimates('${cardId}', this.checked)" />
        <span>All</span>
      </label>
    </div>
    <div class="est-pick-list">${itemsHtml}</div>
    <div class="est-pick-actions">
      <button class="btn-est-confirm" id="est-confirm-${cardId}"
        style="background:${modeColor}"
        onclick="confirmEstimateSelection('${cardId}')">
        ✓ ${modeLabel} Selected
      </button>
    </div>`;

  // Keep "All" checkbox in sync when individual boxes change
  setTimeout(() => {
    card.querySelectorAll('.est-pick-chk').forEach(chk => {
      chk.addEventListener('change', () => {
        const all    = [...card.querySelectorAll('.est-pick-chk')];
        const allCbx = document.getElementById(`est-all-${cardId}`);
        if (allCbx) allCbx.checked = all.every(c => c.checked);
      });
    });
  }, 0);

  return card;
}

function toggleAllEstimates(cardId, checked) {
  document.querySelectorAll(`#${cardId} .est-pick-chk`).forEach(c => c.checked = checked);
}

async function confirmEstimateSelection(cardId) {
  const card = document.getElementById(cardId);
  if (!card) return;

  const checked = [...card.querySelectorAll('.est-pick-chk:checked')];
  if (!checked.length) {
    appendAgentMessage('Please select at least one estimate first.');
    return;
  }

  const estimateIds = checked.map(c => c.dataset.estId);
  const mode        = card.dataset.mode || 'accept';
  const btn         = document.getElementById(`est-confirm-${cardId}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Processing…'; }

  try {
    const res  = await fetch('/chat/multi-estimate-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ estimate_ids: estimateIds, mode }),
    });
    const data = await res.json();
    card.remove();
    appendAgentMessage(data.reply);

    if (data.manual_invoice_draft) {
      const extra = document.createElement('div');
      extra.className = 'extra-cards';
      extra.appendChild(buildManualInvoiceCard(data.manual_invoice_draft));
      chatMessages.appendChild(extra);
      chatMessages.scrollTop = chatMessages.scrollHeight;
    }
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = '✓ Confirm'; }
    appendAgentMessage('Could not process estimates — please try again.');
  }
}

// Legacy compat for plain-text (Telegram) fallback
function chooseEstimateIndex(idx) {
  chatInput.value = String(idx);
  sendMessage();
}

function buildManualInvoiceCard(draft) {
  const card = document.createElement('div');
  card.className = 'draft-card manual-invoice-card' + (draft.is_new_contact ? ' new-contact-card' : '');
  card.id = `manual-draft-${draft.draft_id}`;

  const badge = draft.is_new_contact
    ? `<span class="draft-badge new-contact-badge">👤 New Contact + Invoice</span>`
    : `<span class="draft-badge">Invoice Draft</span>`;

  const itemsHtml = draft.line_items.map((item, idx) => `
    <div class="manual-item-row" id="manual-item-row-${draft.draft_id}-${idx}">
      <div class="manual-item-header">
        <span class="manual-item-label">Item ${idx + 1}</span>
      </div>
      <div class="draft-fields manual-item-fields">
        <div class="draft-field">
          <label>Service / Name</label>
          <input type="text"
            class="manual-item-name"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${escapeHtml(item.item_name)}"
            placeholder="e.g. Website Redesign" />
        </div>
        <div class="draft-field">
          <label>Description</label>
          <input type="text"
            class="manual-item-desc"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${escapeHtml(item.task_description)}"
            placeholder="Full description" />
        </div>
        <div class="draft-field draft-field-amount">
          <label>Amount (${escapeHtml(draft.currency || 'INR')})</label>
          <input type="number"
            class="manual-item-amount"
            data-draft="${draft.draft_id}" data-idx="${idx}"
            value="${Number(item.amount)}"
            placeholder="0"
            oninput="recalcManualTotal('${draft.draft_id}')" />
        </div>
      </div>
    </div>
  `).join('');

  const total = draft.line_items.reduce((s, i) => s + Number(i.amount || 0), 0);

  card.innerHTML = `
    <div class="draft-header">
      ${badge}
      <span class="draft-subject">${escapeHtml(draft.client_name)}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client Name</label>
        <input type="text" id="manual-client-name-${draft.draft_id}"
          value="${escapeHtml(draft.client_name || '')}"
          placeholder="Client name" />
      </div>
      <div class="draft-field">
        <label>Email</label>
        <input type="email" id="manual-client-email-${draft.draft_id}"
          value="${escapeHtml(draft.client_email || '')}"
          placeholder="client@example.com" />
      </div>
    </div>
    <div class="manual-items-section">
      ${itemsHtml}
    </div>

    <div class="draft-actions">
      <button class="btn-approve ${draft.is_new_contact ? 'btn-new-contact' : ''}"
        id="manual-approve-${draft.draft_id}"
        onclick="approveManualInvoice('${draft.draft_id}', false)">
        ${draft.is_new_contact ? '👤 Create Contact & Invoice' : '✓ Create Invoice'}
      </button>
      <button class="btn-approve-send"
        id="manual-approve-send-${draft.draft_id}"
        onclick="approveManualInvoice('${draft.draft_id}', true)">
        ${draft.is_new_contact ? '👤 Create Contact & Send' : '📧 Create & Send Invoice'}
      </button>
      <button class="btn-close-action" title="Cancel" onclick="cancelManualInvoice('${draft.draft_id}')">✕</button>
    </div>`;
  return card;
}

function recalcManualTotal(draftId) {
  const amounts = document.querySelectorAll(`.manual-item-amount[data-draft="${draftId}"]`);
  const total = Array.from(amounts).reduce((s, el) => s + (parseFloat(el.value) || 0), 0);
  const el = document.getElementById(`manual-total-${draftId}`);
  if (el) {
    const cur = amounts[0]?.closest('.draft-field')?.querySelector('label')?.textContent?.match(/\(([^)]+)\)/)?.[1] || 'INR';
    el.textContent = `${cur} ${total.toLocaleString('en-IN')}`;
  }
}

function buildRecurringInvoiceCard(draft) {
  const card = document.createElement('div');
  card.className = 'draft-card';
  card.id = `recurring-draft`;

  const total = Number(draft.amount || 0);
  const badge = `<span class="draft-badge">Recurring Invoice Draft</span>`;

  card.innerHTML = `
    <div class="draft-header">
      ${badge}
      <span class="draft-subject">${escapeHtml(draft.client_name || 'New Profile')}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client Name</label>
        <div>${escapeHtml(draft.client_name || '—')}</div>
      </div>
      <div class="draft-field">
        <label>Email</label>
        <div>${escapeHtml(draft.client_email || '—')}</div>
      </div>
      <div class="draft-field">
        <label>Service/Item</label>
        <div>${escapeHtml(draft.item_name || draft.task_description || 'Monthly Service')}</div>
      </div>
      <div class="draft-field">
        <label>Amount</label>
        <div>${escapeHtml(draft.currency || 'INR')} ${total.toLocaleString()}</div>
      </div>
      <div class="draft-field">
        <label>Frequency</label>
        <div>${escapeHtml(draft.frequency || 'Monthly')}</div>
      </div>
      <div class="draft-field">
        <label>Start Date</label>
        <div>${escapeHtml(draft.start_date || 'Today')}</div>
      </div>
    </div>
    <div class="draft-actions">
      <button class="btn-approve" id="recurring-confirm-btn" onclick="submitRecurringApproval(true)">
        ✓ Confirm & Create
      </button>
      <button class="btn-close-action" title="Cancel" onclick="submitRecurringApproval(false)">✕</button>
    </div>`;
  return card;
}

async function submitRecurringApproval(confirm) {
  const confirmBtn = document.getElementById('recurring-confirm-btn');
  const cancelBtn = document.getElementById('recurring-cancel-btn');
  if (confirmBtn) confirmBtn.disabled = true;
  if (cancelBtn) cancelBtn.disabled = true;
  if (confirmBtn && confirm) confirmBtn.textContent = 'Creating…';
  
  // Set value and send
  chatInput.value = confirm ? "confirm" : "cancel";
  document.getElementById('recurring-draft')?.remove();
  
  await sendMessage();
}

function buildPaymentRecordDraftCard(draft) {
  const card = document.createElement('div');
  card.className = 'payment-draft-card';
  card.id = `payment-draft-${draft.draft_id}`;

  const defaultMode = draft.payment_mode || 'banktransfer';
  const modes = [
    { value: 'banktransfer', label: 'Bank Transfer (UPI/NetBanking)' },
    { value: 'cash', label: 'Cash' },
    { value: 'check', label: 'Cheque' },
    { value: 'creditcard', label: 'Credit Card' },
    { value: 'bankremittance', label: 'Bank Remittance' },
    { value: 'others', label: 'Others' }
  ];
  const options = modes.map(m => `<option value="${m.value}" ${m.value === defaultMode ? 'selected' : ''}>${escapeHtml(m.label)}</option>`).join('');

  card.innerHTML = `
    <div class="draft-header">💵 Record Payment Draft</div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client</label>
        <div><strong>${escapeHtml(draft.customer_name)}</strong></div>
      </div>
      <div class="draft-field">
        <label>Invoice ID</label>
        <div><strong>#${escapeHtml(draft.invoice_id)}</strong></div>
      </div>
      <div class="draft-field">
        <label>Amount (${escapeHtml(draft.currency)})</label>
        <input type="number" step="0.01" class="draft-input" id="pay-amount-${draft.draft_id}" value="${draft.amount}">
      </div>
      <div class="draft-field">
        <label>Payment Method</label>
        <select class="draft-select" id="pay-mode-${draft.draft_id}">
          ${options}
        </select>
      </div>
      <div class="draft-field">
        <label>Payment Date</label>
        <input type="date" class="draft-input" id="pay-date-${draft.draft_id}" value="${escapeHtml(draft.payment_date)}">
      </div>
      <div class="overpayment-warning-msg" id="overpayment-warn-${draft.draft_id}" style="display: none; color: var(--warning); margin-top: 8px; font-size: 12px; font-weight: 500;">
        ⚠️ Warning: Payment exceeds invoice balance. This creates an overpayment in Zoho.
      </div>
    </div>
    <div class="draft-actions">
      <button class="btn-approve" id="pay-confirm-btn-${draft.draft_id}" onclick="confirmPayment('${draft.draft_id}')">
        ✓ Confirm Payment
      </button>
      <button class="btn-close-action" title="Cancel" onclick="cancelPaymentDraft('${draft.draft_id}')">✕</button>
    </div>`;

  const initialBalance = draft.amount;
  setTimeout(() => {
    const amtInput = document.getElementById(`pay-amount-${draft.draft_id}`);
    const warnEl = document.getElementById(`overpayment-warn-${draft.draft_id}`);
    if (amtInput && warnEl) {
      const check = () => {
        const val = parseFloat(amtInput.value);
        if (!isNaN(val) && val > initialBalance) {
          warnEl.style.display = 'block';
        } else {
          warnEl.style.display = 'none';
        }
      };
      amtInput.addEventListener('input', check);
      check();
    }
  }, 100);

  return card;
}

function cancelPaymentDraft(draftId) {
  const card = document.getElementById(`payment-draft-${draftId}`);
  if (card) {
    card.style.transition = 'opacity 0.25s, transform 0.25s';
    card.style.opacity = '0';
    card.style.transform = 'scale(0.97)';
    setTimeout(() => card.remove(), 260);
  }
  appendAgentMessage('Payment recording cancelled.');
}

async function confirmPayment(draftId) {
  const confirmBtn = document.getElementById(`pay-confirm-btn-${draftId}`);
  if (confirmBtn) confirmBtn.disabled = true;

  const amount = parseFloat(document.getElementById(`pay-amount-${draftId}`)?.value);
  const paymentMode = document.getElementById(`pay-mode-${draftId}`)?.value;
  const paymentDate = document.getElementById(`pay-date-${draftId}`)?.value;

  if (confirmBtn) confirmBtn.textContent = 'Recording…';

  try {
    const res = await fetch('/chat/payment-approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id: draftId,
        amount: isNaN(amount) ? undefined : amount,
        payment_mode: paymentMode || undefined,
        payment_date: paymentDate || undefined,
      }),
    });
    const data = await res.json();
    document.getElementById(`payment-draft-${draftId}`)?.remove();

    appendAgentMessage(data.reply);
    scrollToBottom();
  } catch (e) {
    if (confirmBtn) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Confirm Payment';
    }
    console.error(e);
    appendAgentMessage('Failed to record payment. Please try again.');
  }
}

function appendExtraCards(extra, data) {
  if (data.recurring_draft) {
    extra.appendChild(buildRecurringInvoiceCard(data.recurring_draft));
    return;
  }
  if (data.manual_invoice_draft) {
    extra.appendChild(buildManualInvoiceCard(data.manual_invoice_draft));
    return;
  }
  if (data.estimate_draft) {
    extra.appendChild(buildEstimateCard(data.estimate_draft));
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
    return;
  }
  if (data.estimates_created?.length) {
    data.estimates_created.forEach(est => extra.appendChild(buildEstimateCreatedCard(est)));
    return;
  }
  if (data.payment_record_draft) {
    extra.appendChild(buildPaymentRecordDraftCard(data.payment_record_draft));
    return;
  }
  // Reminder result cards — rendered when payment_invoices come back from a remind_overdue action
  if (data.payment_invoices?.length && data.action === 'invoice_sent') {
    extra.appendChild(buildReminderSummaryCard(data.payment_invoices));
  }
  if (data.ambiguous_estimates?.length) {
    extra.appendChild(buildAmbiguousEstimatesCard(data.ambiguous_estimates, data.disambiguation_mode || 'accept'));
  }
}

/** Compact summary card for payment reminders sent. */
function buildReminderSummaryCard(invoices) {
  const card = document.createElement('div');
  card.className = 'reminder-summary-card';

  const rows = invoices.map(inv => {
    const reminded = inv.last_reminded_at
      ? `<span class="reminded-badge" title="Reminder sent">📧 Reminded</span>`
      : '';
    const overdueTxt = inv.days_overdue != null
      ? `<span class="overdue-tag">${inv.days_overdue}d overdue</span>` : '';
    const balance = Number(inv.balance || 0).toLocaleString('en-IN', { style: 'currency', currency: inv.currency_code || 'INR' });
    const link = inv.zoho_view_url
      ? `<a href="${inv.zoho_view_url}" target="_blank" class="reminder-view-link">View →</a>`
      : '';
    return `
      <div class="reminder-row">
        <div class="reminder-row-left">
          <span class="reminder-name">${escapeHtml(inv.customer_name)}</span>
          ${overdueTxt}
        </div>
        <div class="reminder-row-right">
          <span class="reminder-balance">${escapeHtml(balance)}</span>
          ${reminded}
          ${link}
        </div>
      </div>`;
  }).join('');

  card.innerHTML = `
    <div class="reminder-summary-header">📧 Reminders sent (${invoices.length})</div>
    <div class="reminder-rows">${rows}</div>`;
  return card;
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
          const hasCards = (
            data.manual_invoice_draft || data.batch_draft ||
            data.drafts?.length || data.invoices_created?.length ||
            data.recurring_draft || data.payment_record_draft ||
            data.estimate_draft || data.estimates_created?.length ||
            data.ambiguous_estimates?.length ||
            (data.payment_invoices?.length && data.action === 'invoice_sent')
          );
          if (hasCards) {
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
  const sendBtn    = document.getElementById(`manual-approve-send-${draftId}`);
  const activeBtn  = sendEmail ? sendBtn : approveBtn;

  if (approveBtn) approveBtn.disabled = true;
  if (sendBtn)    sendBtn.disabled    = true;
  if (activeBtn)  activeBtn.textContent = sendEmail ? 'Sending…' : 'Processing…';

  // Read edited values from the card inputs
  const clientName  = document.getElementById(`manual-client-name-${draftId}`)?.value?.trim()  || '';
  const clientEmail = document.getElementById(`manual-client-email-${draftId}`)?.value?.trim() || '';

  // Collect edited line items
  const nameInputs   = document.querySelectorAll(`.manual-item-name[data-draft="${draftId}"]`);
  const descInputs   = document.querySelectorAll(`.manual-item-desc[data-draft="${draftId}"]`);
  const amountInputs = document.querySelectorAll(`.manual-item-amount[data-draft="${draftId}"]`);
  const line_items = Array.from(nameInputs).map((el, i) => ({
    item_name:        el.value.trim() || 'Service',
    task_description: descInputs[i]?.value?.trim() || el.value.trim(),
    amount:           parseFloat(amountInputs[i]?.value) || 0,
  }));

  try {
    const res = await fetch('/chat/manual-approve', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_id:     draftId,
        send_email:   sendEmail,
        // Pass edited values so backend can use them
        client_name:  clientName,
        client_email: clientEmail,
        line_items,
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
    if (approveBtn) { approveBtn.disabled = false; approveBtn.textContent = '✓ Create Invoice'; }
    if (sendBtn)    { sendBtn.disabled    = false; sendBtn.textContent    = '📧 Create & Send Invoice'; }
    appendAgentMessage('Failed to create invoice — please try again.');
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

async function sendInvoiceFromTable(invoiceId, viewUrl, btnEl) {
  btnEl.disabled = true;
  btnEl.textContent = '…';
  try {
    const res = await fetch('/api/invoices/send', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ invoice_id: invoiceId }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    
    // Update the status badge in the same row to "sent"
    const row = btnEl.closest('tr');
    if (row) {
      const statusBadge = row.querySelector('.status-badge');
      if (statusBadge) {
        statusBadge.className = 'status-badge badge-sent';
        statusBadge.innerHTML = '● sent';
      }
    }

    // Replace the whole actions cell with just the View link
    const cell = btnEl.closest('td');
    if (cell) cell.innerHTML = `<a href="${escapeHtml(viewUrl)}" target="_blank" rel="noopener" class="tbl-link-btn">View →</a>`;
  } catch (e) {
    btnEl.disabled = false;
    btnEl.textContent = 'Send';
    btnEl.title = e.message || 'Failed to send';
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

/* ══════════════════════════════════════════════════════════════════════════
   PAGE ROUTING
   ══════════════════════════════════════════════════════════════════════════ */

let _chartRevenue   = null;
let _chartStatus    = null;
let _chartCustomers = null;
let _currentPage    = 'chat';

function showPage(name) {
  _currentPage = name;

  // Toggle page panels
  document.querySelectorAll('.page-content').forEach(el => el.classList.add('hidden'));
  const target = document.getElementById(`page-${name}`);
  if (target) target.classList.remove('hidden');

  // Update nav active states
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  const navEl = document.getElementById(`nav-${name}`);
  if (navEl) navEl.classList.add('active');

  // Lazy-load data
  if (name === 'invoices') refreshInvoices();
  if (name === 'analytics') loadStats();
}

/* ── Invoices Page ─────────────────────────────────────────────────────── */

function refreshInvoices() {
  const activeTab = document.querySelector('.tab-btn.active');
  const tab = activeTab ? activeTab.id.replace('tab-', '') : 'all';
  if (tab === 'recurring') loadRecurring();
  else {
    const activeChip = document.querySelector('.chip.active');
    const filter = activeChip ? (activeChip.dataset.filter || 'all') : 'all';
    loadInvoices(filter);
  }
}

function switchInvoiceTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.getElementById('tab-' + tab)?.classList.add('active');
  document.getElementById(tab === 'all' ? 'inv-all-panel' : 'inv-recurring-panel')?.classList.remove('hidden');
  if (tab === 'recurring') loadRecurring();
  else loadInvoices('all');
}

function filterInvoices(chipEl, filter) {
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  chipEl.classList.add('active');
  loadInvoices(filter);
}

function formatCurrency(value, currency) {
  const cur = currency || 'INR';
  const num = parseFloat(value) || 0;
  const sym = cur === 'INR' ? '₹' : (cur === 'USD' ? '$' : cur + ' ');
  return sym + num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function loadInvoices(filter) {
  filter = filter || 'all';
  const tbody = document.getElementById('invoices-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr class="table-loading"><td colspan="8">Loading invoices…</td></tr>';

  try {
    const res  = await fetch('/api/invoices?status=' + filter);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    const invoices = data.invoices || [];
    const batchContainer = document.getElementById('batch-send-container');
    if (batchContainer) batchContainer.classList.add('hidden');

    if (!invoices.length) {
      tbody.innerHTML = '<tr class="table-empty"><td colspan="8">No invoices found.</td></tr>';
      return;
    }

    const badgeMap = { sent: 'badge-sent', paid: 'badge-paid', overdue: 'badge-overdue', draft: 'badge-draft', void: 'badge-void' };
    const dotMap   = { sent: '●', paid: '●', overdue: '⚠', draft: '○', void: '○' };

    tbody.innerHTML = invoices.map(inv => {
      const status   = (inv.status || 'draft').toLowerCase();
      const badgeCls = badgeMap[status] || 'badge-draft';
      const dot      = dotMap[status] || '○';
      const amount   = formatCurrency(inv.total, inv.currency_code);
      const balance  = status === 'paid'
        ? '<span style="color:var(--success)">Paid</span>'
        : formatCurrency(inv.balance, inv.currency_code);
      const date     = inv.invoice_date   || '—';
      const due      = inv.due_date       || '—';
      const invNum   = inv.invoice_number || inv.invoice_id || '—';
      const client   = inv.customer_name  || '—';
      const url      = inv.invoice_url    || '#';
      const actions = status === 'draft'
        ? `<div class="td-actions">
             <button class="tbl-send-btn" 
                     data-id="${escapeHtml(inv.invoice_id || inv.zoho_invoice_id || '')}"
                     data-url="${escapeHtml(url)}"
                     onclick="sendInvoiceFromTable('${escapeHtml(inv.invoice_id || inv.zoho_invoice_id || '')}', '${escapeHtml(url)}', this)">Send</button>
             <a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="tbl-link-btn">View →</a>
           </div>`
        : `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="tbl-link-btn">View →</a>`;
      return `<tr>
        <td class="td-muted">${escapeHtml(String(invNum))}</td>
        <td><strong>${escapeHtml(String(client))}</strong></td>
        <td class="td-amount">${amount}</td>
        <td class="td-amount">${balance}</td>
        <td><span class="status-badge ${badgeCls}">${dot} ${status}</span></td>
        <td class="td-muted">${escapeHtml(String(date))}</td>
        <td class="td-muted">${escapeHtml(String(due))}</td>
        <td class="td-link">${actions}</td>
      </tr>`;
    }).join('');

    // If filter is 'draft' and there are invoices, show batch send button
    if (filter === 'draft' && invoices.some(inv => (inv.status || '').toLowerCase() === 'draft') && batchContainer) {
      batchContainer.classList.remove('hidden');
    }
  } catch (e) {
    const batchContainer = document.getElementById('batch-send-container');
    if (batchContainer) batchContainer.classList.add('hidden');
    tbody.innerHTML = `<tr class="table-empty"><td colspan="8">⚠️ ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function sendAllDraftInvoices() {
  const btn = document.getElementById('btn-batch-send');
  if (!btn) return;

  const sendBtns = Array.from(document.querySelectorAll('#invoices-tbody .tbl-send-btn:not(:disabled)'));
  if (!sendBtns.length) {
    appendAgentMessage('No draft invoices to send.');
    return;
  }

  btn.disabled = true;
  const originalText = btn.innerHTML;
  btn.innerHTML = `⏳ Sending 0 / ${sendBtns.length}…`;

  let successCount = 0;
  
  // Send in parallel limit to avoid server strain, or just simple parallel
  const promises = sendBtns.map(async (sendBtn) => {
    const invoiceId = sendBtn.getAttribute('data-id');
    const viewUrl = sendBtn.getAttribute('data-url');
    if (!invoiceId) return;

    const ok = await sendInvoiceFromTable(invoiceId, viewUrl, sendBtn);
    if (ok) {
      successCount++;
      btn.innerHTML = `⏳ Sending ${successCount} / ${sendBtns.length}…`;
    }
  });

  await Promise.all(promises);

  btn.innerHTML = `✅ Sent ${successCount} invoices!`;
  setTimeout(() => {
    btn.disabled = false;
    btn.innerHTML = originalText;
    // Refresh the invoices list (which will update the UI and hide the button)
    const activeChip = document.querySelector('.filter-chips .chip.active');
    if (activeChip) {
      activeChip.click();
    }
  }, 2000);
}


async function loadRecurring() {
  const tbody = document.getElementById('recurring-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr class="table-loading"><td colspan="7">Loading recurring invoices…</td></tr>';

  try {
    const res  = await fetch('/api/invoices/recurring');
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    const invoices = data.recurring_invoices || [];
    if (!invoices.length) {
      tbody.innerHTML = '<tr class="table-empty"><td colspan="7">No active recurring invoices.</td></tr>';
      return;
    }

    tbody.innerHTML = invoices.map(inv => {
      const name   = inv.recurrence_name || inv.customer_name || '—';
      const client = inv.customer_name   || '—';
      const freq   = inv.recurrence_frequency || '—';
      const amount = formatCurrency(inv.total || inv.amount || 0, inv.currency_code || 'INR');
      const start  = inv.start_date          || '—';
      const next   = inv.next_invoice_date   || '—';
      const url    = inv.recurring_invoice_url || '#';
      return `<tr>
        <td><strong>${escapeHtml(String(name))}</strong></td>
        <td class="td-muted">${escapeHtml(String(client))}</td>
        <td class="td-amount">${amount}</td>
        <td><span class="freq-badge">${escapeHtml(String(freq))}</span></td>
        <td class="td-muted">${escapeHtml(String(start))}</td>
        <td class="td-muted">${escapeHtml(String(next))}</td>
        <td class="td-link"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">View →</a></td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="table-empty"><td colspan="7">⚠️ ${escapeHtml(e.message)}</td></tr>`;
  }
}

/* ── Analytics Page ────────────────────────────────────────────────────── */

async function loadStats() {
  const statIds = ['stat-outstanding','stat-collected','stat-overdue-count',
                   'stat-recurring-count','stat-sent-count','stat-paid-count','stat-overdue-amount'];
  statIds.forEach(id => { const el = document.getElementById(id); if (el) el.textContent = '…'; });

  try {
    const res  = await fetch('/api/stats');
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    document.getElementById('stat-outstanding').textContent     = formatCurrency(data.outstanding_amount);
    document.getElementById('stat-collected').textContent       = formatCurrency(data.collected_this_month);
    document.getElementById('stat-overdue-count').textContent   = data.overdue_count;
    document.getElementById('stat-recurring-count').textContent = data.recurring_count;
    document.getElementById('stat-sent-count').textContent      = (data.sent_count || 0) + ' invoices unpaid';
    document.getElementById('stat-paid-count').textContent      = (data.paid_count_this_month || 0) + ' invoices paid';
    document.getElementById('stat-overdue-amount').textContent  = formatCurrency(data.overdue_amount) + ' overdue';

    // ── Revenue line chart ──────────────────────────────────────────────
    const revenueData = data.revenue_history || [];
    if (_chartRevenue) { _chartRevenue.destroy(); _chartRevenue = null; }
    const ctxRev = document.getElementById('chart-revenue');
    if (ctxRev) {
      _chartRevenue = new Chart(ctxRev, {
        type: 'line',
        data: {
          labels: revenueData.map(r => r.month),
          datasets: [{
            label: 'Revenue Collected',
            data: revenueData.map(r => r.amount),
            borderColor: '#7c3aed',
            backgroundColor: 'rgba(124,58,237,0.08)',
            borderWidth: 2.5,
            pointBackgroundColor: '#9155ff',
            pointBorderColor: '#9155ff',
            pointRadius: 5,
            pointHoverRadius: 7,
            tension: 0.4,
            fill: true,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: 'rgba(15,15,26,0.95)',
              titleColor: '#94a3b8',
              bodyColor: '#f1f5f9',
              borderColor: 'rgba(124,58,237,0.4)',
              borderWidth: 1,
              callbacks: { label: ctx => ' ' + formatCurrency(ctx.raw) }
            }
          },
          scales: {
            x: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#64748b', font: { size: 11 } } },
            y: {
              grid: { color: 'rgba(255,255,255,0.05)' },
              ticks: {
                color: '#64748b',
                font: { size: 11 },
                callback: v => '₹' + (v >= 1000 ? (v/1000).toFixed(1) + 'k' : v)
              },
              beginAtZero: true
            }
          }
        }
      });
    }

    // ── Status donut chart (uses all-time paid count so it's never empty) ──
    if (_chartStatus) { _chartStatus.destroy(); _chartStatus = null; }
    const ctxStatus = document.getElementById('chart-status');
    if (ctxStatus) {
      // Use all-time paid count so the chart has data even if nothing paid this month
      const paid    = data.paid_total_count || data.paid_count_this_month || 0;
      const sent    = data.sent_count   || 0;
      const overdue = data.overdue_count || 0;
      const total   = paid + sent + overdue;

      if (total === 0) {
        // No invoices at all — show placeholder text
        ctxStatus.style.display = 'none';
        const msg = document.createElement('p');
        msg.style.cssText = 'text-align:center;color:var(--text3);margin-top:40px;font-size:13px';
        msg.textContent = 'No invoice data yet';
        ctxStatus.parentNode.appendChild(msg);
      } else {
        ctxStatus.style.display = '';
        _chartStatus = new Chart(ctxStatus, {
          type: 'doughnut',
          data: {
            labels: ['Paid', 'Sent', 'Overdue'],
            datasets: [{
              data: [paid, sent, overdue],
              backgroundColor: ['rgba(16,185,129,0.7)', 'rgba(59,130,246,0.7)', 'rgba(239,68,68,0.7)'],
              borderColor:     ['#10b981', '#3b82f6', '#ef4444'],
              borderWidth: 1.5,
              hoverOffset: 8,
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '68%',
            plugins: {
              legend: {
                position: 'bottom',
                labels: { color: '#94a3b8', padding: 14, font: { size: 12 } }
              },
              tooltip: {
                backgroundColor: 'rgba(15,15,26,0.95)',
                titleColor: '#94a3b8',
                bodyColor: '#f1f5f9',
                borderColor: 'rgba(255,255,255,0.08)',
                borderWidth: 1,
                callbacks: {
                  label: ctx => ` ${ctx.label}: ${ctx.raw} (${Math.round(ctx.raw / total * 100)}%)`
                }
              }
            }
          }
        });
      }
    }

    // ── Customer breakdown horizontal stacked bar chart ──────────────────
    const customerBreakdown = data.customer_breakdown || [];
    const sortedCustomers = customerBreakdown
      .map(c => ({
        ...c,
        total: parseFloat(c.paid) + parseFloat(c.overdue) + parseFloat(c.sent)
      }))
      .filter(c => c.total > 0)
      .sort((a, b) => b.total - a.total)
      .slice(0, 10);

    if (_chartCustomers) { _chartCustomers.destroy(); _chartCustomers = null; }
    const ctxCust = document.getElementById('chart-customers');
    if (ctxCust) {
      // Remove any existing no-data message
      const oldMsg = ctxCust.parentNode.querySelector('.no-cust-msg');
      if (oldMsg) oldMsg.remove();

      if (sortedCustomers.length === 0) {
        ctxCust.style.display = 'none';
        const msg = document.createElement('p');
        msg.className = 'no-cust-msg';
        msg.style.cssText = 'text-align:center;color:var(--text3);margin-top:40px;font-size:13px';
        msg.textContent = 'No customer transactions found';
        ctxCust.parentNode.appendChild(msg);
      } else {
        ctxCust.style.display = '';
        _chartCustomers = new Chart(ctxCust, {
          type: 'bar',
          data: {
            labels: sortedCustomers.map(c => c.customer_name),
            datasets: [
              {
                label: 'Paid',
                data: sortedCustomers.map(c => c.paid),
                backgroundColor: 'rgba(16,185,129,0.7)',
                borderColor: '#10b981',
                borderWidth: 1.5
              },
              {
                label: 'Sent (Unpaid)',
                data: sortedCustomers.map(c => c.sent),
                backgroundColor: 'rgba(59,130,246,0.7)',
                borderColor: '#3b82f6',
                borderWidth: 1.5
              },
              {
                label: 'Overdue',
                data: sortedCustomers.map(c => c.overdue),
                backgroundColor: 'rgba(239,68,68,0.7)',
                borderColor: '#ef4444',
                borderWidth: 1.5
              }
            ]
          },
          options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: {
                position: 'top',
                labels: { color: '#94a3b8', font: { size: 11 } }
              },
              tooltip: {
                backgroundColor: 'rgba(15,15,26,0.95)',
                titleColor: '#94a3b8',
                bodyColor: '#f1f5f9',
                borderColor: 'rgba(255,255,255,0.08)',
                borderWidth: 1,
                callbacks: {
                  label: ctx => ` ${ctx.dataset.label}: ${formatCurrency(ctx.raw)}`
                }
              }
            },
            scales: {
              x: {
                stacked: true,
                grid: { color: 'rgba(255,255,255,0.05)' },
                ticks: {
                  color: '#64748b',
                  font: { size: 10 },
                  callback: v => '₹' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v)
                }
              },
              y: {
                stacked: true,
                grid: { display: false },
                ticks: { color: '#94a3b8', font: { size: 11 } }
              }
            }
          }
        });
      }
    }


  } catch (e) {
    console.error('Stats load failed:', e);
    statIds.forEach(id => { const el = document.getElementById(id); if (el) el.textContent = '—'; });
  }
}

/* ── Bootstrap ─────────────────────────────────────────────────────────── */
checkAuthStatus();
