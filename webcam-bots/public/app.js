// ── Auth helpers ──────────────────────────────────────────────────────────────
const safeStorage = {
    get: (key) => {
        try { return localStorage.getItem(key) || ''; }
        catch (err) { return ''; }
    },
    remove: (key) => {
        try { localStorage.removeItem(key); }
        catch (err) {}
    }
};

const getToken = () => safeStorage.get('auth_token');
const clearToken = () => safeStorage.remove('auth_token');
const redirectToLogin = () => {
    clearToken();
    window.location.replace('/login.html');
};

const revealApp = () => {
    const app    = document.getElementById('app-container');
    const loader = document.getElementById('loading-screen');
    if (app) app.style.display = '';
    if (loader) loader.remove();
};

let currentRole = 'admin';

const applyRoleAccess = (role) => {
    const isEmployee = role === 'employee';
    const dashboardTab = document.querySelector('[data-tab="dashboard"]');
    const dashboardPane = document.getElementById('tab-dashboard');
    const accountsTab = document.querySelector('[data-tab="accounts"]');
    const accountsPane = document.getElementById('tab-accounts');
    const messagesTab = document.querySelector('[data-tab="messages"]');
    const messagesPane = document.getElementById('tab-messages');
    const settingsTab = document.querySelector('[data-tab="settings"]');
    const settingsPane = document.getElementById('tab-settings');
    const startBtn = document.getElementById('btn-start-anon');
    const stopBtn = document.getElementById('btn-stop-anon');

    if (dashboardTab) dashboardTab.classList.toggle('hidden', isEmployee);
    if (dashboardPane) dashboardPane.classList.toggle('hidden', isEmployee);
    if (messagesTab) messagesTab.classList.toggle('hidden', isEmployee);
    if (messagesPane) messagesPane.classList.toggle('hidden', isEmployee);
    if (settingsTab) settingsTab.classList.toggle('hidden', isEmployee);
    if (settingsPane) settingsPane.classList.toggle('hidden', isEmployee);

    if (startBtn) startBtn.style.display = isEmployee ? 'none' : '';
    if (stopBtn) stopBtn.style.display = isEmployee ? 'none' : '';

    if (isEmployee) {
        const activeTab = document.querySelector('.nav-item.active');
        if (activeTab && ['dashboard', 'settings', 'messages'].includes(activeTab.dataset.tab)) {
            document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            if (accountsTab) accountsTab.classList.add('active');
            if (accountsPane) accountsPane.classList.add('active');
            if (typeof loadAccounts === 'function') loadAccounts();
        }
    }
};

const verifySession = async () => {
    const token = getToken();
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    try {
        const res = await fetch('/api/session', { headers, credentials: 'same-origin' });
        if (res.ok) {
            const data = await res.json();
            currentRole = data.role || 'admin';
            applyRoleAccess(currentRole);
            if (currentRole === 'admin') {
                loadEmployees();
            }
            revealApp();
            return;
        }
    } catch (err) {}
    redirectToLogin();
};

// Auth gate — uses token if available, otherwise relies on HttpOnly cookie.
(() => {
    const token = getToken();
    if (token) {
        revealApp();
        verifySession();
        return;
    }
    verifySession();
})();

// Authenticated fetch wrapper — auto-redirects on 401
const authFetch = async (url, options = {}) => {
    const token = getToken();
    options.headers = { ...(options.headers || {}) };
    if (token) {
        options.headers.Authorization = `Bearer ${token}`;
    }
    options.credentials = 'same-origin';
    const res = await fetch(url, options);
    if (res.status === 401) { redirectToLogin(); }
    return res;
};

const socket = io({ auth: { token: getToken() }, withCredentials: true });

const ACCOUNTS_PAGE_SIZE = 10;
const TARGETS_PAGE_SIZE = 10;
let accountsData = [];
let targetsData = [];
let accountsPage = 1;
let targetsPage = 1;
let pendingNewAccount = false;
let pendingNewTarget = false;
let allAccountsEnabled = true;
let allTargetsEnabled = true;

