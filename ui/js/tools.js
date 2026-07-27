const ToolsPage = {
    async render(params) {
        // Must come first: the catch-all below would treat 'mcp' as a tool id.
        if (params[0] === 'mcp') return McpPage.render(params.slice(1));
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    /** Pill row shared by the tools list and the MCP views.
     *
     * Plain buttons, not .nav-link: App.updateActiveNav() toggles .active on
     * every .nav-link whose href prefixes the hash, which would light up both
     * pills at once on #/tools/mcp. */
    tabs(active) {
        const pill = (href, key, icon, id) =>
            `<a href="${href}" class="btn btn-sm ${active === id ? 'btn-primary' : 'btn-outline-secondary'}">
                <i class="bi bi-${icon}"></i> ${i18n(key)}
            </a>`;
        return `<div class="d-flex flex-wrap gap-2 mb-3">
            ${pill('#/tools', 'tools.title', 'tools', 'tools')}
            ${pill('#/tools/mcp', 'mcp.title', 'plugin', 'mcp')}
        </div>`;
    },

    async renderList() {
        let tools = [];
        try { tools = await App.api('GET', '/tools'); } catch (e) { /* empty */ }
        // MCP tools come from external servers and have no folder to edit: they
        // live in their own tab, and only their count is mentioned here.
        const mcpCount = tools.filter(t => t.source === 'mcp').length;
        tools = tools.filter(t => t.source !== 'mcp');
        let native = [];
        try { native = await App.api('GET', '/tools/native'); } catch (e) { /* no catalog */ }

        const paramCount = (t) => {
            const props = t.parameters?.properties;
            return props ? Object.keys(props).length : 0;
        };

        App.container.innerHTML = `
            ${this.tabs('tools')}
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-tools"></i> ${i18n('tools.title')}</h3>
                <a href="#/tools/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('tools.new')}</a>
            </div>
            <div class="table-responsive">
                <table class="table table-hover">
                    <thead>
                        <tr><th>${i18n('common.id')}</th><th>${i18n('common.name')}</th><th>${i18n('tools.colType')}</th><th>${i18n('tools.colParameters')}</th><th></th></tr>
                    </thead>
                    <tbody>
                        ${tools.map(t => `
                            <tr>
                                <td><code>${App.esc(t.id)}</code></td>
                                <td>${App.esc(t.name)}</td>
                                <td><span class="badge bg-${t.internal ? 'warning' : 'secondary'}">${t.internal ? i18n('tools.typeInternal') : i18n('tools.typeScript')}</span></td>
                                <td>${paramCount(t)}</td>
                                <td>
                                    ${!t.internal ? `<a href="#/tools/${t.id}" class="btn btn-sm btn-outline-primary">${i18n('common.edit')}</a>` : ''}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
            ${mcpCount ? `<p class="text-secondary"><i class="bi bi-plugin"></i>
                ${i18n('tools.mcpCount', { n: mcpCount })}
                <a href="#/tools/mcp">${i18n('tools.mcpManage')}</a></p>` : ''}
            ${this.nativeSection(native)}`;

        this.wireNative();
    },

    nativeSection(native) {
        if (!native || !native.length) return '';

        const rowFor = (n) => {
            const id = App.esc(n.id);
            let badge, action;
            if (!n.installed) {
                badge = `<span class="badge bg-secondary">${i18n('tools.statusNotInstalled')}</span>`;
                action = `<button class="btn btn-sm btn-primary" data-native-import="${id}"><i class="bi bi-download"></i> ${i18n('tools.import')}</button>`;
            } else if (n.modified) {
                badge = `<span class="badge bg-warning text-dark">${i18n('tools.statusModified')}</span>`;
                action = `<button class="btn btn-sm btn-outline-warning" data-native-reset="${id}"><i class="bi bi-arrow-counterclockwise"></i> ${i18n('tools.reset')}</button>`;
            } else {
                badge = `<span class="badge bg-success">${i18n('tools.statusInstalled')}</span>`;
                action = `<button class="btn btn-sm btn-outline-secondary" data-native-reimport="${id}"><i class="bi bi-arrow-repeat"></i> ${i18n('tools.reimport')}</button>`;
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
                    <i class="bi bi-box-seam"></i> <strong>${i18n('tools.nativeTitle')}</strong>
                    <small class="text-secondary">${i18n('tools.nativeHint')}</small>
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
                if (!confirm(i18n('tools.confirmReset'))) return;
                this.importNative(btn.dataset.nativeReset, true);
            };
        });
    },

    async importNative(id, overwrite) {
        try {
            await App.api('POST', `/tools/native/${id}/import`, { overwrite });
            App.toast(i18n(overwrite ? 'tools.reimported' : 'tools.imported'));
            this.renderList();
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    async renderForm(toolId) {
        let tool = { id: '', name: '', description: '', parameters: {type:'object',properties:{},required:[]}, enabled: true };
        let isEdit = false;
        let script = '#!/bin/bash\n# Read JSON from stdin\nINPUT=$(cat)\necho "Hello from tool"\n';

        if (toolId) {
            try {
                tool = await App.api('GET', `/tools/${toolId}`);
                isEdit = true;
                if (tool.internal) {
                    App.toast(i18n('tools.internalNoEdit'), 'danger');
                    location.hash = '#/tools';
                    return;
                }
                // Load run script source
                try {
                    const src = await App.api('GET', `/tools/${toolId}/source`);
                    script = src.script || '';
                } catch (e) { /* no run file */ }
            } catch (e) {
                App.toast(i18n('tools.notFound'), 'danger');
                location.hash = '#/tools';
                return;
            }
        }

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3>${isEdit ? i18n('tools.editTitle') : i18n('tools.newTitle')}</h3>
                    <form id="tool-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.id')}</label>
                            <input type="text" class="form-control" id="f-id" value="${App.esc(tool.id)}" ${isEdit ? 'readonly' : ''} required
                                   pattern="[a-z0-9_-]+">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.name')}</label>
                            <input type="text" class="form-control" id="f-name" value="${App.esc(tool.name)}" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.description')}</label>
                            <textarea class="form-control" id="f-desc" rows="2">${App.esc(tool.description || '')}</textarea>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('tools.scriptLabel')} <small class="text-secondary">${i18n('tools.scriptHint')}</small></label>
                            <textarea class="form-control font-monospace" id="f-script" rows="12" spellcheck="false">${App.esc(script)}</textarea>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('tools.parametersLabel')} <small class="text-secondary">${i18n('tools.parametersHint')}</small></label>
                            <textarea class="form-control font-monospace" id="f-params" rows="8" spellcheck="false">${JSON.stringify(tool.parameters || {type:'object',properties:{},required:[]}, null, 2)}</textarea>
                        </div>
                        <div class="row mb-3">
                            <div class="col-md-6">
                                <label class="form-label">${i18n('tools.timeout')}</label>
                                <input type="number" class="form-control" id="f-timeout" value="${tool.timeout ?? 30}" min="1" max="300">
                            </div>
                            <div class="col-md-6">
                                <label class="form-label">${i18n('tools.maxOutput')}</label>
                                <input type="number" class="form-control" id="f-maxout" value="${tool.max_output ?? 10000}" min="100" max="100000">
                            </div>
                        </div>
                        <div class="d-flex gap-2">
                            <button type="submit" class="btn btn-primary">${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                            <a href="#/tools" class="btn btn-secondary">${i18n('common.cancel')}</a>
                            ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="btn-delete">${i18n('common.delete')}</button>` : ''}
                        </div>
                    </form>
                </div>
            </div>`;

        if (!isEdit) App.autoId('f-name', 'f-id');

        // Tab key in script textarea
        document.getElementById('f-script').addEventListener('keydown', (e) => {
            if (e.key === 'Tab') {
                e.preventDefault();
                const ta = e.target;
                const start = ta.selectionStart;
                ta.value = ta.value.substring(0, start) + '    ' + ta.value.substring(ta.selectionEnd);
                ta.selectionStart = ta.selectionEnd = start + 4;
            }
        });

        document.getElementById('tool-form').onsubmit = async (e) => {
            e.preventDefault();
            let params;
            try {
                params = JSON.parse(document.getElementById('f-params').value || '{}');
            } catch (err) {
                App.toast(i18n('tools.invalidParams'), 'danger');
                return;
            }
            const data = {
                id: document.getElementById('f-id').value.trim(),
                name: document.getElementById('f-name').value.trim(),
                description: document.getElementById('f-desc').value.trim(),
                parameters: params,
                script: document.getElementById('f-script').value,
                timeout: parseInt(document.getElementById('f-timeout').value) || 30,
                max_output: parseInt(document.getElementById('f-maxout').value) || 10000,
            };
            try {
                if (isEdit) {
                    await App.api('PUT', `/tools/${toolId}`, data);
                } else {
                    await App.api('POST', '/tools', data);
                }
                App.toast(i18n('tools.saved'));
                location.hash = '#/tools';
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        if (isEdit) {
            document.getElementById('btn-delete').onclick = async () => {
                if (!confirm(i18n('tools.confirmDelete'))) return;
                try {
                    await App.api('DELETE', `/tools/${toolId}`);
                    App.toast(i18n('tools.deleted'));
                    location.hash = '#/tools';
                } catch (err) {
                    App.toast(err.message, 'danger');
                }
            };
        }
    },
};
