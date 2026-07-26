/**
 * Internationalization (i18n) Module
 * Supports English (default) and Italian translations.
 * Translation dictionaries are loaded from js/i18n/{locale}.js
 * Usage: i18n('key') or i18n('key', { var: 'value' })
 *
 * Load order matters: this file must load BEFORE js/i18n/*.js (which
 * assign onto I18n.translations) and before the page/app scripts.
 */
const I18n = {
    locale: 'en',
    STORAGE_KEY: 'myagent_locale',
    translations: { en: {}, it: {} },

    init() {
        const saved = localStorage.getItem(this.STORAGE_KEY);
        if (saved && this.translations[saved]) {
            this.locale = saved;
        } else {
            // First visit: follow the browser language when we support it.
            const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
            if (this.translations[nav]) this.locale = nav;
        }
        document.documentElement.lang = this.locale;
    },

    t(key, vars) {
        let text = this.translations[this.locale]?.[key]
            || this.translations['en']?.[key]
            || key;

        if (vars) {
            Object.entries(vars).forEach(([k, v]) => {
                text = text.replace(new RegExp(`\\{${k}\\}`, 'g'), v);
            });
        }
        return text;
    },

    setLocale(lang) {
        if (!this.translations[lang]) return;
        this.locale = lang;
        localStorage.setItem(this.STORAGE_KEY, lang);
        document.documentElement.lang = lang;

        // Re-translate static chrome and re-render the current page.
        if (typeof App !== 'undefined') {
            App.applyStaticI18n?.();
            App.route?.();
        }
    },

    available() {
        return Object.keys(this.translations);
    },

    getDateLocale() {
        return this.locale === 'it' ? 'it-IT' : 'en-US';
    }
};

/**
 * Shorthand translation function.
 */
function i18n(key, vars) {
    return I18n.t(key, vars);
}