const normalizeEnabled = (value) => {
    if (value === undefined || value === null) return true;
    if (value === true || value === 1 || value === '1') return true;
    return false;
};

// UI Elements
const els = {
    anon: {
        status: document.getElementById('status-anon'),
        start:  document.getElementById('btn-start-anon'),
        stop:   document.getElementById('btn-stop-anon')
    },
    ip:            document.getElementById('stat-current-ip'),
    browsersCount: document.getElementById('stat-active-browsers'),
    totalAccounts: document.getElementById('stat-total-accounts'),
    totalTargets:  document.getElementById('stat-total-targets'),
    totalMessages: document.getElementById('stat-total-messages'),
    todayMessages: document.getElementById('stat-today-messages'),
    consoleOutput: document.getElementById('console-output'),
    clearConsole:  document.getElementById('btn-clear-console')
};

// Log helper

// IP Detection
const updateIp = async () => {
    try {
        const res = await fetch('https://api.ipify.org?format=json');
        const data = await res.json();
        els.ip.textContent = data.ip;
    } catch (e) {
        els.ip.textContent = 'Error';
    }
};

updateIp();
setInterval(updateIp, 30000);

const updateMessageStats = (stats = {}) => {
    const total = stats.total ?? 0;
    const today = stats.today ?? 0;
    if (els.totalMessages) els.totalMessages.textContent = total;
    if (els.todayMessages) els.todayMessages.textContent = today;
};

const loadMessageStats = async () => {
    const res = await authFetch('/api/message-stats');
    if (!res.ok) return;
    const data = await res.json();
    updateMessageStats(data);
};

loadMessageStats();
setInterval(loadMessageStats, 30000);

// Socket Handlers
socket.on('browser-count', ({ count }) => {
    if (els.browsersCount) els.browsersCount.textContent = count;
    const bigCounter = document.getElementById('active-browsers-count');
    if (bigCounter) bigCounter.textContent = count;
});

socket.on('message-stats', (stats) => {
    updateMessageStats(stats);
});

socket.on('status', ({ name, status }) => {
    const isRunning = status === 'running';
    const group = els[name];
    if (!group || !group.status) return;

    const badge = group.status;
    const startBtn = group.start;
    const stopBtn = group.stop;

    badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
    badge.className = `status-badge ${status}`;

    if (startBtn) startBtn.disabled = isRunning;
    if (stopBtn) stopBtn.disabled = !isRunning;
});

