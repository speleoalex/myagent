const ModelsPage = {
    async render(params) {
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    async renderList() {
        let models = [];
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }

        const providerBadge = (p) =>
            ({ ollama: 'success', llamacpp: 'primary', openai: 'info', anthropic: 'warning', a1111: 'danger' }[p] || 'secondary');

        // No extra column for the kind: an icon in front of the name says it in
        // the width a phone has, and "chat" needs no marking — it is what every
        // model in this table was until image generators and embedders joined it.
        const kindIcon = (m) => m.kind === 'image'
            ? `<i class="bi bi-image text-secondary me-1" title="${App.escAttr(i18n('models.kindImage'))}"></i>`
            : m.kind === 'embedding'
                ? `<i class="bi bi-diagram-2 text-secondary me-1" title="${App.escAttr(i18n('models.kindEmbedding'))}"></i>`
                : '';

        App.container.innerHTML = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-box"></i> ${i18n('models.title')}</h3>
                <div class="d-flex flex-wrap gap-2">
                    <button class="btn btn-outline-info" id="btn-import-ollama"><i class="bi bi-download"></i> ${i18n('models.importOllama')}</button>
                    <a href="#/models/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('models.add')}</a>
                </div>
            </div>
            <div class="table-responsive">
                <table class="table table-hover">
                    <thead>
                        <tr><th>${i18n('common.id')}</th><th>${i18n('common.name')}</th><th>${i18n('models.colProvider')}</th><th>${i18n('models.colModel')}</th><th>${i18n('models.colUrl')}</th><th></th></tr>
                    </thead>
                    <tbody>
                        ${models.map(m => `
                            <tr>
                                <td><code>${App.esc(m.id)}</code></td>
                                <td>${kindIcon(m)}${App.esc(m.name)}</td>
                                <td><span class="badge bg-${providerBadge(m.provider)}">${App.esc(m.provider)}</span></td>
                                <td class="model-cell" data-mid="${App.escAttr(m.id)}">
                                    <code>${App.esc(m.model || '—')}</code>
                                    <span class="model-live"></span>
                                </td>
                                <td class="small">${App.esc(m.base_url)}</td>
                                <td>
                                    <a href="#/models/${m.id}" class="btn btn-sm btn-outline-primary">${i18n('common.edit')}</a>
                                </td>
                            </tr>
                        `).join('')}
                        ${models.length === 0 ? `<tr><td colspan="6" class="text-secondary">${i18n('models.empty')}</td></tr>` : ''}
                    </tbody>
                </table>
            </div>
            <div id="ollama-import" class="mt-3" style="display:none"></div>`;

        document.getElementById('btn-import-ollama').onclick = () => this.showOllamaImport();

        // Enrich each row with what the server is ACTUALLY serving (real model
        // name + capabilities). Fired after render so the table shows instantly;
        // failures leave the stored value untouched.
        this.enrichLiveInfo(models);
    },

    // Map a server-reported capability to a small labelled badge. Returns '' for
    // capabilities not worth surfacing (e.g. plain "completion").
    capabilityBadge(cap) {
        const map = {
            vision:      { icon: 'bi-eye',     cls: 'text-bg-info',    label: i18n('models.capVision') },
            audio:       { icon: 'bi-mic',     cls: 'text-bg-info',    label: i18n('models.capAudio') },
            multimodal:  { icon: 'bi-images',  cls: 'text-bg-info',    label: i18n('models.capMultimodal') },
            tools:       { icon: 'bi-wrench',  cls: 'text-bg-secondary', label: i18n('models.capTools') },
            embedding:   { icon: 'bi-diagram-2', cls: 'text-bg-secondary', label: i18n('models.capEmbedding') },
        };
        const b = map[cap];
        if (!b) return '';
        return `<span class="badge ${b.cls} me-1" title="${App.escAttr(cap)}"><i class="bi ${b.icon}"></i> ${b.label}</span>`;
    },

    async enrichLiveInfo(models) {
        // Only local providers are probed automatically: their served model /
        // capabilities are live and worth surfacing (llama.cpp's configured
        // value is meaningless). Remote 'openai' models already carry the real
        // model name in their config and expose no capabilities, so probing
        // them on every page load would just be needless traffic to a paid API.
        const local = models.filter(m => m.provider === 'ollama' || m.provider === 'llamacpp');
        await Promise.all(local.map(async (m) => {
            let info;
            try { info = await App.api('GET', `/models/${m.id}/probe`); }
            catch (e) { return; }
            const cell = document.querySelector(`.model-cell[data-mid="${CSS.escape(m.id)}"]`);
            if (!cell) return;
            const code = cell.querySelector('code');
            const live = cell.querySelector('.model-live');
            if (!info.reachable) {
                if (code) { code.classList.add('text-secondary'); code.title = i18n('models.serverOffline'); }
                return;
            }
            // Show the real served model (authoritative for llama.cpp, whose
            // configured value is ignored).
            if (code && info.served_model) code.textContent = info.served_model;
            const badges = (info.capabilities || [])
                .map(c => this.capabilityBadge(String(c).toLowerCase())).join('');
            let html = badges;
            if (info.context_window) {
                html += `<span class="badge text-bg-light border me-1" title="${i18n('models.ctxTitle')}">${this.fmtTokens(info.context_window)} ctx</span>`;
            }
            if (live && html) live.innerHTML = ' ' + html;
        }));
    },

    async showOllamaImport() {
        const container = document.getElementById('ollama-import');
        container.style.display = 'block';
        container.innerHTML = `<div class="text-center"><div class="spinner-border spinner-border-sm"></div> ${i18n('models.loadingOllama')}</div>`;

        try {
            const ollamaModels = await App.api('GET', '/models/ollama/available');
            if (ollamaModels.length === 0) {
                container.innerHTML = `<div class="alert alert-info">${i18n('models.noOllamaModels')}</div>`;
                return;
            }
            container.innerHTML = `
                <div class="card">
                    <div class="card-header">${i18n('models.availableOllama')}</div>
                    <div class="list-group list-group-flush">
                        ${ollamaModels.map(m => `
                            <div class="list-group-item d-flex justify-content-between align-items-center">
                                <div>
                                    <strong>${App.esc(m.name)}</strong>
                                    <small class="text-secondary ms-2">${m.size ? (m.size / 1e9).toFixed(1) + ' GB' : ''}</small>
                                </div>
                                <button class="btn btn-sm btn-outline-success btn-import-model" data-model="${App.escAttr(m.name)}">
                                    <i class="bi bi-plus"></i> ${i18n('models.import')}
                                </button>
                            </div>
                        `).join('')}
                    </div>
                </div>`;

            container.querySelectorAll('.btn-import-model').forEach(btn => {
                btn.onclick = async () => {
                    const modelName = btn.dataset.model;
                    const id = modelName.toLowerCase().replace(/[^a-z0-9]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
                    try {
                        await App.api('POST', '/models', {
                            id: id,
                            name: modelName,
                            provider: 'ollama',
                            model: modelName,
                            base_url: 'http://localhost:11434',
                            api_format: 'openai',
                            options: {},
                        });
                        App.toast(i18n('models.imported', { name: modelName }));
                        this.renderList();
                    } catch (err) {
                        App.toast(err.message, 'danger');
                    }
                };
            });
        } catch (err) {
            container.innerHTML = `<div class="alert alert-danger">${App.esc(err.message)}</div>`;
        }
    },

    // "16384" -> "16k" (context windows are always read in binary multiples)
    fmtTokens(n) {
        if (!n) return '—';
        return n >= 1024 ? `${(n / 1024).toFixed(n % 1024 ? 1 : 0)}k` : String(n);
    },

    // Sampling / runtime options offered for a provider, mirroring the
    // passthrough allow-list in llm_provider._build_payload: remote
    // OpenAI-compatible APIs reject unknown params with 400, so the extended
    // Ollama/llama.cpp knobs are only shown for local providers. `ph` is the
    // effective default, shown as placeholder so an empty field is honest about
    // what actually happens.
    // What a model of this kind may be. Chat has four providers and image
    // two, and the two lists share the label "openai" while meaning different
    // endpoints (/v1/chat/completions vs /v1/images/generations) — which is why
    // the kind is a stored field and not something guessed from the provider.
    // An embedder is LOCAL only: indexing ships the contents of the user's
    // documents to it, and app/engine/embedding.py refuses a remote one
    // whatever is stored, so offering one here would only offer a 400.
    providerChoices(kind) {
        if (kind === 'image') {
            return [['openai', i18n('models.providerImageOpenai')], ['a1111', i18n('models.providerA1111')]];
        }
        if (kind === 'embedding') {
            return [['ollama', 'Ollama'], ['llamacpp', 'llama.cpp']];
        }
        return [['ollama', 'Ollama'], ['llamacpp', 'llama.cpp'],
                ['openai', i18n('models.providerOpenai')], ['anthropic', i18n('models.providerAnthropic')]];
    },

    // Defaults for the generator, overridable per call by the tool's own
    // parameters. Mirrors imagegen.KNOWN_OPTIONS; sampler and negative_prompt
    // are text, every other knob a number.
    imageOptionSpecs() {
        return [
            { key: 'width',           min: 64, int: true, ph: '512' },
            { key: 'height',          min: 64, int: true, ph: '512' },
            { key: 'steps',           min: 1,  int: true, ph: '20' },
            { key: 'cfg_scale',       min: 0,  max: 30, ph: '7' },
            { key: 'sampler',         text: true, ph: 'euler_a' },
            { key: 'negative_prompt', text: true, ph: i18n('models.optUnset') },
        ];
    },

    optionSpecs(provider, kind) {
        if (kind === 'image') return this.imageOptionSpecs();
        // An embedder samples nothing: temperature, penalties and the context
        // window are all questions for a model that writes text.
        if (kind === 'embedding') return [];
        const local = provider === 'ollama' || provider === 'llamacpp';
        const anthropic = provider === 'anthropic';
        const specs = [
            { key: 'temperature',       min: 0,  max: 2, ph: '0.7' },
            { key: 'top_p',             min: 0,  max: 1, ph: '0.9' },
            // Anthropic REQUIRES max_tokens: the empty field falls back to the
            // provider's 8192 default, so the placeholder says so.
            { key: 'max_tokens',        min: 0,  int: true, ph: local ? '2048' : (anthropic ? '8192' : i18n('models.optUnset')) },
            { key: 'request_timeout',   min: 5,  int: true, ph: '600' },
        ];
        if (!anthropic) specs.push(
            // The Messages API has no frequency/presence penalty.
            { key: 'frequency_penalty', min: -2, max: 2, ph: '0' },
            { key: 'presence_penalty',  min: -2, max: 2, ph: '0' },
        );
        if (local || anthropic) specs.push(
            { key: 'top_k',          min: 0,  int: true, ph: '40' },
        );
        if (local) specs.push(
            { key: 'min_p',          min: 0,  max: 1, ph: '0.05' },
            { key: 'repeat_penalty', min: 0,  max: 2, ph: '1.1' },
            { key: 'repeat_last_n',  min: -1, int: true, ph: '64' },
        );
        return specs;
    },

    // Providers that talk to a paid remote API with an API key.
    isRemote(provider) {
        return provider === 'openai' || provider === 'anthropic';
    },

    // Whether the api_key field is offered. An image backend is usually on
    // localhost and needs none, but it may sit behind an auth proxy and the
    // server keeps a key for it either way (KEYED_PROVIDERS in app/models.py),
    // so the field is shown and simply left empty when there is no key.
    showsKey(kind, provider) {
        if (kind === 'image') return true;
        if (kind === 'embedding') return false;   // local only, by policy
        return this.isRemote(provider);
    },

    // Placeholder of the model-name field, per kind: an example the user can
    // copy is worth more than a generic "model name".
    modelPlaceholderKey(kind) {
        return { image: 'models.modelImagePlaceholder',
                 embedding: 'models.modelEmbedPlaceholder' }[kind] || 'models.modelPlaceholder';
    },

    // Union of every key that has a dedicated field for SOME provider. Keys in
    // here are never kept in the advanced-JSON box: switching to a provider that
    // rejects them (top_k on a remote API) must drop them, not smuggle them
    // through and earn a 400.
    knownOptionKeys() {
        return new Set([
            ...this.optionSpecs('ollama').map(s => s.key),
            ...this.optionSpecs('openai').map(s => s.key),
            ...this.imageOptionSpecs().map(s => s.key),
        ]);
    },

    // snake_case -> CamelCase for building i18n keys (optTemperature, ctxSourceModelMax).
    _camel(key) {
        return key.replace(/(^|_)([a-z])/g, (m, _s, c) => c.toUpperCase());
    },

    optionLabel(key) {
        // temperature -> models.optTemperature, repeat_last_n -> models.optRepeatLastN
        return i18n('models.opt' + this._camel(key));
    },

    // No step grid on these fields: in HTML5 `step` is a VALIDATION rule, not
    // just the arrow increment. Any stored value off the grid (repeat_last_n 320
    // against a step of 16, request_timeout 600 against 5+30k — both shipped
    // defaults) makes the field :invalid, and the form then refuses to submit
    // with nothing on screen to explain why. Integers step by 1, floats by "any".
    renderOptionFields(provider, options, kind) {
        const cell = (s) => {
            const v = options[s.key];
            const val = v === undefined || v === null ? '' : App.esc(String(v));
            // A sampler name is not a number; everything else here is.
            const input = s.text
                ? `<input type="text" class="form-control form-control-sm opt-field" id="opt-${s.key}"
                           data-key="${s.key}" data-text="1"
                           value="${val}" placeholder="${App.escAttr(s.ph)}">`
                : `<input type="number" class="form-control form-control-sm opt-field" id="opt-${s.key}"
                           data-key="${s.key}" ${s.int ? 'data-int="1"' : ''}
                           min="${s.min}" ${s.max !== undefined ? `max="${s.max}"` : ''}
                           step="${s.int ? '1' : 'any'}"
                           value="${val}" placeholder="${App.escAttr(s.ph)}">`;
            return `
                <div class="col-6 col-md-4">
                    <label class="form-label small mb-1" for="opt-${s.key}">${this.optionLabel(s.key)}</label>
                    ${input}
                </div>`;
        };
        return this.optionSpecs(provider, kind).map(cell).join('');
    },

    // Options the form has no dedicated field for (custom/experimental keys):
    // kept verbatim in the advanced JSON box so nothing is silently lost.
    extraOptions(options) {
        const known = this.knownOptionKeys();
        return Object.fromEntries(Object.entries(options || {}).filter(([k]) => !known.has(k)));
    },

    // Values currently typed in the dedicated fields. An empty field means "not
    // set" — the key is dropped, not sent as 0.
    collectFieldOptions() {
        const out = {};
        document.querySelectorAll('.opt-field').forEach(inp => {
            const raw = inp.value.trim();
            if (raw === '') return;
            if (inp.dataset.text) { out[inp.dataset.key] = raw; return; }
            const n = inp.dataset.int ? parseInt(raw, 10) : parseFloat(raw);
            if (!Number.isNaN(n)) out[inp.dataset.key] = n;
        });
        return out;
    },

    // Full options object: advanced JSON first, dedicated fields on top.
    // Returns null when the advanced JSON doesn't parse (caller reports it).
    collectOptions() {
        let extra;
        try {
            extra = JSON.parse(document.getElementById('f-options').value || '{}');
        } catch (err) {
            return null;
        }
        return { ...this.extraOptions(extra), ...this.collectFieldOptions() };
    },

    // Fill the line under the context field with what the server reports.
    async probeContext(modelId, refresh = false) {
        const el = document.getElementById('ctx-info');
        if (!el) return;
        el.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${i18n('models.contextProbing')}`;
        let info;
        try {
            info = await App.api('GET', `/models/${modelId}/probe${refresh ? '?refresh=true' : ''}`);
        } catch (e) {
            el.textContent = i18n('models.contextProbeFailed');
            return;
        }
        if (!info.reachable) {
            el.innerHTML = `<i class="bi bi-exclamation-triangle text-warning"></i> ${i18n('models.serverOffline')}`;
            return;
        }
        const srcKey = 'models.ctxSource' + this._camel(info.context_source);
        const parts = [i18n('models.contextEffective', {
            n: info.context_window, k: this.fmtTokens(info.context_window), src: i18n(srcKey),
        })];
        if (info.n_ctx_max) parts.push(i18n('models.contextMax', { k: this.fmtTokens(info.n_ctx_max) }));
        el.innerHTML = `<i class="bi bi-info-circle"></i> ` + parts.join(' · ');
    },

    // Default base URL per provider (used when switching in the form). Split by
    // kind because "openai" means two different servers: the chat API, or
    // whatever speaks /v1/images/generations — stable-diffusion.cpp's sd-server
    // on 8080 being the local case this was written for.
    defaultUrl(provider, kind) {
        if (kind === 'image') {
            return {
                openai: 'http://127.0.0.1:8080',
                a1111: 'http://127.0.0.1:7860',
            }[provider] || '';
        }
        return {
            ollama: 'http://localhost:11434',
            llamacpp: 'http://localhost:8080',
            openai: 'https://api.openai.com/v1',
            anthropic: 'https://api.anthropic.com',
        }[provider] || '';
    },

    // True when this URL is one this form would have filled in by itself, for
    // any kind and provider. Used to decide whether switching kind may rewrite
    // it (see the f-kind handler).
    isDefaultUrl(url) {
        return ['chat', 'image'].some(k =>
            this.providerChoices(k).some(([v]) => this.defaultUrl(v, k) === url));
    },

    async renderForm(modelId) {
        let model = { id: '', name: '', kind: 'chat', provider: 'ollama', model: '', base_url: 'http://localhost:11434', api_key: '', api_format: 'openai', supports_vision: true, supports_audio: false, supports_tools: null, context_window: null, options: {} };
        let isEdit = false;

        if (modelId) {
            try {
                model = await App.api('GET', `/models/${modelId}`);
                isEdit = true;
            } catch (e) {
                App.toast(i18n('models.notFound'), 'danger');
                location.hash = '#/models';
                return;
            }
        }

        // The server never returns the key in clear — only a mask ("********")
        // when one is set. We prefill the password field with that mask so an
        // untouched field round-trips as "keep the stored key", while clearing
        // the field sends an empty value = explicitly remove the key.
        // Absent kind = chat: that is all this store held until image
        // generators joined it, and the server reads it back the same way.
        const kind = model.kind || 'chat';
        const isImage = kind === 'image';
        // Chat is the only kind with capabilities, a context window and
        // sampling options; the other two hide those groups.
        const isChat = kind === 'chat';
        const KEY_MASK = '********';
        const hasKey = !!model.api_key;
        const keyInitial = hasKey ? KEY_MASK : '';
        // Anything without a dedicated field stays editable as raw JSON.
        const extraOptions = this.extraOptions(model.options);

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3>${isEdit ? i18n('models.editTitle') : i18n('models.addTitle')}</h3>
                    <form id="model-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.id')}</label>
                            <input type="text" class="form-control" id="f-id" value="${App.esc(model.id)}" ${isEdit ? 'readonly' : ''} required
                                   pattern="[a-z0-9_-]+">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.name')}</label>
                            <input type="text" class="form-control" id="f-name" value="${App.esc(model.name)}" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label" for="f-kind">${i18n('models.kind')}</label>
                            <select class="form-select" id="f-kind">
                                <option value="chat" ${isChat ? 'selected' : ''}>${i18n('models.kindChat')}</option>
                                <option value="embedding" ${kind === 'embedding' ? 'selected' : ''}>${i18n('models.kindEmbedding')}</option>
                                <option value="image" ${isImage ? 'selected' : ''}>${i18n('models.kindImage')}</option>
                            </select>
                            <div class="form-text">${i18n('models.kindHelp')}</div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label" for="f-provider">${i18n('models.provider')}</label>
                            <select class="form-select" id="f-provider">
                                ${this.providerChoices(kind).map(([v, label]) =>
                                    `<option value="${v}" ${model.provider === v ? 'selected' : ''}>${App.esc(label)}</option>`).join('')}
                            </select>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('models.baseUrl')}</label>
                            <input type="text" class="form-control" id="f-url" value="${App.esc(model.base_url)}" autocomplete="url" required>
                        </div>
                        <div class="mb-3" id="api-key-group" ${this.showsKey(kind, model.provider) ? '' : 'style="display:none"'}>
                            <label class="form-label">${i18n('models.apiKey')}</label>
                            <div class="input-group">
                                <input type="password" class="form-control" id="f-apikey" value="${keyInitial}" autocomplete="new-password"
                                       placeholder="sk-...">
                                ${App.revealButton('f-apikey')}
                            </div>
                            <small class="text-secondary">${i18n('models.apiKeyHint')}${isEdit && hasKey ? ' ' + i18n('models.apiKeyKeep') : ''}</small>
                        </div>
                        <div class="mb-3" id="model-name-group" ${!isImage && model.provider === 'llamacpp' ? 'style="display:none"' : ''}>
                            <label class="form-label" for="f-model">${i18n('models.modelName')}</label>
                            <div class="input-group">
                                <input type="text" class="form-control" id="f-model" value="${App.esc(model.model)}"
                                       list="remote-models"
                                       ${!isImage && model.provider !== 'llamacpp' ? 'required' : ''}
                                       placeholder="${i18n(this.modelPlaceholderKey(kind))}">
                                <button type="button" class="btn btn-outline-secondary" id="btn-fetch-models"
                                        ${!isImage && this.isRemote(model.provider) ? '' : 'style="display:none"'}>
                                    <i class="bi bi-cloud-download"></i> ${i18n('models.fetchRemote')}
                                </button>
                            </div>
                            <datalist id="remote-models"></datalist>
                            ${isImage ? `<small class="text-secondary">${i18n('models.modelImageHint')}</small>` : ''}
                        </div>
                        <div class="mb-3" id="cap-group" ${isChat ? '' : 'style="display:none"'}>
                            <label class="form-label">${i18n('models.capabilities')}</label>
                            <div class="form-check">
                                <input class="form-check-input" type="checkbox" id="f-vision" ${model.supports_vision !== false ? 'checked' : ''}>
                                <label class="form-check-label" for="f-vision">${i18n('models.supportsVision')}</label>
                            </div>
                            <div class="form-check">
                                <input class="form-check-input" type="checkbox" id="f-audio" ${model.supports_audio ? 'checked' : ''}>
                                <label class="form-check-label" for="f-audio">${i18n('models.supportsAudio')}</label>
                            </div>
                            <div class="form-text">${i18n('models.capabilitiesHelp')}</div>
                            <div class="mt-2" style="max-width:260px">
                                <label class="form-label small mb-1" for="f-tools">${i18n('models.toolCalling')}</label>
                                <select class="form-select form-select-sm" id="f-tools">
                                    <option value="" ${model.supports_tools === null || model.supports_tools === undefined ? 'selected' : ''}>${i18n('models.toolsAuto')}</option>
                                    <option value="native" ${model.supports_tools === true ? 'selected' : ''}>${i18n('models.toolsNative')}</option>
                                    <option value="text" ${model.supports_tools === false ? 'selected' : ''}>${i18n('models.toolsText')}</option>
                                </select>
                                <div class="form-text">${i18n('models.toolCallingHelp')}</div>
                            </div>
                        </div>
                        <div class="mb-3" id="ctx-group" ${isChat ? '' : 'style="display:none"'}>
                            <label class="form-label" for="f-ctx">${i18n('models.contextWindow')}</label>
                            <div class="input-group">
                                <input type="number" class="form-control" id="f-ctx" min="0" step="1024"
                                       value="${model.context_window || ''}" placeholder="${i18n('models.contextAuto')}">
                                <span class="input-group-text">${i18n('models.tokensUnit')}</span>
                                ${isEdit ? `<button type="button" class="btn btn-outline-secondary" id="btn-probe-ctx">
                                    <i class="bi bi-arrow-clockwise"></i> ${i18n('models.contextDetect')}</button>` : ''}
                            </div>
                            <div class="form-text" id="ctx-info">${isEdit ? '' : i18n('models.contextNewHint')}</div>
                            <div class="form-text" id="ctx-help"></div>
                        </div>
                        <div class="mb-3" id="options-group" ${kind === 'embedding' ? 'style="display:none"' : ''}>
                            <label class="form-label">${i18n('models.options')}</label>
                            <div class="row g-2" id="options-fields">${this.renderOptionFields(model.provider, model.options || {}, kind)}</div>
                            <div class="form-text" id="options-help"></div>
                            <details class="mt-2" ${Object.keys(extraOptions).length ? 'open' : ''}>
                                <summary class="small text-secondary">${i18n('models.optionsAdvanced')}</summary>
                                <textarea class="form-control font-monospace mt-2" id="f-options" rows="3">${JSON.stringify(extraOptions, null, 2)}</textarea>
                                <div class="form-text">${i18n('models.optionsAdvancedHelp')}</div>
                            </details>
                        </div>
                        <div class="d-flex gap-2">
                            <button type="submit" class="btn btn-primary">${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                            <a href="#/models" class="btn btn-secondary">${i18n('common.cancel')}</a>
                            ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="btn-delete">${i18n('common.delete')}</button>` : ''}
                        </div>
                    </form>
                </div>
            </div>`;

        if (!isEdit) App.autoId('f-name', 'f-id');

        // Provider-specific help: which options exist, and what the context
        // field actually does (it allocates on Ollama, only guards truncation
        // elsewhere).
        const currentKind = () => document.getElementById('f-kind').value;

        const applyProviderHelp = (provider, k) => {
            if (k === 'embedding') return;   // both help lines live in hidden groups
            document.getElementById('options-help').textContent = k === 'image'
                // On the OpenAI image route only the size survives: steps, cfg
                // and the sampler have no field in that request body. Say so
                // here rather than let them look effective and be dropped.
                ? i18n(provider === 'a1111' ? 'models.optionsA1111Help' : 'models.optionsImageOpenaiHelp')
                : i18n(this.isRemote(provider) ? 'models.optionsRemoteHelp' : 'models.optionsLocalHelp');
            if (k === 'image') return;   // ctx-help lives in a hidden group
            document.getElementById('ctx-help').textContent = i18n({
                ollama: 'models.contextHelpOllama',
                llamacpp: 'models.contextHelpLlamacpp',
                openai: 'models.contextHelpRemote',
                anthropic: 'models.contextHelpAnthropic',
            }[provider] || 'models.contextHelpOllama');
        };
        applyProviderHelp(model.provider, kind);

        // Adapt the form to the provider: API key, model-name
        // visibility/requiredness and the available options. Shared by the two
        // selects, because changing the kind also changes the provider.
        const applyProvider = (provider, k) => {
            const img = k === 'image';
            document.getElementById('api-key-group').style.display = this.showsKey(k, provider) ? '' : 'none';
            // llama.cpp serves one model and ignores the name (chat and
            // embedding alike); an image backend takes an optional one.
            document.getElementById('model-name-group').style.display = !img && provider === 'llamacpp' ? 'none' : '';
            document.getElementById('f-model').required = !img && provider !== 'llamacpp';
            document.getElementById('f-model').placeholder = i18n(this.modelPlaceholderKey(k));
            document.getElementById('btn-fetch-models').style.display = !img && this.isRemote(provider) ? '' : 'none';
            document.getElementById('options-group').style.display = k === 'embedding' ? 'none' : '';
            // Re-render the option fields for the new provider, keeping the
            // values the user already typed for the knobs that survive.
            document.getElementById('options-fields').innerHTML =
                this.renderOptionFields(provider, this.collectFieldOptions(), k);
            applyProviderHelp(provider, k);
            // The probe reads the STORED config, so a pending provider switch
            // makes any detected value stale until the form is saved.
            const probeBtn = document.getElementById('btn-probe-ctx');
            if (probeBtn) {
                const pending = provider !== model.provider || k !== kind;
                probeBtn.disabled = pending;
                if (pending) document.getElementById('ctx-info').textContent = i18n('models.contextSaveFirst');
            }
        };

        document.getElementById('f-provider').onchange = (e) => {
            const k = currentKind();
            document.getElementById('f-url').value = this.defaultUrl(e.target.value, k);
            applyProvider(e.target.value, k);
        };

        // Changing the kind rebuilds the provider list: the two kinds share the
        // name "openai" for two unrelated endpoints, so the provider cannot
        // simply carry over.
        document.getElementById('f-kind').onchange = (e) => {
            const k = e.target.value;
            const chat = k === 'chat';
            const sel = document.getElementById('f-provider');
            const keep = this.providerChoices(k).some(([v]) => v === sel.value) ? sel.value : null;
            sel.innerHTML = this.providerChoices(k).map(([v, label]) =>
                `<option value="${v}" ${v === (keep || '') ? 'selected' : ''}>${App.esc(label)}</option>`).join('');
            const provider = sel.value;
            // The URL only follows when what is in the box is still one of the
            // defaults: a URL the user typed by hand is the one thing on this
            // form they could not get back.
            const url = document.getElementById('f-url');
            if (!url.value.trim() || this.isDefaultUrl(url.value.trim())) {
                url.value = this.defaultUrl(provider, k);
            }
            // Neither an image generator nor an embedder answers "can it see"
            // or "how big is its context": both questions belong to a chat model.
            document.getElementById('cap-group').style.display = chat ? '' : 'none';
            document.getElementById('ctx-group').style.display = chat ? '' : 'none';
            applyProvider(provider, k);
        };

        // Query the remote provider for its model list (fills the datalist)
        document.getElementById('btn-fetch-models').onclick = async () => {
            const btn = document.getElementById('btn-fetch-models');
            btn.disabled = true;
            const endpoint = document.getElementById('f-provider').value === 'anthropic'
                ? '/models/anthropic/available' : '/models/openai/available';
            try {
                const ids = await App.api('POST', endpoint, {
                    base_url: document.getElementById('f-url').value.trim(),
                    api_key: document.getElementById('f-apikey').value,
                    model_id: isEdit ? modelId : null,
                });
                document.getElementById('remote-models').innerHTML =
                    // escAttr, not esc: these ids come from a third-party
                    // gateway's /v1/models — in attribute position a quote in
                    // the value would open an injection (see App.escAttr).
                    ids.map(id => `<option value="${App.escAttr(id)}">`).join('');
                App.toast(i18n('models.remoteLoaded', { count: ids.length }));
            } catch (err) {
                App.toast(i18n('models.remoteError') + ': ' + err.message, 'danger');
            } finally {
                btn.disabled = false;
            }
        };

        document.getElementById('model-form').onsubmit = async (e) => {
            e.preventDefault();
            const provider = document.getElementById('f-provider').value;
            const newKind = currentKind();
            const options = this.collectOptions();
            if (options === null) {
                App.toast(i18n('models.invalidOptions'), 'danger');
                return;
            }
            const ctxRaw = document.getElementById('f-ctx').value.trim();
            const modelName = document.getElementById('f-model').value.trim();
            const data = {
                id: document.getElementById('f-id').value.trim(),
                name: document.getElementById('f-name').value.trim(),
                kind: newKind,
                provider: provider,
                model: modelName || (newKind !== 'image' && provider === 'llamacpp' ? 'default' : ''),
                base_url: document.getElementById('f-url').value.trim(),
                // Empty on an existing keyed config means "keep the stored key"
                // (the server never sends the real key back).
                api_key: this.showsKey(newKind, provider) ? document.getElementById('f-apikey').value : '',
                api_format: 'openai',
                supports_vision: document.getElementById('f-vision').checked,
                supports_audio: document.getElementById('f-audio').checked,
                // Empty = auto: send the tools and let the endpoint answer.
                supports_tools: { native: true, text: false }[document.getElementById('f-tools').value] ?? null,
                // Empty = auto: the server tells us the real window (see model_probe).
                context_window: ctxRaw === '' ? null : parseInt(ctxRaw, 10),
                options: options,
            };
            try {
                if (isEdit) {
                    await App.api('PUT', `/models/${modelId}`, data);
                } else {
                    await App.api('POST', '/models', data);
                }
                App.toast(i18n('models.saved'));
                location.hash = '#/models';
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        if (isEdit) {
            // Local servers are cheap to ask, so show the live window right away;
            // remote (paid) providers only on demand, via the button. An image
            // backend or an embedder has no context window to ask about.
            if (!isChat) {
                /* nothing to probe */
            } else if (!this.isRemote(model.provider)) {
                this.probeContext(modelId);
            } else {
                document.getElementById('ctx-info').textContent = i18n('models.contextDetectHint');
            }
            const probeBtn = document.getElementById('btn-probe-ctx');
            if (probeBtn) probeBtn.onclick = () => this.probeContext(modelId, true);

            document.getElementById('btn-delete').onclick = async () => {
                if (!confirm(i18n('models.confirmDelete'))) return;
                try {
                    await App.api('DELETE', `/models/${modelId}`);
                    App.toast(i18n('models.deleted'));
                    location.hash = '#/models';
                } catch (err) {
                    App.toast(err.message, 'danger');
                }
            };
        }
    },
};
