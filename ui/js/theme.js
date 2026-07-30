/**
 * Theme Manager
 * Light/dark theme switching with localStorage persistence.
 *
 * MyAgent is built on Bootstrap 5.3, whose components and the app's own CSS
 * both read the native `--bs-*` variables. So instead of a custom variable
 * system we simply toggle Bootstrap's `data-bs-theme` attribute on <html>:
 * every component and every `var(--bs-*)` in style.css adapts automatically.
 *
 * Loaded early (before Bootstrap/app scripts) to avoid a flash of the wrong
 * theme on first paint.
 */
const ThemeManager = {
    STORAGE_KEY: 'myagent-theme',
    LIGHT: 'light',
    DARK: 'dark',

    init() {
        this.applyTheme(this.getSavedTheme());
        // Follow OS changes only while the user hasn't picked a theme manually
        // (the explicit choice lives in the Settings screen -> setTheme).
        if (window.matchMedia) {
            window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
                if (!localStorage.getItem(this.STORAGE_KEY)) {
                    this.applyTheme(e.matches ? this.DARK : this.LIGHT);
                }
            });
        }
    },

    getSavedTheme() {
        const saved = localStorage.getItem(this.STORAGE_KEY);
        if (saved === this.LIGHT || saved === this.DARK) return saved;

        // No explicit choice yet: follow the OS preference, defaulting to dark
        // (MyAgent shipped dark by default).
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
            return this.LIGHT;
        }
        return this.DARK;
    },

    applyTheme(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
    },

    setTheme(theme) {
        if (theme !== this.LIGHT && theme !== this.DARK) return;
        this.applyTheme(theme);
        localStorage.setItem(this.STORAGE_KEY, theme);
    },

    getCurrentTheme() {
        return document.documentElement.getAttribute('data-bs-theme') || this.DARK;
    }
};

// Initialize immediately to prevent a flash of the wrong theme.
ThemeManager.init();