socket.on('log', ({ name, type, data }) => {
    if (!els.consoleOutput) return;
    
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="time">[${time}]</span> <span class="proc">[${name}]</span> ${data}`;
    
    els.consoleOutput.appendChild(entry);
    els.consoleOutput.scrollTop = els.consoleOutput.scrollHeight;
    
    // Keep last 100 logs
    while (els.consoleOutput.childNodes.length > 100) {
        els.consoleOutput.removeChild(els.consoleOutput.firstChild);
    }
});

// Event Listeners
// ── Confirmation Modal ────────────────────────────────────────────────────────
const confirmModal  = document.getElementById('confirm-modal');
const modalConfirm  = document.getElementById('modal-confirm');
const modalCancel   = document.getElementById('modal-cancel');

const showModal = () => confirmModal.classList.add('visible');
const hideModal = () => confirmModal.classList.remove('visible');

// Close when clicking the backdrop (outside the box)
confirmModal.addEventListener('click', (e) => {
    if (e.target === confirmModal) hideModal();
});

modalCancel.onclick = hideModal;

modalConfirm.onclick = () => {
    hideModal();
    // Proceed with starting bots
    els.anon.start.disabled = true;
    els.anon.stop.disabled = false;
    els.anon.status.textContent = 'Starting...';
    els.anon.status.className = 'status-badge starting';
    socket.emit('start-anon', {});
};

// Event Listeners
els.anon.start.onclick = () => showModal();

els.anon.stop.onclick = () => {
    socket.emit('stop-anon');
    // Immediately reset UI so start works again without a page refresh
    els.anon.start.disabled = false;
    els.anon.stop.disabled = true;
    els.anon.status.textContent = 'Stopped';
    els.anon.status.className = 'status-badge stopped';
};


els.clearConsole.onclick = () => {
    els.consoleOutput.innerHTML = '<div class="log-entry system">Console cleared.</div>';
};

// Tab Switching Logic
document.querySelectorAll('.nav-item').forEach(btn => {
    btn.onclick = () => {
        const tab = btn.dataset.tab;
        document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        const target = document.getElementById(`tab-${tab}`);
        if (target) target.classList.add('active');
        if (tab === 'accounts') loadAccounts();
        if (tab === 'targets')  loadTargets();
        if (tab === 'messages') loadSettings();
        if (tab === 'settings') {
            loadSettings();
            loadEmployees();
        }
    };
});

// Logout
document.getElementById('btn-logout')?.addEventListener('click', async () => {
    await authFetch('/api/logout', { method: 'POST' });
    redirectToLogin();
});

const loadTargets = async () => {
    const res  = await authFetch('/api/targets');
    const data = await res.json();
    targetsData = data.map(t => ({ ...t, enabled: normalizeEnabled(t.enabled) }));
    if (els.totalTargets) els.totalTargets.textContent = targetsData.length;
    renderTargetsPage();
};

const renderTargetsPage = () => {
    const list = document.getElementById('targets-list');
    if (!list) return;

    const totalPages = Math.max(1, Math.ceil(targetsData.length / TARGETS_PAGE_SIZE));
    if (targetsPage > totalPages) targetsPage = totalPages;

    allTargetsEnabled = targetsData.length ? targetsData.every(t => t.enabled) : true;
    updateGlobalTargetsToggle();

    list.innerHTML = '';

    if (pendingNewTarget && targetsPage === 1) {
        const row = document.createElement('tr');
        row.dataset.enabled = '1';
        row.innerHTML = `
            <td class="auto-cell">
                <button class="auto-toggle auto-toggle-target enabled" data-id="new" type="button" aria-pressed="true" title="Automation enabled">👁</button>
            </td>
            <td><input type="text" placeholder="Username" data-id="new" class="target-user"></td>
            <td>
                <div class="table-actions">
                    <button class="btn btn-primary btn-sm btn-save-target" data-id="new">Save</button>
                </div>
            </td>
        `;
        list.appendChild(row);
    }

    const start = (targetsPage - 1) * TARGETS_PAGE_SIZE;
    const pageItems = targetsData.slice(start, start + TARGETS_PAGE_SIZE);
    pageItems.forEach(t => {
        const row = document.createElement('tr');
        const enabled = t.enabled;
        row.dataset.enabled = enabled ? '1' : '0';
        row.innerHTML = `
            <td class="auto-cell">
                <button class="auto-toggle auto-toggle-target ${enabled ? 'enabled' : 'disabled'}" data-id="${t.id}" type="button" aria-pressed="${enabled ? 'true' : 'false'}" title="${enabled ? 'Automation enabled' : 'Automation disabled'}">${enabled ? '👁' : '🚫'}</button>
            </td>
            <td><input type="text" value="${t.username}" data-id="${t.id}" class="target-user"></td>
            <td>
                <div class="table-actions">
                    <button class="btn btn-primary btn-sm btn-save-target" data-id="${t.id}">Save</button>
                    <button class="btn btn-danger btn-sm btn-delete-target" data-id="${t.id}">🗑️</button>
                </div>
            </td>
        `;
        list.appendChild(row);
    });

    list.querySelectorAll('.auto-toggle-target').forEach(btn => { btn.onclick = () => toggleTarget(btn); });
    list.querySelectorAll('.btn-save-target').forEach(btn => btn.onclick = () => saveTarget(btn.dataset.id));
    list.querySelectorAll('.btn-delete-target').forEach(btn => btn.onclick = () => deleteTarget(btn.dataset.id));

    updateTargetsPagination(totalPages);
};

const updateTargetsPagination = (totalPages) => {
    const prev = document.getElementById('targets-prev');
    const next = document.getElementById('targets-next');
    const info = document.getElementById('targets-page-info');
    if (prev) prev.disabled = targetsPage <= 1;
    if (next) next.disabled = targetsPage >= totalPages;
    if (info) info.textContent = `Page ${targetsPage} / ${totalPages}`;
};

const updateGlobalTargetsToggle = () => {
    const btn = document.getElementById('btn-toggle-all-targets');
    if (!btn) return;
    btn.classList.toggle('disabled', !allTargetsEnabled);
    btn.textContent = allTargetsEnabled ? '👁 All' : '🚫 All';
    btn.title = allTargetsEnabled ? 'Disable all automation' : 'Enable all automation';
};

// Sync selectors to inputs removed

const saveTarget = async (id, reload = true) => {
    const row = document.querySelector(`.btn-save-target[data-id="${id}"]`).closest('tr');
    const target = {
        id: id === 'new' ? null : id,
        username: row.querySelector('.target-user').value,
        enabled: row.dataset.enabled !== '0'
    };
    await authFetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(target)
    });
    if (reload) loadTargets();
};

const toggleTarget = async (btnOrId) => {
    const toggle = typeof btnOrId === 'string'
        ? document.querySelector(`#targets-list .auto-toggle-target[data-id="${btnOrId}"]`)
        : btnOrId;
    if (!toggle) return;
    const row = toggle.closest('tr');
    const enabled = row.dataset.enabled !== '1';
    row.dataset.enabled = enabled ? '1' : '0';
    toggle.classList.toggle('enabled', enabled);
    toggle.classList.toggle('disabled', !enabled);
    toggle.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    toggle.setAttribute('title', enabled ? 'Automation enabled' : 'Automation disabled');
    toggle.textContent = enabled ? '👁' : '🚫';

    const id = toggle.dataset.id;
    const target = targetsData.find((t) => t.id === id);
    if (target) target.enabled = enabled;

    if (id !== 'new') {
        await saveTarget(id, false);
    }
};

const deleteTarget = async (id) => {
    if (confirm('Delete target?')) {
        await authFetch(`/api/targets/${id}`, { method: 'DELETE' });
        loadTargets();
    }
};

document.getElementById('btn-add-target').onclick = () => {
    pendingNewTarget = true;
    targetsPage = 1;
    renderTargetsPage();
};

document.getElementById('btn-toggle-all-targets')?.addEventListener('click', async () => {
    const nextEnabled = !allTargetsEnabled;
    if (targetsData.length === 0) return;

    for (const t of targetsData) {
        t.enabled = nextEnabled;
    }

    const rows = document.querySelectorAll('#targets-list tr');
    rows.forEach(row => {
        row.dataset.enabled = nextEnabled ? '1' : '0';
        const btn = row.querySelector('.auto-toggle-target');
        if (btn) {
            btn.classList.toggle('enabled', nextEnabled);
            btn.classList.toggle('disabled', !nextEnabled);
            btn.setAttribute('aria-pressed', nextEnabled ? 'true' : 'false');
            btn.setAttribute('title', nextEnabled ? 'Automation enabled' : 'Automation disabled');
            btn.textContent = nextEnabled ? '👁' : '🚫';
        }
    });

    allTargetsEnabled = nextEnabled;
    updateGlobalTargetsToggle();

    const updates = targetsData.map(t => authFetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            id: t.id,
            username: t.username,
            enabled: t.enabled
        })
    }));

    await Promise.all(updates);
    loadTargets();
});

