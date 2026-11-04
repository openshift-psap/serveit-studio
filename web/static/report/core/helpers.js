// helpers.js — shared utilities for modular report tabs (web/static/report/)
window.ReportHelpers = (function() {

    function shortNode(node) {
        // "psap-fire-athena-bnfx9-worker-2-gpu-h200-k6qsd" → "k6qsd"
        return (node || '?').split('-').pop();
    }

    function truncate(s, n) {
        s = s || '';
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // Left margin sized to the longest label so y-axis tick labels never
    // clip or collide with the plot area. Plotly's automargin stays on as
    // a safety net, but an explicit computed margin is deterministic
    // regardless of the page's inherited font (the live report inherits
    // the wide Red Hat Display font, which consumed fixed margins whole).
    //   labels    — the y tick labels
    //   pxPerChar — conservative per-character width of the tick font
    //               (monospace 12px ≈ 8, monospace 10px ≈ 6.6, sans 12px ≈ 7.5)
    //   pad       — extra room for the axis line and breathing space
    function leftMarginFor(labels, pxPerChar, pad) {
        let maxLen = 0;
        (labels || []).forEach(function(l) { maxLen = Math.max(maxLen, (l || '').length); });
        return Math.min(420, Math.ceil(maxLen * pxPerChar) + (pad == null ? 40 : pad));
    }

    // Longest label in a list (for hover text truncation decisions)
    function maxLabelLen(labels) {
        let m = 0;
        (labels || []).forEach(function(l) { m = Math.max(m, (l || '').length); });
        return m;
    }

    // The standard tick fonts for report charts — monospace keeps pod
    // names, function names and config labels metric-stable across the
    // live page and the offline report.
    var MONO_12 = { size: 12, family: "'Red Hat Mono', 'JetBrains Mono', monospace" };
    var MONO_10 = { size: 10, family: "'Red Hat Mono', 'JetBrains Mono', monospace" };
    var MONO_9 = { size: 9, family: "'Red Hat Mono', 'JetBrains Mono', monospace" };

    // Color for an EPP/vLLM CPU% value (hot thresholds shared by all
    // profile charts so colors mean the same thing everywhere)
    function cpuColor(pct) {
        return pct > 50 ? '#dc2626' : pct > 20 ? '#d97706' : '#0891b2';
    }

    return {
        shortNode: shortNode,
        truncate: truncate,
        leftMarginFor: leftMarginFor,
        maxLabelLen: maxLabelLen,
        cpuColor: cpuColor,
        MONO_12: MONO_12,
        MONO_10: MONO_10,
        MONO_9: MONO_9
    };
})();
