const SettingsPage = {
    async render() {
        let settings = {};
        try { settings = await App.api('GET', '/system/settings'); } catch (e) { /* empty */ }

        let models = [];
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3><i class="bi bi-gear"></i> ${i18n('settings.title')}</h3>

                    <!-- Appearance: client-side preferences (apply immediately,
                         stored in localStorage — not part of the server form). -->
                    <h5 class="mt-3">${i18n('settings.appearance')}</h5>
                    <div class="row g-3 mb-2">
                        <div class="col-sm-6">
                            <label class="form-label"><i class="bi bi-translate"></i> ${i18n('settings.language')}</label>
                            <select class="form-select" id="f-language">
                                <option value="en" ${I18n.locale === 'en' ? 'selected' : ''}>English</option>
                                <option value="it" ${I18n.locale === 'it' ? 'selected' : ''}>Italiano</option>
                            </select>
                        </div>
                        <div class="col-sm-6">
                            <label class="form-label"><i class="bi bi-circle-half"></i> ${i18n('settings.theme')}</label>
                            <select class="form-select" id="f-theme">
                                <option value="light" ${ThemeManager.getCurrentTheme() === 'light' ? 'selected' : ''}>${i18n('settings.themeLight')}</option>
                                <option value="dark" ${ThemeManager.getCurrentTheme() === 'dark' ? 'selected' : ''}>${i18n('settings.themeDark')}</option>
                            </select>
                        </div>
                    </div>

                    <hr class="my-4">

                    <form id="settings-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.defaultModel')}</label>
                            <select class="form-select" id="f-default-model">
                                <option value="">${i18n('settings.noDefaultModel')}</option>
                                ${models.map(m => `<option value="${m.id}" ${m.id === settings.default_model_id ? 'selected' : ''}>${App.esc(m.name)} (${m.provider})</option>`).join('')}
                            </select>
                            <small class="text-secondary">${i18n('settings.defaultModelHint')}</small>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.ollamaUrl')}</label>
                            <input type="text" class="form-control" id="f-ollama-url" value="${App.esc(settings.ollama_base_url || 'http://localhost:11434')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('settings.llamacppUrl')}</label>
                            <input type="text" class="form-control" id="f-llamacpp-url" value="${App.esc(settings.llamacpp_base_url || 'http://localhost:8080')}">
                        </div>
                        <button type="submit" class="btn btn-primary">${i18n('settings.save')}</button>
                    </form>

                    <hr class="my-4">

                    <h5>${i18n('settings.systemStatus')}</h5>
                    <div id="status-checks" class="mb-3">
                        <div class="spinner-border spinner-border-sm"></div> ${i18n('settings.checking')}
                    </div>
                </div>
            </div>`;

        // Language: applies immediately and re-renders the app (so this page
        // re-renders with the new locale, keeping the select in sync).
        document.getElementById('f-language').onchange = (e) => I18n.setLocale(e.target.value);
        // Theme: applies immediately (localStorage-persisted via ThemeManager).
        document.getElementById('f-theme').onchange = (e) => ThemeManager.setTheme(e.target.value);

        document.getElementById('settings-form').onsubmit = async (e) => {
            e.preventDefault();
            const data = {
                ollama_base_url: document.getElementById('f-ollama-url').value.trim(),
                llamacpp_base_url: document.getElementById('f-llamacpp-url').value.trim(),
                default_model_id: document.getElementById('f-default-model').value || null,
            };
            try {
                await App.api('PUT', '/system/settings', data);
                App.toast(i18n('settings.saved'));
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        this.checkStatus();
    },

    async checkStatus() {
        const container = document.getElementById('status-checks');
        let html = '';

        // Health
        try {
            await App.api('GET', '/system/health');
            html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.apiOk')}</div>`;
        } catch (e) {
            html += `<div><i class="bi bi-x-circle text-danger"></i> ${i18n('settings.apiError')}</div>`;
        }

        // Ollama
        try {
            const models = await App.api('GET', '/models/ollama/available');
            html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.ollamaModels', { count: models.length })}</div>`;
        } catch (e) {
            html += `<div><i class="bi bi-x-circle text-danger"></i> ${i18n('settings.ollamaUnreachable')}</div>`;
        }

        // llama.cpp — one line per registered instance (each has its own base_url)
        try {
            const res = await App.api('GET', '/models/llamacpp/status');
            const instances = res.instances || [];
            if (instances.length === 0) {
                html += `<div><i class="bi bi-dash-circle text-secondary"></i> ${i18n('settings.llamacppNone')}</div>`;
            }
            for (const inst of instances) {
                const label = App.esc(inst.name ? `${inst.name} (${inst.base_url})` : inst.base_url);
                if (inst.status === 'ok') {
                    html += `<div><i class="bi bi-check-circle text-success"></i> ${i18n('settings.llamacppOkNamed', { label })}</div>`;
                } else {
                    html += `<div><i class="bi bi-exclamation-circle text-warning"></i> ${i18n('settings.llamacppUnreachableNamed', { label })}</div>`;
                }
            }
        } catch (e) {
            html += `<div><i class="bi bi-exclamation-circle text-warning"></i> ${i18n('settings.llamacppUnreachable')}</div>`;
        }

        container.innerHTML = html;
    },
};
