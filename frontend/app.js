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

function appendTypingIndicator() {
  const el = document.createElement('div');
  el.className = 'message agent-message typing-indicator';
  el.id = 'typing-indicator';
  el.innerHTML = `
    <div class="message-avatar">⚡</div>
    <div class="message-body">
      <div class="message-bubble">
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
      </div>
    </div>`;
  messagesList.appendChild(el);
  scrollToBottom();
}

function removeTypingIndicator() {
  document.getElementById('typing-indicator')?.remove();
}

function renderMarkdown(text) {
  // Simple markdown: **bold**, bullet lists, line breaks
  return text
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^• (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .replace(/\n/g, '<br>');
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
  const card = document.createElement('div');
  card.className = 'draft-card';
  card.id = `draft-${draft.draft_id}`;

  const missing = d.missing_fields?.length
    ? `<div class="draft-missing">⚠ Low confidence — missing: ${d.missing_fields.join(', ')}</div>`
    : '';

  card.innerHTML = `
    <div class="draft-header">
      <span class="draft-badge">Needs Approval</span>
      <span class="draft-subject">${escapeHtml(draft.email_subject || 'Email')}</span>
    </div>
    <div class="draft-fields">
      <div class="draft-field">
        <label>Client</label>
        <input type="text" id="draft-client-${draft.draft_id}" value="${escapeHtml(d.client_name || '')}" ${draft.zoho_contact_id ? 'readonly' : 'placeholder="New client name"'} />
      </div>
      <div class="draft-field">
        <label>Amount (${d.currency || 'USD'})</label>
        <input type="number" id="draft-amount-${draft.draft_id}" value="${d.amount ?? ''}" placeholder="e.g. 1200" />
      </div>
      <div class="draft-field full-width">
        <label>Description</label>
        <input type="text" id="draft-desc-${draft.draft_id}" value="${escapeHtml(d.task_description || '')}" placeholder="What was the work?" />
      </div>
    </div>
    ${missing}
    <div class="draft-actions">
      <button class="btn-approve" id="approve-${draft.draft_id}" onclick="approveDraft('${draft.draft_id}')">
        ✓ Create Invoice
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
  const viewBtn = inv.invoice_url
    ? `<a href="${inv.invoice_url}" target="_blank" class="btn-view-invoice">View →</a>`
    : '';
  card.innerHTML = `
    <div class="invoice-icon">🧾</div>
    <div class="invoice-info">
      <div class="invoice-number">Invoice #${escapeHtml(inv.invoice_number || '—')}</div>
      <div class="invoice-detail">${escapeHtml(inv.client_name)} · ${inv.currency} ${Number(inv.amount).toLocaleString()}</div>
    </div>
    ${viewBtn}`;
  return card;
}

/* ─── Send message ───────────────────────────────────────────────────────── */
async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text) return;

  chatInput.value = '';
  chatInput.style.height = 'auto';
  sendBtn.disabled = true;
  setStatus('Thinking…', 'loading');

  appendUserMessage(text);
  appendTypingIndicator();

  try {
    const res  = await fetch('/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: text }),
    });
    const data = await res.json();
    removeTypingIndicator();

    // Build extra content
    let extra = null;

    if (data.drafts?.length) {
      extra = document.createDocumentFragment();
      data.drafts.forEach(d => extra.appendChild(buildDraftCard(d)));
    }

    if (data.invoices_created?.length && !data.drafts?.length) {
      extra = document.createDocumentFragment();
      data.invoices_created.forEach(inv => extra.appendChild(buildInvoiceCard(inv)));
    }

    appendAgentMessage(data.reply, extra);
    setStatus('Ready', 'idle');
  } catch (e) {
    removeTypingIndicator();
    appendAgentMessage('Something went wrong — please try again.');
    setStatus('Ready', 'idle');
    console.error(e);
  } finally {
    sendBtn.disabled = false;
    chatInput.focus();
  }
}

/* ─── Approve / Decline draft ────────────────────────────────────────────── */
async function approveDraft(draftId) {
  const btn   = document.getElementById(`approve-${draftId}`);
  const desc  = document.getElementById(`draft-desc-${draftId}`)?.value?.trim();
  const amount= parseFloat(document.getElementById(`draft-amount-${draftId}`)?.value);
  const client= document.getElementById(`draft-client-${draftId}`)?.value?.trim();

  btn.disabled = true;
  btn.textContent = 'Creating…';

  try {
    const res  = await fetch('/chat/approve', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        draft_id: draftId,
        task_description: desc || undefined,
        amount: isNaN(amount) ? undefined : amount,
        client_name: client || undefined,
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
    btn.disabled = false;
    btn.textContent = '✓ Create Invoice';
    appendAgentMessage('Failed to create invoice — please try again.');
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