// Settings Management
const loadSettings = async () => {
    const res = await authFetch('/api/settings');
    const settings = await res.json();
    if (settings.tg_token) document.getElementById('tg-token').value = settings.tg_token;
    if (settings.tg_chats) document.getElementById('tg-chats').value = settings.tg_chats;
    const headlessToggle = document.getElementById('cookie-headless');
    if (headlessToggle) headlessToggle.checked = normalizeEnabled(settings.cookie_headless);

    const msgEnabled = document.getElementById('msg-enabled');
    if (msgEnabled) msgEnabled.checked = settings.msg_enabled === '1' || settings.msg_enabled === 1 || settings.msg_enabled === true;
    const msgMin = document.getElementById('msg-min-minutes');
    if (msgMin && settings.msg_min_minutes !== undefined) msgMin.value = settings.msg_min_minutes;
    const msgMax = document.getElementById('msg-max-minutes');
    if (msgMax && settings.msg_max_minutes !== undefined) msgMax.value = settings.msg_max_minutes;
    const msgTexts = document.getElementById('msg-texts');
    if (msgTexts) msgTexts.value = settings.msg_texts || '';
};

document.getElementById('btn-save-settings').onclick = async () => {
    const msgEnabled = document.getElementById('msg-enabled');
    const msgMin = document.getElementById('msg-min-minutes');
    const msgMax = document.getElementById('msg-max-minutes');

    let minMinutes = parseInt(msgMin?.value || '2', 10);
    let maxMinutes = parseInt(msgMax?.value || '5', 10);
    if (!Number.isFinite(minMinutes)) minMinutes = 2;
    if (!Number.isFinite(maxMinutes)) maxMinutes = 5;
    minMinutes = Math.max(2, minMinutes);
    maxMinutes = Math.max(2, maxMinutes);
    if (minMinutes > maxMinutes) {
        const swap = minMinutes;
        minMinutes = maxMinutes;
        maxMinutes = swap;
    }

    const data = {
        tg_token: document.getElementById('tg-token').value,
        tg_chats: document.getElementById('tg-chats').value,
        msg_enabled: msgEnabled?.checked ? '1' : '0',
        msg_min_minutes: String(minMinutes),
        msg_max_minutes: String(maxMinutes)
    };
    const headlessToggle = document.getElementById('cookie-headless');
    if (headlessToggle) data.cookie_headless = headlessToggle.checked ? '1' : '0';
    const res = await authFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (res.ok) alert('Settings saved successfully!');
};

