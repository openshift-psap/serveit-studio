/*
 * InfeRecipe — app.js entry point
 *
 * All code has been split into focused modules under js/modules/:
 *   socket.js     — Socket.IO init, session guard
 *   console.js    — Console log display
 *   config.js     — Config save/load, wizard state
 *   wizard.js     — Model selector, workload mode
 *   settings.js   — Advanced vLLM, TP pairs, EPP, prefix cache
 *   cluster.js    — PVC listing, test plan, cluster scan
 *   navigation.js — Step navigation, optimization start/stop
 *   resume.js     — Resume overlay, run management
 *   report.js     — Report tabs, estimator
 *   charts.js     — Plotly chart rendering
 *   ui-helpers.js — Subtabs, comparison, sidebar
 *
 * This file is now empty — modules are loaded via <script> tags in index.html.
 * Kept for backward compatibility with any direct references.
 */
