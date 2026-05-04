/**
 * Auto-Save & Anti-Cheat Module for CEFR Test Environment.
 *
 * Usage: Include this script on test pages. It reads configuration from
 * the element with id="autosave-config":
 *   data-attempt-id  — UserAttempt PK
 *   data-autosave-url — POST endpoint for saving drafts
 *   data-tabblur-url  — POST endpoint for logging tab blurs
 *
 * Features:
 *  1. Auto-save answers to server every 20 seconds
 *  2. localStorage fallback when offline
 *  3. Online/offline status indicator
 *  4. Anti-cheat: disable right-click, copy shortcuts
 *  5. Tab blur tracking with warning modals
 */
(function () {
    'use strict';

    const config = document.getElementById('autosave-config');
    if (!config) return;

    const ATTEMPT_ID = config.dataset.attemptId;
    const AUTOSAVE_URL = config.dataset.autosaveUrl;
    const TABBLUR_URL = config.dataset.tabblurUrl;
    const STORAGE_KEY = `cefr_draft_${ATTEMPT_ID}`;
    const SAVE_INTERVAL = 20000; // 20 seconds

    const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value
        || document.cookie.match(/csrftoken=([^;]+)/)?.[1]
        || '';

    // ── Status Indicator ────────────────────────────────────────────
    const indicator = document.createElement('div');
    indicator.id = 'save-indicator';
    indicator.className = 'fixed bottom-4 right-4 z-50 flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-extrabold uppercase tracking-widest transition-all duration-300 opacity-0 pointer-events-none';
    indicator.style.cssText = 'background:#1A2F35;border:2px solid #2B4D56;box-shadow:0 4px 20px rgba(0,0,0,0.3);';
    document.body.appendChild(indicator);

    let hideTimeout = null;

    function showIndicator(state) {
        clearTimeout(hideTimeout);
        indicator.classList.remove('opacity-0', 'pointer-events-none');

        const states = {
            saving: { icon: 'ph-spinner', color: '#1CB0F6', text: 'Saving...', spin: true },
            saved:  { icon: 'ph-check-circle', color: '#58CC02', text: 'Saved', spin: false },
            offline:{ icon: 'ph-wifi-slash', color: '#FFC800', text: 'Offline', spin: false },
            error:  { icon: 'ph-warning', color: '#FF4B4B', text: 'Save failed', spin: false },
        };
        const s = states[state] || states.error;
        indicator.innerHTML = `<i class="ph-fill ${s.icon} text-base ${s.spin ? 'animate-spin' : ''}" style="color:${s.color}"></i><span style="color:${s.color}">${s.text}</span>`;
        indicator.style.borderColor = s.color + '40';

        if (state === 'saved') {
            hideTimeout = setTimeout(() => {
                indicator.classList.add('opacity-0', 'pointer-events-none');
            }, 2500);
        }
    }

    // ── Collect Form Answers ────────────────────────────────────────
    function collectAnswers() {
        const answers = {};
        const form = document.getElementById('test-form');
        if (!form) return answers;

        // Radio buttons (multiple choice)
        form.querySelectorAll('input[type="radio"]:checked').forEach(r => {
            answers[r.name] = r.value;
        });

        // Hidden select elements (custom dropdown writes to hidden select)
        form.querySelectorAll('select').forEach(sel => {
            if (sel.name && sel.value) answers[sel.name] = sel.value;
        });

        // Text inputs (fill-in-the-blank)
        form.querySelectorAll('input[type="text"]').forEach(inp => {
            if (inp.name && inp.value) answers[inp.name] = inp.value;
        });

        // Textareas (writing tasks)
        form.querySelectorAll('textarea').forEach(ta => {
            if (ta.name && ta.value) answers[ta.name] = ta.value;
        });

        return answers;
    }

    // ── Restore Answers into Form ───────────────────────────────────
    function restoreAnswers(answers) {
        if (!answers || typeof answers !== 'object') return;
        const form = document.getElementById('test-form');
        if (!form) return;

        Object.entries(answers).forEach(([name, value]) => {
            if (!value) return;

            // Radio buttons
            const radio = form.querySelector(`input[type="radio"][name="${name}"][value="${value}"]`);
            if (radio) {
                radio.checked = true;
                // Trigger visual update for custom choice-option styling
                radio.dispatchEvent(new Event('change', { bubbles: true }));
                return;
            }

            // Select elements
            const select = form.querySelector(`select[name="${name}"]`);
            if (select) {
                select.value = value;
                select.dispatchEvent(new Event('change', { bubbles: true }));
                // Also update custom dropdown trigger text if present
                const wrapper = select.closest('.relative');
                if (wrapper) {
                    const trigger = wrapper.querySelector('span.truncate, span.text-white, span.select-none');
                    if (trigger) {
                        const selectedOpt = select.options[select.selectedIndex];
                        if (selectedOpt) trigger.textContent = selectedOpt.text;
                    }
                }
                return;
            }

            // Text inputs
            const textInput = form.querySelector(`input[type="text"][name="${name}"]`);
            if (textInput) {
                textInput.value = value;
                return;
            }

            // Textareas
            const textarea = form.querySelector(`textarea[name="${name}"]`);
            if (textarea) {
                textarea.value = value;
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
            }
        });
    }

    // ── Server Save ─────────────────────────────────────────────────
    async function saveToServer(answers) {
        const resp = await fetch(AUTOSAVE_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ answers }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
    }

    // ── localStorage Fallback ───────────────────────────────────────
    function saveToLocal(answers) {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(answers));
        } catch (e) { /* quota exceeded — ignore */ }
    }

    function loadFromLocal() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
    }

    function clearLocal() {
        localStorage.removeItem(STORAGE_KEY);
    }

    // ── Main Auto-Save Cycle ────────────────────────────────────────
    let lastSavedJson = '';

    async function autoSave() {
        const answers = collectAnswers();
        const currentJson = JSON.stringify(answers);

        // Skip if nothing changed
        if (currentJson === lastSavedJson) return;

        if (!navigator.onLine) {
            saveToLocal(answers);
            showIndicator('offline');
            return;
        }

        showIndicator('saving');
        try {
            await saveToServer(answers);
            lastSavedJson = currentJson;
            clearLocal();
            showIndicator('saved');
        } catch (e) {
            // Network failure — save locally
            saveToLocal(answers);
            showIndicator('offline');
        }
    }

    // Sync local draft when coming back online
    async function syncLocalDraft() {
        const local = loadFromLocal();
        if (!local || !navigator.onLine) return;

        showIndicator('saving');
        try {
            await saveToServer(local);
            clearLocal();
            lastSavedJson = JSON.stringify(local);
            showIndicator('saved');
        } catch (e) {
            showIndicator('error');
        }
    }

    // ── Tab Blur Tracking ───────────────────────────────────────────
    let blurTime = null;
    let blurCount = 0;

    function onTabBlur() {
        blurTime = Date.now();
        blurCount++;
    }

    function onTabFocus() {
        if (!blurTime) return;
        const duration = (Date.now() - blurTime) / 1000;
        blurTime = null;

        // Log to server (fire-and-forget)
        if (navigator.onLine && TABBLUR_URL) {
            fetch(TABBLUR_URL, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: JSON.stringify({ duration }),
            }).catch(() => {});
        }

        // Show warning modal
        if (typeof DuoModal !== 'undefined') {
            let msg = '⚠️ You left the test tab! This event has been recorded.';
            if (blurCount >= 3) {
                msg = `⚠️ WARNING: You have left the test tab ${blurCount} times. All events are logged and visible to your mentor.`;
            }
            DuoModal.alert(msg);
        }

        // Update counter badge if present
        const badge = document.getElementById('blur-counter');
        if (badge) {
            badge.textContent = blurCount;
            badge.closest('.blur-tracker')?.classList.remove('hidden');
        }
    }

    // ── Anti-Cheat: Text Protection ─────────────────────────────────
    function setupTextProtection() {
        // Disable right-click context menu
        document.addEventListener('contextmenu', e => {
            // Allow right-click in textareas for writing tests
            if (e.target.tagName === 'TEXTAREA') return;
            e.preventDefault();
        });

        // Disable copy/paste/save keyboard shortcuts
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && ['c', 'u', 's'].includes(e.key.toLowerCase())) {
                // Allow Ctrl+C in textareas for writing tests
                if (e.key.toLowerCase() === 'c' && e.target.tagName === 'TEXTAREA') return;
                e.preventDefault();
            }
            // Disable F12 developer tools
            if (e.key === 'F12') e.preventDefault();
            // Disable Ctrl+Shift+I (DevTools)
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'i') e.preventDefault();
        });
    }

    // ── Initialize ──────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        // Restore drafts: prefer server-side (passed via template), fallback to localStorage
        const serverDraftEl = document.getElementById('server-draft-data');
        const serverDraft = serverDraftEl ? JSON.parse(serverDraftEl.textContent || '{}') : {};
        const localDraft = loadFromLocal();

        // Use whichever has more data (local might be newer if saved while offline)
        const serverKeys = Object.keys(serverDraft).length;
        const localKeys = localDraft ? Object.keys(localDraft).length : 0;
        const draftToRestore = localKeys > serverKeys ? localDraft : serverDraft;

        if (Object.keys(draftToRestore).length > 0) {
            // Small delay to let custom UI components initialize first
            setTimeout(() => restoreAnswers(draftToRestore), 100);
        }

        // Start auto-save cycle
        setInterval(autoSave, SAVE_INTERVAL);

        // Also save on every input change (debounced by the interval)
        // and immediately before page unload
        window.addEventListener('beforeunload', () => {
            const answers = collectAnswers();
            saveToLocal(answers);
            // Attempt synchronous beacon save with CSRF token in body
            if (navigator.onLine && AUTOSAVE_URL) {
                const formData = new FormData();
                formData.append('csrfmiddlewaretoken', csrfToken);
                formData.append('payload', JSON.stringify({ answers }));
                navigator.sendBeacon(AUTOSAVE_URL, formData);
            }
        });

        // Online/offline listeners
        window.addEventListener('online', () => syncLocalDraft());
        window.addEventListener('offline', () => showIndicator('offline'));

        // Tab blur tracking
        window.addEventListener('blur', onTabBlur);
        window.addEventListener('focus', onTabFocus);

        // Anti-cheat text protection
        setupTextProtection();
    });
})();