document.getElementById('btn-save-messages')?.addEventListener('click', async () => {
    const msgTexts = document.getElementById('msg-texts');
    const data = { msg_texts: msgTexts?.value || '' };
    const res = await authFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (res.ok) alert('Messages saved successfully!');
});

const cookieHeadlessToggle = document.getElementById('cookie-headless');
if (cookieHeadlessToggle) {
    cookieHeadlessToggle.onchange = async () => {
        await authFetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cookie_headless: cookieHeadlessToggle.checked ? '1' : '0' })
        });
    };
}

const loadEmployees = async () => {
    const list = document.getElementById('employees-list');
    if (!list) return;
    const res = await authFetch('/api/employees');
    if (!res.ok) return;
    const employees = await res.json();
    list.innerHTML = '';

    if (!employees.length) {
        list.innerHTML = '<div class="employee-empty">No employee accounts yet.</div>';
        return;
    }

    employees.forEach(emp => {
        const card = document.createElement('div');
        card.className = 'employee-card';
        card.innerHTML = `
            <div class="employee-header">
                <span class="employee-name">${emp.username}</span>
                <span class="employee-role">${emp.role || 'employee'}</span>
            </div>
            <div class="employee-actions">
                <button class="btn btn-primary btn-sm btn-emp-edit" data-id="${emp.id}">Edit</button>
                <button class="btn btn-secondary btn-sm btn-emp-reset" data-id="${emp.id}">Reset</button>
                <button class="btn btn-danger btn-sm btn-emp-delete" data-id="${emp.id}">Delete</button>
            </div>
        `;
        list.appendChild(card);
    });

    list.querySelectorAll('.btn-emp-edit').forEach(btn => {
        btn.onclick = () => editEmployee(btn.dataset.id);
    });
    list.querySelectorAll('.btn-emp-reset').forEach(btn => {
        btn.onclick = () => resetEmployeePassword(btn.dataset.id);
    });
    list.querySelectorAll('.btn-emp-delete').forEach(btn => {
        btn.onclick = () => deleteEmployee(btn.dataset.id);
    });
};

