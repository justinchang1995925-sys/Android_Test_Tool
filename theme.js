const THEME_KEY = "android-tool-theme";

function applyTheme(mode) {
  const isLight = mode === "light";
  document.documentElement.classList.toggle("theme-light", isLight);
  document.documentElement.classList.toggle("theme-dark", !isLight);
  const btn = document.getElementById("themeToggle");
  if (btn) btn.textContent = isLight ? "暗色模式" : "浅色模式";
}

function initAppTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "dark";
  applyTheme(saved);
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = document.documentElement.classList.contains("theme-light") ? "dark" : "light";
    applyTheme(next);
    localStorage.setItem(THEME_KEY, next);
    if (typeof window.onThemeChanged === "function") {
      window.onThemeChanged();
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAppTheme);
} else {
  initAppTheme();
}
