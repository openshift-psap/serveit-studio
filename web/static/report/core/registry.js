// registry.js — modular report tab registry (web/static/report/)
//
// Each report tab lives in its own file under report/tabs/ and registers
// itself here. One implementation serves BOTH the live report and the
// downloaded offline report — the differences (element id prefixes,
// download links vs embedded blobs) are absorbed by the ctx object from
// core/context.js.
//
// Tab shape:
//   ReportTabs.register({
//     id: 'pod-profiles',            // unique; also the subtab pane id
//     label: 'Pod Profiles',         // subtab bar label
//     icon: '&#128300;',             // subtab icon (HTML entity)
//     position: 'before:estimator',  // 'end' | 'before:<tabId>' | 'after:<tabId>'
//     build(data, ctx) -> html,      // section HTML ('' = hide the tab)
//     render(data, ctx)              // Plotly renders into ctx.chartId() ids
//   });
window.ReportTabs = (function() {
    const tabs = [];

    function register(tab) {
        if (!tab || !tab.id || typeof tab.build !== 'function') {
            console.warn('ReportTabs: invalid tab registration', tab);
            return;
        }
        const i = tabs.findIndex(t => t.id === tab.id);
        if (i >= 0) tabs[i] = tab; else tabs.push(tab);
    }

    function all() { return tabs.slice(); }
    function get(id) { return tabs.find(t => t.id === id) || null; }

    // Build one tab's section HTML and schedule its render onto the queue.
    // The queue item runs after the HTML is in the DOM (same contract as
    // the legacy chartQueue in charts.js).
    function buildTab(tab, data, ctx, queue) {
        let html = '';
        try {
            html = tab.build(data, ctx);
        } catch (e) {
            console.warn('ReportTabs: build failed for ' + tab.id, e);
            return '';
        }
        if (html && typeof tab.render === 'function' && queue) {
            queue.push(function() {
                try { tab.render(data, ctx); }
                catch (e) { console.warn('ReportTabs: render failed for ' + tab.id, e); }
            });
        }
        return html;
    }

    return { register: register, all: all, get: get, buildTab: buildTab };
})();