const createEmployee = async () => {
    const usernameInput = document.getElementById('employee-username');
    const passwordInput = document.getElementById('employee-password');
    const username = usernameInput.value.trim();
    const password = passwordInput.value.trim();
    if (!username || !password) {
        alert('Enter username and password.');
        return;
    }
    const res = await authFetch('/api/employees', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
    });
    if (res.ok) {
        usernameInput.value = '';
        passwordInput.value = '';
        loadEmployees();
        return;
    }
    const data = await res.json();
    alert(data.error || 'Failed to create employee.');
};

const editEmployee = async (id) => {
    const username = prompt('New username:');
    if (!username) return;
    const res = await authFetch(`/api/employees/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username })
    });
    if (res.ok) {
        loadEmployees();
        return;
    }
    const data = await res.json();
    alert(data.error || 'Failed to update employee.');
};

const resetEmployeePassword = async (id) => {
    const password = prompt('New password (min 4 chars):');
    if (!password) return;
    const res = await authFetch(`/api/employees/${id}/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
    });
    if (res.ok) {
        alert('Password reset.');
        return;
    }
    const data = await res.json();
    alert(data.error || 'Failed to reset password.');
};

const deleteEmployee = async (id) => {
    if (!confirm('Delete this employee?')) return;
    const res = await authFetch(`/api/employees/${id}`, { method: 'DELETE' });
    if (res.ok) {
        loadEmployees();
        return;
    }
    const data = await res.json();
    alert(data.error || 'Failed to delete employee.');
};

document.getElementById('btn-add-employee')?.addEventListener('click', createEmployee);

document.getElementById('btn-change-password')?.addEventListener('click', async () => {
    const newPw  = document.getElementById('new-password').value.trim();
    const confPw = document.getElementById('confirm-password').value.trim();
    const msg    = document.getElementById('pw-msg');
    if (!newPw) { msg.textContent = 'Enter a new password.'; msg.style.color = '#f87171'; return; }
    if (newPw !== confPw) { msg.textContent = 'Passwords do not match.'; msg.style.color = '#f87171'; return; }
    const res = await authFetch('/api/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: newPw })
    });
    if (res.ok) {
        msg.textContent = '✓ Password changed! Please log in again.';
        msg.style.color = '#34d399';
        setTimeout(() => { redirectToLogin(); }, 1500);
    } else {
        const d = await res.json();
        msg.textContent = d.error || 'Error changing password.';
        msg.style.color = '#f87171';
    }
});

const loadAccounts = async () => {
    const res  = await authFetch('/api/accounts');
    const data = await res.json();
    accountsData = data.map(acc => ({ ...acc, enabled: normalizeEnabled(acc.enabled) }));
    if (els.totalAccounts) els.totalAccounts.textContent = accountsData.length;
    renderAccountsPage();
};

