// context.js — live/download context for modular report tabs
//
// One tab implementation renders in both contexts; every context-dependent
// decision routes through this ctx object:
//   ctx.mode        — 'live' | 'download'
//   ctx.runId       — the optimization run id
//   ctx.chartId(b)  — fully-qualified element id for a chart base id
//   ctx.profileAnchor(testId, key, inner, title) — HTML <a> for a raw
//                     .prof download (live: API route; offline: embedded
//                     blob via dlProfile())
//   ctx.manifestAnchor(testId, type, inner, title) — same for YAML
//                     manifests (live: API route; offline: dlManifest())
window.ReportCtx = (function() {

    var ANCHOR_STYLE = 'color:#0e7490;text-decoration:none;font-size:11px;padding:3px 8px;' +
        'background:#ecfeff;border-radius:4px;border:1px solid #a5f3fc;display:inline-block;cursor:pointer;';

    function live(runId) {
        return {
            mode: 'live',
            runId: runId,
            chartId: function(base) {
                return base + (typeof _chartSuffix !== 'undefined' ? _chartSuffix : '');
            },
            profileAnchor: function(testId, key, inner, title) {
                return '<a href="/api/run/' + runId + '/config/' + encodeURIComponent(testId) +
                    '/profile/' + encodeURIComponent(key) + '" title="' + (title || key) + '"' +
                    ' style="' + ANCHOR_STYLE + '">' + inner + '</a>';
            },
            manifestAnchor: function(testId, type, inner, title) {
                return '<a href="/api/run/' + runId + '/config/' + encodeURIComponent(testId) +
                    '/manifest/' + encodeURIComponent(type) + '" title="' + (title || type) + '"' +
                    ' style="' + ANCHOR_STYLE + '">' + inner + '</a>';
            }
        };
    }

    function download(runId) {
        return {
            mode: 'download',
            runId: runId,
            chartId: function(base) {
                return 'dl-' + base;
            },
            profileAnchor: function(testId, key, inner, title) {
                return '<a href="#" onclick="dlProfile(\'' + testId + '\',\'' + key +
                    '\');return false;" title="' + (title || key) + '" style="' + ANCHOR_STYLE + '">' + inner + '</a>';
            },
            manifestAnchor: function(testId, type, inner, title) {
                return '<a href="#" onclick="dlManifest(\'' + testId + '\',\'' + type +
                    '\');return false;" title="' + (title || type) + '" style="' + ANCHOR_STYLE + '">' + inner + '</a>';
            }
        };
    }

    return { live: live, download: download };
})();
