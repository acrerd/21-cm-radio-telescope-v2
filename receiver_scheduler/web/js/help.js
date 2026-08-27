// Contextual help. Any element carrying a data-help attribute shows its text
// in a floating box when help tips are on. Ported from the controller page's
// hover-help so the two pages behave the same way. The toggle state is kept in
// localStorage (per browser), defaulting to on for a first-time student.

let hoverHelpEnabled = true;
let hoverHelpTarget = null;
let hoverHelpTimer = null;

function _helpBox() { return document.getElementById('hover-help'); }

function hideHoverHelp() {
    if (hoverHelpTimer) { clearTimeout(hoverHelpTimer); hoverHelpTimer = null; }
    hoverHelpTarget = null;
    const b = _helpBox();
    if (b) b.style.display = 'none';
}

function showHoverHelp(el) {
    if (!hoverHelpEnabled || !el || el.disabled) return;
    const text = el.getAttribute('data-help');
    const b = _helpBox();
    if (!text || !b) return;
    b.textContent = text;
    b.style.display = 'block';
    // Place it just below the control, clamped inside the viewport; flip above
    // when there is no room below.
    const r = el.getBoundingClientRect();
    const bw = b.offsetWidth, bh = b.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - bw - 12);
    let top = r.bottom + 8;
    if (top + bh > window.innerHeight - 8) top = Math.max(8, r.top - bh - 8);
    b.style.left = Math.max(8, left) + 'px';
    b.style.top = top + 'px';
}

function setHelpEnabled(on) {
    hoverHelpEnabled = !!on;
    try { localStorage.setItem('srtHelpTips', hoverHelpEnabled ? '1' : '0'); } catch (e) {}
    const cb = document.getElementById('helpTipsToggle');
    if (cb) cb.checked = hoverHelpEnabled;
    if (!hoverHelpEnabled) hideHoverHelp();
}

function initHoverHelp() {
    try {
        const saved = localStorage.getItem('srtHelpTips');
        if (saved !== null) hoverHelpEnabled = saved === '1';
    } catch (e) {}
    const cb = document.getElementById('helpTipsToggle');
    if (cb) cb.checked = hoverHelpEnabled;
    document.addEventListener('pointerover', e => {
        const el = e.target.closest('[data-help]');
        if (!el || el === hoverHelpTarget) return;
        hideHoverHelp();
        hoverHelpTarget = el;
        hoverHelpTimer = setTimeout(() => showHoverHelp(el), 500);
    });
    document.addEventListener('pointerout', e => {
        if (hoverHelpTarget && (!e.relatedTarget || !hoverHelpTarget.contains(e.relatedTarget))) hideHoverHelp();
    });
    document.addEventListener('scroll', hideHoverHelp, true);
}
