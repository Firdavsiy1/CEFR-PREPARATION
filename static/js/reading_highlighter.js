document.addEventListener('DOMContentLoaded', () => {
    const containers = document.querySelectorAll('.passage-content, .passage-text, .post-card p, .highlightable-text');
    if (!containers.length) return;

    const testContainer = document.querySelector('[data-test-id]');
    const autosaveConfig = document.getElementById('autosave-config');

    const testId = (testContainer && testContainer.getAttribute('data-test-id')) || (autosaveConfig && autosaveConfig.getAttribute('data-attempt-id'));
    if (!testId) return;

    const partId = (testContainer && testContainer.getAttribute('data-part-id')) || 'all';
    const storageKey = `exam_highlights_${testId}_${partId}`;

    let highlights = JSON.parse(localStorage.getItem(storageKey) || '[]');

    const createTooltip = document.createElement('div');
    createTooltip.className = 'highlight-create-tooltip absolute z-50 flex items-center gap-1 rounded-xl border border-[#2B4D56] bg-[#1A2F35] px-2 py-1.5 shadow-2xl';
    createTooltip.style.display = 'none';
    createTooltip.innerHTML = [
        '<button type="button" class="hl-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-yellow" style="background:#fde047" title="Yellow"></button>',
        '<button type="button" class="hl-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-green" style="background:#4ade80" title="Green"></button>',
        '<button type="button" class="hl-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-blue" style="background:#60a5fa" title="Blue"></button>',
        '<button type="button" class="hl-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-pink" style="background:#f472b6" title="Pink"></button>',
        '<div class="hl-dict-sep w-px h-5 bg-[#2B4D56] mx-0.5" style="display:none"></div>',
        '<button type="button" class="hl-dict-btn flex h-6 items-center gap-1.5 px-2 rounded-lg bg-[#1565C0]/30 border border-[#1899D6]/40 text-[#60a5fa] text-xs font-semibold hover:bg-[#1565C0]/50 transition-colors whitespace-nowrap" style="display:none" title="Add to Dictionary">',
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h13a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1zm-7 15H8v-2h4v2zm3-4H8v-2h7v2zm0-4H8V7h7v2z"/></svg>',
        '<span data-i18n="dict_add_tooltip">В словарь</span>',
        '</button>'
    ].join('');
    document.body.appendChild(createTooltip);

    const dictBtn = createTooltip.querySelector('.hl-dict-btn');
    const dictSep = createTooltip.querySelector('.hl-dict-sep');

    const editTooltip = document.createElement('div');
    editTooltip.className = 'highlight-edit-tooltip absolute z-50 flex items-center gap-1 rounded-xl border border-[#2B4D56] bg-[#1A2F35] px-2 py-1.5 shadow-2xl';
    editTooltip.style.display = 'none';
    editTooltip.innerHTML = [
        '<button type="button" class="hl-edit-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-yellow" style="background:#fde047"></button>',
        '<button type="button" class="hl-edit-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-green" style="background:#4ade80"></button>',
        '<button type="button" class="hl-edit-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-blue" style="background:#60a5fa"></button>',
        '<button type="button" class="hl-edit-color-btn h-5 w-5 rounded-full transition-transform hover:scale-110" data-color="hl-pink" style="background:#f472b6"></button>',
        '<div class="w-px h-5 bg-[#2B4D56] mx-0.5"></div>',
        '<button type="button" class="hl-erase-btn flex h-6 w-6 items-center justify-center rounded-lg border border-[#b53b3b]/50 text-[#ff6b6b] hover:bg-[#b53b3b]/20 transition-colors" title="Remove highlight"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20H7L3 16l10-10 7 7-3.5 3.5"/><path d="m6.5 17.5 3-3"/></svg></button>'
    ].join('');
    document.body.appendChild(editTooltip);

    const style = document.createElement('style');
    style.innerHTML = `
        .highlighted-text {
            color: inherit;
            cursor: pointer;
            transition: opacity 0.15s;
        }
        .highlighted-text:hover {
            opacity: 0.85;
        }
        .hl-yellow { background: rgba(253, 224, 71, 0.4); border-bottom: 2px solid rgba(253, 224, 71, 0.85); }
        .hl-green  { background: rgba(74, 222, 128, 0.4); border-bottom: 2px solid rgba(74, 222, 128, 0.85); }
        .hl-blue   { background: rgba(96, 165, 250, 0.4); border-bottom: 2px solid rgba(96, 165, 250, 0.85); }
        .hl-pink   { background: rgba(244, 114, 182, 0.4); border-bottom: 2px solid rgba(244, 114, 182, 0.85); }
    `;
    document.head.appendChild(style);

    let pendingSelection = null;
    let pendingWord = null;
    let activeHighlightId = null;

    function saveHighlights() {
        localStorage.setItem(storageKey, JSON.stringify(highlights));
    }

    function getAbsoluteOffset(node, offset, container) {
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        let charCount = 0;

        while (walker.nextNode()) {
            const current = walker.currentNode;
            if (current === node) return charCount + offset;
            charCount += current.nodeValue.length;
        }
        return -1;
    }

    function getTextNodes(container) {
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        return nodes;
    }

    function unwrapExistingMarks() {
        document.querySelectorAll('.highlighted-text').forEach((mark) => {
            const parent = mark.parentNode;
            while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
            parent.removeChild(mark);
        });
    }

    function renderHighlights() {
        unwrapExistingMarks();
        containers.forEach((container) => container.normalize());

        const ordered = [...highlights].sort((a, b) => b.start - a.start);

        ordered.forEach((item) => {
            const container = containers[item.containerIndex];
            if (!container) return;

            const nodes = getTextNodes(container);
            let charCount = 0;
            let startNode = null;
            let endNode = null;
            let startOffset = 0;
            let endOffset = 0;

            for (const node of nodes) {
                const len = node.nodeValue.length;

                if (!startNode && charCount + len > item.start) {
                    startNode = node;
                    startOffset = item.start - charCount;
                }
                if (!endNode && charCount + len >= item.end) {
                    endNode = node;
                    endOffset = item.end - charCount;
                    break;
                }
                charCount += len;
            }

            if (!startNode || !endNode) return;

            try {
                const range = document.createRange();
                range.setStart(startNode, startOffset);
                range.setEnd(endNode, endOffset);

                const mark = document.createElement('mark');
                mark.className = `highlighted-text ${item.color || 'hl-yellow'}`;
                mark.dataset.highlightId = item.id;
                range.surroundContents(mark);
            } catch (e) {
                // Skip broken range if DOM has changed.
            }
        });
    }

    function hideCreateTooltip() {
        createTooltip.style.display = 'none';
        pendingSelection = null;
        pendingWord = null;
    }

    function hideEditTooltip() {
        editTooltip.style.display = 'none';
        activeHighlightId = null;
    }

    document.addEventListener('selectionchange', () => {
        const selection = window.getSelection();
        if (!selection.rangeCount || selection.isCollapsed) {
            hideCreateTooltip();
            return;
        }

        const range = selection.getRangeAt(0);
        let foundContainer = null;
        let containerIndex = -1;

        for (let i = 0; i < containers.length; i += 1) {
            if (containers[i].contains(range.commonAncestorContainer)) {
                foundContainer = containers[i];
                containerIndex = i;
                break;
            }
        }

        if (!foundContainer) {
            hideCreateTooltip();
            return;
        }

        const start = getAbsoluteOffset(range.startContainer, range.startOffset, foundContainer);
        const end = getAbsoluteOffset(range.endContainer, range.endOffset, foundContainer);
        if (start === -1 || end === -1 || start === end) {
            hideCreateTooltip();
            return;
        }

        pendingSelection = {
            id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
            containerIndex,
            start: Math.min(start, end),
            end: Math.max(start, end)
        };

        const selectedText = selection.toString().trim();
        const isSingleWord = /^[a-zA-Z'-]+$/.test(selectedText) && selectedText.length <= 40;
        pendingWord = isSingleWord ? selectedText.toLowerCase() : null;
        const showDict = isSingleWord && window.DICT_LOOKUP_URL && window.DICT_CSRF;
        dictSep.style.display = showDict ? 'block' : 'none';
        dictBtn.style.display = showDict ? 'flex' : 'none';

        const rect = range.getBoundingClientRect();
        createTooltip.style.display = 'flex';
        createTooltip.style.left = `${rect.left + rect.width / 2 - createTooltip.offsetWidth / 2 + window.scrollX}px`;
        createTooltip.style.top = `${rect.top - 45 + window.scrollY}px`;

        hideEditTooltip();
    });

    createTooltip.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    createTooltip.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();

        // Dictionary button
        const dictBtnEl = e.target.closest('.hl-dict-btn');
        if (dictBtnEl && pendingWord) {
            const wordToAdd = pendingWord;
            // Close tooltip immediately — don't make user wait for API
            window.getSelection().removeAllRanges();
            hideCreateTooltip();

            // Show "looking up" toast
            const tlang = localStorage.getItem('cefr_language') || 'ru';
            const lookingUpText = (window._i18n && window._i18n[tlang] && window._i18n[tlang]['dict_looking_up']) || 'Ищем слово...';
            const loadingToast = document.createElement('div');
            loadingToast.className = 'fixed bottom-6 left-1/2 -translate-x-1/2 z-[400] bg-duo-surface border-2 border-[#2B4D56] text-[#60a5fa] font-black text-sm px-6 py-3 rounded-2xl shadow-xl animate-slide-up flex items-center gap-2';
            loadingToast.innerHTML = `<svg class="animate-spin h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg><span>${lookingUpText}</span>`;
            document.body.appendChild(loadingToast);

            fetch(window.DICT_LOOKUP_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.DICT_CSRF },
                body: JSON.stringify({ word: wordToAdd, source: 'dblclick' }),
            }).then((r) => r.json()).then((data) => {
                loadingToast.remove();
                const resultToast = document.createElement('div');
                resultToast.className = 'fixed bottom-6 left-1/2 -translate-x-1/2 z-[400] bg-duo-surface border-2 border-duo-green/30 text-duo-green font-black text-sm px-6 py-3 rounded-2xl shadow-xl animate-slide-up';
                const msgKey = data.created ? 'dict_added_success' : 'dict_already_added';
                const fallback = data.created ? 'Добавлено в словарь! ✓' : 'Уже в словаре ✓';
                resultToast.textContent = (window._i18n && window._i18n[tlang] && window._i18n[tlang][msgKey]) || fallback;
                document.body.appendChild(resultToast);
                setTimeout(() => { resultToast.style.opacity = '0'; resultToast.style.transition = 'opacity 0.3s'; setTimeout(() => resultToast.remove(), 300); }, 2500);
            }).catch(() => {
                loadingToast.remove();
            });
            return;
        }

        const colorBtn = e.target.closest('.hl-color-btn');
        if (!colorBtn || !pendingSelection) return;

        highlights.push({
            ...pendingSelection,
            color: colorBtn.dataset.color || 'hl-yellow'
        });
        saveHighlights();
        window.getSelection().removeAllRanges();
        hideCreateTooltip();
        renderHighlights();
    });

    document.addEventListener('click', (e) => {
        if (e.target.closest('.highlight-edit-tooltip')) return;

        const mark = e.target.closest('.highlighted-text');
        if (!mark) {
            hideEditTooltip();
            return;
        }

        activeHighlightId = mark.dataset.highlightId;
        const rect = mark.getBoundingClientRect();
        editTooltip.style.display = 'flex';
        editTooltip.style.left = `${rect.left + rect.width / 2 - editTooltip.offsetWidth / 2 + window.scrollX}px`;
        editTooltip.style.top = `${rect.top - 42 + window.scrollY}px`;
    });

    editTooltip.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    editTooltip.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!activeHighlightId) return;

        // Eraser button
        if (e.target.closest('.hl-erase-btn')) {
            highlights = highlights.filter((item) => item.id !== activeHighlightId);
            saveHighlights();
            hideEditTooltip();
            renderHighlights();
            return;
        }

        // Color change button
        const colorBtn = e.target.closest('.hl-edit-color-btn');
        if (colorBtn) {
            const hl = highlights.find((item) => item.id === activeHighlightId);
            if (hl) {
                hl.color = colorBtn.dataset.color;
                saveHighlights();
                hideEditTooltip();
                renderHighlights();
            }
        }
    });

    document.querySelectorAll('form').forEach((form) => {
        form.addEventListener('submit', (e) => {
            const submitter = e.submitter;
            if (!submitter) return;

            const isFinish = submitter.value === 'submit' || submitter.textContent.toLowerCase().includes('finish');
            if (!isFinish) return;

            Object.keys(localStorage).forEach((key) => {
                if (key.startsWith(`exam_highlights_${testId}_`)) {
                    localStorage.removeItem(key);
                }
            });
        });
    });

    renderHighlights();
});