const renderAccountsPage = () => {
    const list = document.getElementById('accounts-list');
    if (!list) return;

    const totalPages = Math.max(1, Math.ceil(accountsData.length / ACCOUNTS_PAGE_SIZE));
    allAccountsEnabled = accountsData.length ? accountsData.every(acc => acc.enabled) : true;
    updateGlobalAccountsToggle();
    if (accountsPage > totalPages) accountsPage = totalPages;

    list.innerHTML = '';

    if (pendingNewAccount && accountsPage === 1) {
        const row = document.createElement('tr');
        row.dataset.username = '';
        row.dataset.password = '';
        row.dataset.enabled = '1';
        row.innerHTML = `
            <td class="auto-cell">
                <button class="auto-toggle auto-toggle-account enabled" data-id="new" type="button" aria-pressed="true" title="Automation enabled">👁</button>
            </td>
            <td><textarea placeholder="1 proxy per line" class="acc-proxies"></textarea></td>
            <td><textarea placeholder='Paste JSON cookies' class="acc-cookies"></textarea></td>
            <td>
                <div class="table-actions">
                    <button class="btn btn-primary btn-sm btn-save-acc" data-id="new">Save</button>
                </div>
            </td>
        `;
        list.appendChild(row);
    }

    const start = (accountsPage - 1) * ACCOUNTS_PAGE_SIZE;
    const pageItems = accountsData.slice(start, start + ACCOUNTS_PAGE_SIZE);
    pageItems.forEach(acc => {
        const row = document.createElement('tr');
        const enabled = acc.enabled;
        row.dataset.username = acc.username || '';
        row.dataset.password = acc.password || '';
        row.dataset.enabled = enabled ? '1' : '0';
        row.innerHTML = `
            <td class="auto-cell">
                <button class="auto-toggle auto-toggle-account ${enabled ? 'enabled' : 'disabled'}" data-id="${acc.id}" type="button" aria-pressed="${enabled ? 'true' : 'false'}" title="${enabled ? 'Automation enabled' : 'Automation disabled'}">${enabled ? '👁' : '🚫'}</button>
            </td>
            <td><textarea data-id="${acc.id}" class="acc-proxies" placeholder="1 proxy per line">${acc.proxies || ''}</textarea></td>
            <td><textarea data-id="${acc.id}" class="acc-cookies" placeholder="Paste JSON cookies">${acc.cookies || ''}</textarea></td>
            <td>
                <div class="table-actions">
                    <button class="btn btn-primary btn-sm btn-save-acc" data-id="${acc.id}">Save</button>
                    <button class="btn btn-danger btn-sm btn-delete-acc" data-id="${acc.id}">🗑️</button>
                </div>
            </td>
        `;
        list.appendChild(row);
    });

    list.querySelectorAll('.auto-toggle-account').forEach(btn => { btn.onclick = () => toggleAccount(btn); });
    document.querySelectorAll('.btn-save-acc').forEach(btn => { btn.onclick = () => saveAccount(btn.dataset.id); });
    document.querySelectorAll('.btn-delete-acc').forEach(btn => { btn.onclick = () => deleteAccount(btn.dataset.id); });

    updateAccountsPagination(totalPages);
};

const updateAccountsPagination = (totalPages) => {
    const prev = document.getElementById('accounts-prev');
    const next = document.getElementById('accounts-next');
    const info = document.getElementById('accounts-page-info');
    if (prev) prev.disabled = accountsPage <= 1;
    if (next) next.disabled = accountsPage >= totalPages;
    if (info) info.textContent = `Page ${accountsPage} / ${totalPages}`;
};

const updateGlobalAccountsToggle = () => {
    const btn = document.getElementById('btn-toggle-all-accounts');
    if (!btn) return;
    btn.classList.toggle('disabled', !allAccountsEnabled);
    btn.textContent = allAccountsEnabled ? '👁 All' : '🚫 All';
    btn.title = allAccountsEnabled ? 'Disable all automation' : 'Enable all automation';
};


