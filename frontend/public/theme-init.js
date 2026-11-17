// Apply the persisted theme before first paint to avoid a flash (externalized for strict script-src 'self' CSP).
(function () {
  try {
    var stored = window.localStorage.getItem("rag-arena.theme");
    var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var dark = stored === "dark" || ((stored === null || stored === "system") && prefersDark);
    if (dark) document.documentElement.classList.add("dark");
  } catch (_err) {
    /* localStorage unavailable: fall back to light */
  }
})();
