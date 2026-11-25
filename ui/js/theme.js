/**
 * Theme Manager
 * Light/dark/auto theme switching with localStorage persistence.
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
    AUTO: 'auto',

    init() {
        this.applyTheme(this.resolve(this.getPreference()));
        // In auto mode follow OS changes live; an explicit light/dark choice
        // (Settings screen -> setTheme) pins the theme instead.
        if (window.matchMedia) {
            window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
                if (this.getPreference() === this.AUTO) this.applyTheme(this.resolve(this.AUTO));
            });
        }
    },

    /** The user's choice: 'light', 'dark' or 'auto' (no stored key = auto). */
    getPreference() {
        let saved = null;
        try { saved = localStorage.getItem(this.STORAGE_KEY); } catch (e) { /* storage blocked */ }
        return (saved === this.LIGHT || saved === this.DARK) ? saved : this.AUTO;
    },

    /** Concrete theme for a preference. Auto follows the OS, defaulting to
     *  dark when the OS states nothing (MyAgent shipped dark by default). */
    resolve(preference) {
        if (preference !== this.AUTO) return preference;
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
            return this.LIGHT;
        }
        return this.DARK;
    },

    applyTheme(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
    },

    setTheme(preference) {
        if (![this.LIGHT, this.DARK, this.AUTO].includes(preference)) return;
        try {
            // Auto is stored as the absence of a choice, so installs that never
            // touched the setting keep following the OS.
            if (preference === this.AUTO) localStorage.removeItem(this.STORAGE_KEY);
            else localStorage.setItem(this.STORAGE_KEY, preference);
        } catch (e) { /* storage blocked: still apply for this page */ }
        this.applyTheme(this.resolve(preference));
    }
};

// Initialize immediately to prevent a flash of the wrong theme.
ThemeManager.init();
