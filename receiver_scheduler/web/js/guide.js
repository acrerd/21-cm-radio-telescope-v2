// The Guide tab: an in-page handbook for students. Navigation only - the
// content itself is static HTML in index.html under #tab-guide, so it is
// editable with a browser refresh and checked by test_page_structure.py.

function showGuide(section) {
    document.querySelectorAll('#tab-guide .guide-section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('#tab-guide .guide-nav-item').forEach(n => n.classList.remove('active'));
    const sec = document.getElementById('guide-' + section);
    if (sec) sec.classList.add('active');
    const nav = document.querySelector(`#tab-guide .guide-nav-item[data-guide="${section}"]`);
    if (nav) nav.classList.add('active');
    const pane = document.querySelector('#tab-guide .guide-content');
    if (pane) pane.scrollTop = 0;
}

// Jump to another tab from a link inside the guide, e.g. "open the Sun Scan
// tab". switchTab lives in shared.js and is already global.
function guideGoto(tab) {
    if (typeof switchTab === 'function') switchTab(tab);
}
