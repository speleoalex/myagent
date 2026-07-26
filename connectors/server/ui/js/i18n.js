/**
 * Internationalization for the Connectors admin UI.
 * Mirrors the myagent frontend i18n pattern: English default + Italian,
 * dictionaries assigned onto I18n.translations by js/i18n/{locale}.js.
 * Usage: i18n('key') or i18n('key', { var: 'value' }).
 *
 * Load order: this file BEFORE js/i18n/*.js, both BEFORE app.js.
 */
const I18n = {
  locale: "en",
  STORAGE_KEY: "myagent_locale",       // same key name as the myagent UI
  translations: { en: {}, it: {} },

  init() {
    const saved = localStorage.getItem(this.STORAGE_KEY);
    if (saved && this.translations[saved]) {
      this.locale = saved;
    } else {
      const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
      if (this.translations[nav]) this.locale = nav;
    }
    document.documentElement.lang = this.locale;
  },

  t(key, vars) {
    let text = this.translations[this.locale]?.[key]
      || this.translations["en"]?.[key]
      || key;
    if (vars) {
      Object.entries(vars).forEach(([k, v]) => {
        text = text.replace(new RegExp(`\\{${k}\\}`, "g"), v);
      });
    }
    return text;
  },

  setLocale(lang) {
    if (!this.translations[lang]) return;
    this.locale = lang;
    localStorage.setItem(this.STORAGE_KEY, lang);
    document.documentElement.lang = lang;
    // Re-translate static chrome and re-render dynamic content.
    window.applyStaticI18n?.();
    window.onLocaleChange?.();
  },

  available() {
    return Object.keys(this.translations);
  },
};

function i18n(key, vars) {
  return I18n.t(key, vars);
}