const saveAccount = async (id, reload = true) => {
    const row = document.querySelector(`.btn-save-acc[data-id="${id}"]`).closest('tr');
    const account = {
        id:       id === 'new' ? null : id,
        username: row.dataset.username || '',
        password: row.dataset.password || '',
        enabled:  row.dataset.enabled !== '0',
        proxies:  row.querySelector('.acc-proxies').value,
        cookies:  row.querySelector('.acc-cookies').value
    };
    const res = await authFetch('/api/accounts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(account)
    });
    if (res.ok) {
        pendingNewAccount = false;
        if (reload) loadAccounts();
    }
};

const toggleAccount = async (btnOrId) => {
    const toggle = typeof btnOrId === 'string'
        ? document.querySelector(`#accounts-list .auto-toggle-account[data-id="${btnOrId}"]`)
        : btnOrId;
    if (!toggle) return;
    const row = toggle.closest('tr');
    const enabled = row.dataset.enabled !== '1';
    row.dataset.enabled = enabled ? '1' : '0';
    toggle.classList.toggle('enabled', enabled);
    toggle.classList.toggle('disabled', !enabled);
    toggle.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    toggle.setAttribute('title', enabled ? 'Automation enabled' : 'Automation disabled');
    toggle.textContent = enabled ? '👁' : '🚫';

    const id = toggle.dataset.id;
    const account = accountsData.find((acc) => acc.id === id);
    if (account) account.enabled = enabled;

    if (id !== 'new') {
        await saveAccount(id, false);
    }
};

const deleteAccount = async (id) => {
    if (!confirm('Are you sure?')) return;
    const res = await authFetch(`/api/accounts/${id}`, { method: 'DELETE' });
    if (res.ok) loadAccounts();
};

document.getElementById('btn-add-account').onclick = () => {
    pendingNewAccount = true;
    accountsPage = 1;
    renderAccountsPage();
};

document.getElementById('btn-toggle-all-accounts')?.addEventListener('click', async () => {
    const nextEnabled = !allAccountsEnabled;
    if (accountsData.length === 0) return;

    for (const acc of accountsData) {
        acc.enabled = nextEnabled;
    }

    const rows = document.querySelectorAll('#accounts-list tr');
    rows.forEach(row => {
        row.dataset.enabled = nextEnabled ? '1' : '0';
        const btn = row.querySelector('.auto-toggle-account');
        if (btn) {
            btn.classList.toggle('enabled', nextEnabled);
            btn.classList.toggle('disabled', !nextEnabled);
            btn.setAttribute('aria-pressed', nextEnabled ? 'true' : 'false');
            btn.setAttribute('title', nextEnabled ? 'Automation enabled' : 'Automation disabled');
            btn.textContent = nextEnabled ? '👁' : '🚫';
        }
    });

    allAccountsEnabled = nextEnabled;
    updateGlobalAccountsToggle();

    const res = await authFetch('/api/accounts/bulk-enabled', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: nextEnabled })
    });
    if (!res.ok) {
        alert('Failed to update account automation. Please try again.');
        await loadAccounts();
        return;
    }
    await loadAccounts();
});

document.getElementById('accounts-prev')?.addEventListener('click', () => {
    if (accountsPage > 1) {
        accountsPage -= 1;
        pendingNewAccount = false;
        renderAccountsPage();
    }
});

document.getElementById('accounts-next')?.addEventListener('click', () => {
    const totalPages = Math.max(1, Math.ceil(accountsData.length / ACCOUNTS_PAGE_SIZE));
    if (accountsPage < totalPages) {
        accountsPage += 1;
        pendingNewAccount = false;
        renderAccountsPage();
    }
});

document.getElementById('targets-prev')?.addEventListener('click', () => {
    if (targetsPage > 1) {
        targetsPage -= 1;
        pendingNewTarget = false;
        renderTargetsPage();
    }
});

document.getElementById('targets-next')?.addEventListener('click', () => {
    const totalPages = Math.max(1, Math.ceil(targetsData.length / TARGETS_PAGE_SIZE));
    if (targetsPage < totalPages) {
        targetsPage += 1;
        pendingNewTarget = false;
        renderTargetsPage();
    }
});
