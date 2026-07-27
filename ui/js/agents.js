const AgentsPage = {
    async render(params) {
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    async renderList() {
        let agents = [];
        try { agents = await App.api('GET', '/agents'); } catch (e) { /* empty */ }
        let native = [];
        try { native = await App.api('GET', '/agents/native'); } catch (e) { /* no catalog */ }
        let tools = [];
        try { tools = await App.api('GET', '/tools'); } catch (e) { /* empty */ }
        // Cache agents+tools so the preview tree can resolve delegation without refetching.
        this._allAgents = agents;
        this._allTools = tools;

        App.container.innerHTML = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-cpu"></i> ${i18n('agents.title')}</h3>
                <a href="#/agents/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('agents.new')}</a>
            </div>
            <div class="row g-3">
                ${agents.length === 0 ? `<div class="col-12 text-secondary">${i18n('agents.empty')}</div>` : ''}
                ${agents.map(a => `
                    <div class="col-md-4 col-lg-3">
                        <div class="card card-agent h-100" onclick="location.hash='#/agents/${a.id}'">
                            <div class="card-body">
                                <h5 class="card-title">${App.esc(a.name)}</h5>
                                <p class="card-text text-secondary small">${App.esc(a.description || '')}</p>
                                <div class="small">
                                    <span class="badge bg-secondary">${App.esc(a.model_id)}</span>
                                    <span class="badge bg-info">${i18n('agents.toolsBadge', { count: (a.tools || []).length })}</span>
                                </div>
                            </div>
                            <div class="card-footer d-flex gap-2">
                                <a href="#/chat/${a.id}" class="btn btn-sm btn-outline-primary" onclick="event.stopPropagation()">
                                    <i class="bi bi-chat-dots"></i> ${i18n('agents.chat')}
                                </a>
                                <button type="button" class="btn btn-sm btn-outline-info" data-preview="${a.id}">
                                    <i class="bi bi-diagram-3"></i> ${i18n('agents.preview')}
                                </button>
                            </div>
                        </div>
                    </div>
                `).join('')}
            </div>
            ${this.nativeSection(native)}
            ${this.previewModalMarkup()}`;

        this.wireNative();
        App.container.querySelectorAll('[data-preview]').forEach(btn => {
            btn.onclick = (e) => { e.stopPropagation(); this.openPreviewById(btn.dataset.preview); };
        });
    },

    nativeSection(native) {
        if (!native || !native.length) return '';

        const rowFor = (n) => {
            const id = App.esc(n.id);
            let badge, action;
            if (!n.installed) {
                badge = `<span class="badge bg-secondary">${i18n('agents.statusNotInstalled')}</span>`;
                action = `<button class="btn btn-sm btn-primary" data-native-import="${id}"><i class="bi bi-download"></i> ${i18n('agents.import')}</button>`;
            } else if (n.modified) {
                badge = `<span class="badge bg-warning text-dark">${i18n('agents.statusModified')}</span>`;
                action = `<button class="btn btn-sm btn-outline-warning" data-native-reset="${id}"><i class="bi bi-arrow-counterclockwise"></i> ${i18n('agents.reset')}</button>`;
            } else {
                badge = `<span class="badge bg-success">${i18n('agents.statusInstalled')}</span>`;
                action = `<button class="btn btn-sm btn-outline-secondary" data-native-reimport="${id}"><i class="bi bi-arrow-repeat"></i> ${i18n('agents.reimport')}</button>`;
            }
            return `<tr>
                <td><code>${id}</code></td>
                <td>${App.esc(n.name)}</td>
                <td>${badge}</td>
                <td class="text-end">${action}</td>
            </tr>`;
        };

        return `
            <div class="card mt-4">
                <div class="card-header d-flex flex-wrap align-items-center gap-2">
                    <i class="bi bi-box-seam"></i> <strong>${i18n('agents.nativeTitle')}</strong>
                    <small class="text-secondary">${i18n('agents.nativeHint')}</small>
                </div>
                <div class="table-responsive">
                    <table class="table table-hover mb-0 align-middle">
                        <thead>
                            <tr><th>${i18n('common.id')}</th><th>${i18n('common.name')}</th><th>${i18n('common.status')}</th><th></th></tr>
                        </thead>
                        <tbody>${native.map(rowFor).join('')}</tbody>
                    </table>
                </div>
            </div>`;
    },

    wireNative() {
        App.container.querySelectorAll('[data-native-import]').forEach(btn => {
            btn.onclick = () => this.importNative(btn.dataset.nativeImport, false);
        });
        App.container.querySelectorAll('[data-native-reimport]').forEach(btn => {
            btn.onclick = () => this.importNative(btn.dataset.nativeReimport, true);
        });
        App.container.querySelectorAll('[data-native-reset]').forEach(btn => {
            btn.onclick = () => {
                if (!confirm(i18n('agents.confirmReset'))) return;
                this.importNative(btn.dataset.nativeReset, true);
            };
        });
    },

    async importNative(id, overwrite) {
        try {
            await App.api('POST', `/agents/native/${id}/import`, { overwrite });
            App.toast(i18n(overwrite ? 'agents.reimported' : 'agents.imported'));
            this.renderList();
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    /** Tool checkbox list: folder tools first, then one group per MCP server.
     *
     * Every input carries class `tool-check` and its value is exactly what goes
     * into agent.tools, so readForm() needs no special cases. Ids the agent holds
     * but that no longer exist anywhere are rendered checked+disabled: readForm()
     * reads checked inputs (disabled included), so opening and saving an agent
     * while a server is down cannot silently drop its selections. */
    toolPicker(tools, agentTools, mcpStatus) {
        const local = tools.filter(t => t.source !== 'mcp');
        const mcp = tools.filter(t => t.source === 'mcp');
        const status = mcpStatus || {};

        const row = (value, label, checked, extra = '', cls = '', attrs = '') => `
            <div class="form-check">
                <input class="form-check-input tool-check ${cls}" type="checkbox" value="${App.esc(value)}"
                       id="tool-${App.esc(value)}" ${checked ? 'checked' : ''} ${attrs}>
                <label class="form-check-label" for="tool-${App.esc(value)}">${label}</label>
                ${extra}
            </div>`;

        let html = local.map(t => row(
            t.id,
            `<strong>${App.esc(t.name)}</strong> <small class="text-secondary">(${App.esc(t.id)})</small>`,
            agentTools.includes(t.id),
        )).join('');

        // Group the MCP tools by server, each with a wildcard "all tools" entry.
        // Every configured server gets a group, even with no tools discovered yet:
        // that is where its error state shows, and the wildcard can be granted
        // before the first connection (it is resolved server-side per turn).
        const servers = Object.keys(status).sort().map(sid => ({ sid, tools: [] }));
        mcp.forEach(t => {
            const sid = (t.mcp || {}).server;
            let group = servers.find(g => g.sid === sid);
            if (!group) servers.push(group = { sid, tools: [] });
            group.tools.push(t);
        });
        servers.forEach(group => {
            const wildcard = `mcp:${group.sid}/*`;
            const all = agentTools.includes(wildcard);
            // The state belongs on the server, not on each tool: right after a
            // restart nothing is connected yet and the tools still work (the
            // first turn connects them), so only a real error is worth flagging.
            const state = (status[group.sid] || {}).state;
            const badge = state === 'error'
                ? `<span class="badge bg-danger ms-1" title="${App.esc((status[group.sid] || {}).last_error || '')}">${i18n('mcp.stateError')}</span>`
                : state === 'disabled'
                    ? `<span class="badge bg-secondary ms-1">${i18n('mcp.stateDisabled')}</span>` : '';
            html += `<div class="mt-2 pt-2 border-top" data-mcp-server="${App.esc(group.sid)}">
                ${row(wildcard,
                    `<i class="bi bi-plugin"></i> <strong>${App.esc(group.sid)}</strong>
                     <small class="text-secondary">${i18n('agents.mcpAllTools')}</small> ${badge}`,
                    all, '', 'mcp-all', `data-mcp-all="${App.esc(group.sid)}"`)}
                ${group.tools.length > 10 ? `<div class="form-text text-warning ms-4">${i18n('agents.mcpManyTools', { n: group.tools.length })}</div>` : ''}
                <div class="ms-4" data-mcp-group="${App.esc(group.sid)}">
                    ${group.tools.length === 0 ? `<div class="form-text text-secondary">${i18n('agents.mcpNoTools')}</div>` : ''}
                    ${group.tools.map(t => row(
                        t.id,
                        `${App.esc(t.name)} <small class="text-secondary">(${App.esc(t.id)})</small>`,
                        agentTools.includes(t.id),
                        '',
                        'mcp-tool',
                    )).join('')}
                </div>
            </div>`;
        });

        // Ids the agent references that no longer exist in any source.
        const known = new Set([...tools.map(t => t.id), ...servers.map(g => `mcp:${g.sid}/*`)]);
        const orphans = (agentTools || []).filter(id => !known.has(id));
        if (orphans.length) {
            html += `<div class="mt-2 pt-2 border-top">
                <div class="form-text text-secondary">${i18n('agents.toolsMissing')}</div>
                ${orphans.map(id => row(id,
                    `<span class="text-secondary">${App.esc(id)}</span>`, true, '', '', 'disabled')).join('')}
            </div>`;
        }

        return html || `<div class="text-secondary">${i18n('agents.noTools')}</div>`;
    },

    async renderForm(agentId) {
        let agent = { id: '', name: '', description: '', model_id: '', system_prompt: '', tools: [], max_iterations: 10, max_tool_calls: 5, temperature: 0.7, enabled: true, callable: true, callable_agents: ['*'] };
        let isEdit = false;

        if (agentId) {
            try {
                agent = await App.api('GET', `/agents/${agentId}`);
                isEdit = true;
            } catch (e) {
                App.toast(i18n('agents.notFound'), 'danger');
                location.hash = '#/agents';
                return;
            }
        }

        let models = [], tools = [], agents = [], mcpStatus = {};
        try { models = await App.api('GET', '/models'); } catch (e) { /* empty */ }
        try { tools = await App.api('GET', '/tools'); } catch (e) { /* empty */ }
        try { agents = await App.api('GET', '/agents'); } catch (e) { /* empty */ }
        // Per-server state, so a broken MCP server is visible in the tool picker.
        try { mcpStatus = await App.api('GET', '/mcp/status'); } catch (e) { /* empty */ }
        // Cache for the preview tree (opened from this form with unsaved values).
        this._allAgents = agents;
        this._allTools = tools;

        const agentTools = agent.tools || [];
        const callableList = agent.callable_agents || ['*'];
        const callableAll = callableList.includes('*');
        const otherAgents = agents.filter(a => a.id !== agent.id);

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3>${isEdit ? i18n('agents.editTitle') : i18n('agents.newTitle')}</h3>
                    <form id="agent-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.id')}</label>
                            <input type="text" class="form-control" id="f-id" value="${App.esc(agent.id)}" ${isEdit ? 'readonly' : ''} required
                                   pattern="[a-z0-9_-]+" title="${i18n('agents.idHint')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.name')}</label>
                            <input type="text" class="form-control" id="f-name" value="${App.esc(agent.name)}" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.description')}</label>
                            <input type="text" class="form-control" id="f-desc" value="${App.esc(agent.description || '')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('agents.model')}</label>
                            <select class="form-select" id="f-model" required>
                                <option value="">${i18n('agents.selectModel')}</option>
                                <option value="default" ${agent.model_id === 'default' ? 'selected' : ''}>${i18n('agents.defaultModel')}</option>
                                ${models.map(m => `<option value="${m.id}" ${m.id === agent.model_id ? 'selected' : ''}>${App.esc(m.name)} (${m.provider})</option>`).join('')}
                            </select>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('agents.systemPrompt')}</label>
                            <textarea class="form-control system-prompt-textarea" id="f-prompt" rows="8">${App.esc(agent.system_prompt)}</textarea>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('agents.tools')}</label>
                            <div class="border rounded p-2" style="max-height:260px;overflow-y:auto">
                                ${this.toolPicker(tools, agentTools, mcpStatus)}
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('agents.delegation')}</label>
                            <div class="border rounded p-2">
                                <div class="form-check">
                                    <input class="form-check-input" type="checkbox" id="f-callable" ${agent.callable !== false ? 'checked' : ''}>
                                    <label class="form-check-label" for="f-callable">${i18n('agents.callable')}</label>
                                    <div class="form-text">${i18n('agents.callableHelp')}</div>
                                </div>
                                <hr class="my-2">
                                <label class="form-label small mb-1">${i18n('agents.callableAgents')}</label>
                                <div class="form-check">
                                    <input class="form-check-input" type="checkbox" id="f-callable-all" ${callableAll ? 'checked' : ''}>
                                    <label class="form-check-label" for="f-callable-all">${i18n('agents.callableAll')}</label>
                                </div>
                                <div id="callable-list" class="ms-3 mt-1" style="max-height:160px;overflow-y:auto">
                                    ${otherAgents.map(a => `
                                        <div class="form-check">
                                            <input class="form-check-input agent-check" type="checkbox" value="${a.id}" id="ca-${a.id}"
                                                ${callableAll || callableList.includes(a.id) ? 'checked' : ''}>
                                            <label class="form-check-label" for="ca-${a.id}">
                                                <strong>${App.esc(a.name)}</strong> <small class="text-secondary">(${a.id})</small>
                                            </label>
                                        </div>
                                    `).join('')}
                                    ${otherAgents.length === 0 ? `<div class="text-secondary small">${i18n('agents.empty')}</div>` : ''}
                                </div>
                                <div class="form-text">${i18n('agents.callableHint')}</div>
                            </div>
                        </div>
                        <div class="row g-3 mb-3">
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('agents.maxIterations')}</label>
                                <input type="number" class="form-control" id="f-maxiter" value="${agent.max_iterations}" min="1" max="50">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('agents.maxToolCalls')}</label>
                                <input type="number" class="form-control" id="f-maxtools" value="${agent.max_tool_calls ?? 5}" min="1" max="50">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('agents.temperature')} <small class="text-secondary">${i18n('agents.temperatureHint')}</small></label>
                                <input type="number" class="form-control" id="f-temp" value="${agent.temperature ?? 0.7}" min="0" max="2" step="0.1">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('agents.respTemperature')} <small class="text-secondary">${i18n('agents.respTemperatureHint')}</small></label>
                                <input type="number" class="form-control" id="f-resp-temp" value="${agent.response_temperature ?? ''}" min="0" max="2" step="0.1"
                                       placeholder="${i18n('agents.respTempPlaceholder')}">
                            </div>
                        </div>
                        <div class="d-flex gap-2">
                            <button type="submit" class="btn btn-primary">${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                            <a href="#/agents" class="btn btn-secondary">${i18n('common.cancel')}</a>
                            <button type="button" class="btn btn-outline-info" id="btn-preview"><i class="bi bi-diagram-3"></i> ${i18n('agents.preview')}</button>
                            ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="btn-delete">${i18n('common.delete')}</button>` : ''}
                        </div>
                    </form>
                </div>
            </div>
            ${this.previewModalMarkup()}`;

        if (!isEdit) App.autoId('f-name', 'f-id');

        // "Tutti (*)" disables the per-agent allowlist below it.
        const callableAllEl = document.getElementById('f-callable-all');
        const syncCallable = () => {
            const off = callableAllEl.checked;
            document.querySelectorAll('.agent-check').forEach(c => { c.disabled = off; });
            document.getElementById('callable-list').style.opacity = off ? '0.5' : '1';
        };
        callableAllEl.onchange = syncCallable;
        syncCallable();

        // An MCP server's "all tools" entry supersedes its individual tools:
        // uncheck them as well as disabling them, since readForm() reads checked
        // inputs regardless of the disabled state (both grants would be sent).
        document.querySelectorAll('[data-mcp-all]').forEach(master => {
            const group = document.querySelector(`[data-mcp-group="${master.dataset.mcpAll}"]`);
            const sync = () => {
                group.querySelectorAll('.mcp-tool').forEach(c => {
                    if (master.checked) c.checked = false;
                    c.disabled = master.checked;
                });
                group.style.opacity = master.checked ? '0.5' : '1';
            };
            master.onchange = sync;
            sync();
        });

        // Read the current (possibly unsaved) form values into an agent object.
        const readForm = () => {
            const respTempVal = document.getElementById('f-resp-temp').value;
            return {
                id: document.getElementById('f-id').value.trim(),
                name: document.getElementById('f-name').value.trim(),
                description: document.getElementById('f-desc').value.trim(),
                model_id: document.getElementById('f-model').value,
                system_prompt: document.getElementById('f-prompt').value,
                tools: [...document.querySelectorAll('.tool-check:checked')].map(c => c.value),
                max_iterations: parseInt(document.getElementById('f-maxiter').value) || 10,
                max_tool_calls: parseInt(document.getElementById('f-maxtools').value) || 5,
                temperature: parseFloat(document.getElementById('f-temp').value) || 0.7,
                response_temperature: respTempVal ? parseFloat(respTempVal) : null,
                enabled: true,
                callable: document.getElementById('f-callable').checked,
                callable_agents: callableAllEl.checked
                    ? ['*']
                    : [...document.querySelectorAll('.agent-check:checked')].map(c => c.value),
            };
        };

        document.getElementById('btn-preview').onclick = () => {
            const draft = readForm();
            draft.id = draft.id || '(new)';
            draft.name = draft.name || draft.id;
            this.openPreview(draft);
        };

        document.getElementById('agent-form').onsubmit = async (e) => {
            e.preventDefault();
            const data = readForm();
            try {
                if (isEdit) {
                    await App.api('PUT', `/agents/${agentId}`, data);
                } else {
                    await App.api('POST', '/agents', data);
                }
                App.toast(i18n('agents.saved'));
                location.hash = '#/agents';
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        if (isEdit) {
            document.getElementById('btn-delete').onclick = async () => {
                if (!confirm(i18n('agents.confirmDelete'))) return;
                try {
                    await App.api('DELETE', `/agents/${agentId}`);
                    App.toast(i18n('agents.deleted'));
                    location.hash = '#/agents';
                } catch (err) {
                    App.toast(err.message, 'danger');
                }
            };
        }
    },

    // ---- Agent preview (delegation + tools tree) --------------------------
    MAX_PREVIEW_DEPTH: 5,

    previewModalMarkup() {
        return `
            <div class="modal fade" id="agent-preview-modal" tabindex="-1" aria-hidden="true">
                <div class="modal-dialog modal-dialog-scrollable modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title"><i class="bi bi-diagram-3"></i> <span id="agent-preview-title"></span></h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                        <div class="modal-body"><div id="agent-preview-body" class="agent-tree"></div></div>
                    </div>
                </div>
            </div>`;
    },

    // Load agents+tools if a caller (e.g. list card) opens the preview before
    // the form/list has cached them.
    async ensurePreviewData() {
        const jobs = [];
        if (!this._allTools) jobs.push(App.api('GET', '/tools').then(t => this._allTools = t).catch(() => this._allTools = []));
        if (!this._allAgents) jobs.push(App.api('GET', '/agents').then(a => this._allAgents = a).catch(() => this._allAgents = []));
        if (jobs.length) await Promise.all(jobs);
    },

    async openPreviewById(agentId) {
        await this.ensurePreviewData();
        const agent = (this._allAgents || []).find(a => a.id === agentId);
        if (!agent) { App.toast(i18n('agents.notFound'), 'danger'); return; }
        this.openPreview(agent);
    },

    openPreview(agent) {
        const el = document.getElementById('agent-preview-modal');
        if (!el) return;
        const agentsById = {};
        (this._allAgents || []).forEach(a => { agentsById[a.id] = a; });
        agentsById[agent.id] = agent;  // reflect the (possibly unsaved) root
        const toolsById = {};
        (this._allTools || []).forEach(t => { toolsById[t.id] = t; });

        const contents = this.renderAgentContents(agent, { agentsById, toolsById, path: new Set([agent.id]), depth: 0 });
        const modelBadge = agent.model_id ? `<span class="badge bg-secondary">${App.esc(agent.model_id)}</span>` : '';
        document.getElementById('agent-preview-title').textContent = i18n('agents.previewTitle', { name: agent.name || agent.id });
        document.getElementById('agent-preview-body').innerHTML =
            `<div class="agent-node"><div class="agent-node-head"><i class="bi bi-robot"></i> <strong>${App.esc(agent.name || agent.id)}</strong> ${modelBadge}</div>${contents}</div>`;
        bootstrap.Modal.getOrCreateInstance(el).show();
    },

    // Recursively render an agent's tools + the sub-agents it may delegate to.
    // Mirrors the backend AgentExecutor._agent_can_call gate exactly.
    renderAgentContents(agent, ctx) {
        const { agentsById, toolsById, path, depth } = ctx;
        const toolIds = agent.tools || [];
        const hasCall = toolIds.includes('call_agent');

        const toolItems = toolIds.map(tid => {
            const wildcard = /^mcp:([a-z0-9][a-z0-9_-]*)\/\*$/.exec(tid);
            if (wildcard) {
                return `<li><i class="bi bi-plugin"></i> <strong>${App.esc(wildcard[1])}</strong>
                        <small class="text-secondary">${i18n('agents.mcpAllTools')}</small></li>`;
            }
            const t = toolsById[tid];
            const label = t ? App.esc(t.name) : App.esc(tid);
            const icon = tid === 'call_agent' ? 'bi-diagram-3'
                : (t && t.source === 'mcp') ? 'bi-plugin' : 'bi-gear-wide-connected';
            return `<li><i class="bi ${icon}"></i> ${label} <small class="text-secondary">(${App.esc(tid)})</small></li>`;
        }).join('');

        let childItems = '';
        if (hasCall) {
            if (depth >= this.MAX_PREVIEW_DEPTH) {
                childItems = `<li class="text-secondary"><i class="bi bi-info-circle"></i> ${i18n('agents.previewDepth')}</li>`;
            } else {
                const allow = agent.callable_agents || ['*'];
                const targets = Object.values(agentsById).filter(t =>
                    t.id !== agent.id &&
                    t.enabled !== false &&
                    t.callable !== false &&
                    (allow.includes('*') || allow.includes(t.id))
                );
                if (targets.length === 0) {
                    childItems = `<li class="text-secondary"><i class="bi bi-slash-circle"></i> ${i18n('agents.previewNoDelegate')}</li>`;
                } else {
                    childItems = targets.map(t => {
                        const head = `<i class="bi bi-robot"></i> ${App.esc(t.name)} <small class="text-secondary">(${App.esc(t.id)})</small>`;
                        if (path.has(t.id)) {
                            return `<li>${head} <span class="badge bg-secondary">${i18n('agents.previewCycle')}</span></li>`;
                        }
                        const inner = this.renderAgentContents(t, { agentsById, toolsById, path: new Set([...path, t.id]), depth: depth + 1 });
                        return `<li><details><summary>${head}</summary>${inner}</details></li>`;
                    }).join('');
                }
            }
        }

        return `<ul>${toolItems || `<li class="text-secondary">${i18n('agents.noTools')}</li>`}${childItems}</ul>`;
    },
};
