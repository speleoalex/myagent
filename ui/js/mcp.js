/**
 * MCP servers: a tab of the Tools area (#/tools/mcp).
 *
 * Reached through ToolsPage.render(), which forwards the sub-route here, so the
 * Tools nav entry stays highlighted and both views share the same pill row.
 */
const McpPage = {
    async render(params) {
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    // ------------------------------------------------------------------
    // List
    // ------------------------------------------------------------------

    async renderList() {
        let servers = [];
        try { servers = await App.api('GET', '/mcp'); } catch (e) { /* empty */ }

        App.container.innerHTML = `
            ${ToolsPage.tabs('mcp')}
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-plugin"></i> ${i18n('mcp.title')}</h3>
                <div class="d-flex flex-wrap gap-2">
                    <button class="btn btn-outline-secondary" id="btn-import"><i class="bi bi-filetype-json"></i> ${i18n('mcp.import')}</button>
                    <a href="#/tools/mcp/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('mcp.new')}</a>
                </div>
            </div>
            <p class="text-secondary">${i18n('mcp.hint')}</p>
            <div id="import-panel" class="card mb-3 d-none">
                <div class="card-header"><i class="bi bi-filetype-json"></i> <strong>${i18n('mcp.importTitle')}</strong></div>
                <div class="card-body">
                    <p class="text-secondary small mb-2">${i18n('mcp.importHint')}</p>
                    <textarea class="form-control font-monospace" id="f-import" rows="8" spellcheck="false"
                              placeholder='${App.esc(JSON.stringify({ mcpServers: { filesystem: { command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'] } } }, null, 2))}'></textarea>
                    <div class="form-check mt-2">
                        <input class="form-check-input" type="checkbox" id="f-import-overwrite">
                        <label class="form-check-label" for="f-import-overwrite">${i18n('mcp.importOverwrite')}</label>
                    </div>
                    <button class="btn btn-primary mt-2" id="btn-do-import">${i18n('mcp.importDo')}</button>
                    <div id="import-result" class="mt-2"></div>
                </div>
            </div>
            ${servers.length === 0 ? `
                <div class="alert alert-secondary">
                    <strong>${i18n('mcp.empty')}</strong><br>${i18n('mcp.emptyHint')}
                </div>` : `
                <div class="table-responsive">
                    <table class="table table-hover align-middle">
                        <thead>
                            <tr>
                                <th>${i18n('common.id')}</th><th>${i18n('common.name')}</th>
                                <th>${i18n('mcp.colTransport')}</th><th>${i18n('common.status')}</th>
                                <th>${i18n('mcp.colTools')}</th><th></th>
                            </tr>
                        </thead>
                        <tbody>${servers.map(s => this.row(s)).join('')}</tbody>
                    </table>
                </div>`}`;

        this.wireList();
    },

    row(s) {
        const id = App.esc(s.id);
        const status = s.status || {};
        const target = s.transport === 'http' ? s.url : [s.command, ...(s.args || [])].join(' ');
        return `<tr>
            <td><code>${id}</code></td>
            <td>
                ${App.esc(s.name || s.id)}
                <div class="small text-secondary text-truncate" style="max-width:20rem"
                     title="${App.esc(target || '')}">${App.esc(target || '')}</div>
            </td>
            <td><span class="badge bg-secondary">${s.transport === 'http' ? i18n('mcp.transportHttp') : i18n('mcp.transportStdio')}</span></td>
            <td>${this.stateBadge(status, s.enabled)}</td>
            <td class="text-nowrap">
                ${status.tool_count || 0}
                ${status.tools_cached ? `<small class="text-secondary">${i18n('mcp.cachedTools')}</small>` : ''}
            </td>
            <td class="text-end text-nowrap">
                <button class="btn btn-sm btn-outline-secondary" data-refresh="${id}" title="${i18n('mcp.refresh')}"><i class="bi bi-arrow-repeat"></i></button>
                <a href="#/tools/mcp/${id}" class="btn btn-sm btn-outline-primary">${i18n('common.edit')}</a>
            </td>
        </tr>`;
    },

    stateBadge(status, enabled) {
        if (enabled === false) return `<span class="badge bg-secondary">${i18n('mcp.stateDisabled')}</span>`;
        const state = status.state || 'idle';
        if (state === 'ready') return `<span class="badge bg-success">${i18n('mcp.stateReady')}</span>`;
        if (state === 'error') {
            return `<span class="badge bg-danger">${i18n('mcp.stateError')}</span>
                    <div class="small text-danger text-truncate" style="max-width:16rem"
                         title="${App.esc(status.last_error || '')}">${App.esc(status.last_error || '')}</div>`;
        }
        return `<span class="badge bg-secondary">${i18n('mcp.stateIdle')}</span>`;
    },

    wireList() {
        const panel = document.getElementById('import-panel');
        const btnImport = document.getElementById('btn-import');
        if (btnImport) btnImport.onclick = () => panel.classList.toggle('d-none');
        const btnDo = document.getElementById('btn-do-import');
        if (btnDo) btnDo.onclick = () => this.doImport();

        App.container.querySelectorAll('[data-refresh]').forEach(btn => {
            btn.onclick = async () => {
                const id = btn.dataset.refresh;
                btn.disabled = true;
                btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
                try {
                    const res = await App.api('POST', `/mcp/${id}/refresh`, {});
                    const st = res.status || {};
                    if (st.state === 'ready') {
                        App.toast(i18n('mcp.refreshed', { n: st.tool_count || 0 }));
                    } else {
                        App.toast(st.last_error || i18n('mcp.stateError'), 'danger');
                    }
                } catch (err) {
                    App.toast(err.message, 'danger');
                }
                this.renderList();
            };
        });
    },

    async doImport() {
        const raw = document.getElementById('f-import').value.trim();
        const out = document.getElementById('import-result');
        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (e) {
            App.toast(i18n('mcp.importInvalid'), 'danger');
            return;
        }
        try {
            const res = await App.api('POST', '/mcp/import', {
                config: parsed,
                overwrite: document.getElementById('f-import-overwrite').checked,
            });
            const created = (res.created || []).map(c => c.id);
            const skipped = res.skipped || [];
            out.innerHTML = `
                ${created.length ? `<div class="alert alert-success mb-2">${i18n('mcp.importCreated', { list: App.esc(created.join(', ')) })}</div>` : ''}
                ${skipped.length ? `<div class="alert alert-warning mb-0"><ul class="mb-0">${skipped.map(s =>
                    `<li><code>${App.esc(s.id || s.name || '?')}</code>: ${App.esc(s.reason || '')}</li>`).join('')}</ul></div>` : ''}`;
            if (created.length) {
                App.toast(i18n('mcp.imported', { n: created.length }));
                // The catalogue is warmed in the background; reload shortly so the
                // tool counts show up without another click.
                setTimeout(() => this.renderList(), 1500);
            }
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    // ------------------------------------------------------------------
    // Form
    // ------------------------------------------------------------------

    async renderForm(serverId) {
        let server = {
            id: '', name: '', transport: 'stdio', enabled: true,
            command: '', args: [], env: {}, cwd: '',
            url: '', headers: {}, bearer: '',
            connect_timeout: 20, timeout: 60, max_output: 10000, max_tools: 32,
            tools_ttl: 300, allow_tools: [], deny_tools: [],
        };
        let isEdit = false;
        if (serverId) {
            try {
                server = await App.api('GET', `/mcp/${serverId}`);
                isEdit = true;
            } catch (e) {
                App.toast(i18n('mcp.notFound'), 'danger');
                location.hash = '#/tools/mcp';
                return;
            }
        }
        const isHttp = server.transport === 'http';

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3>${isEdit ? i18n('mcp.editTitle') : i18n('mcp.newTitle')}</h3>
                    <form id="mcp-form">
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.id')}</label>
                            <input type="text" class="form-control" id="f-id" value="${App.esc(server.id)}" ${isEdit ? 'readonly' : ''} required
                                   pattern="[a-z0-9][a-z0-9_-]{0,23}" title="${i18n('mcp.idHint')}">
                            <div class="form-text">${i18n('mcp.idHint')}</div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('common.name')}</label>
                            <input type="text" class="form-control" id="f-name" value="${App.esc(server.name || '')}">
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('mcp.transport')}</label>
                            <div class="d-flex gap-3">
                                <div class="form-check">
                                    <input class="form-check-input" type="radio" name="transport" id="f-tr-stdio" value="stdio" ${isHttp ? '' : 'checked'}>
                                    <label class="form-check-label" for="f-tr-stdio">${i18n('mcp.transportStdio')}</label>
                                </div>
                                <div class="form-check">
                                    <input class="form-check-input" type="radio" name="transport" id="f-tr-http" value="http" ${isHttp ? 'checked' : ''}>
                                    <label class="form-check-label" for="f-tr-http">${i18n('mcp.transportHttp')}</label>
                                </div>
                            </div>
                            <div class="form-text" id="transport-hint"></div>
                        </div>

                        <div id="block-stdio" class="${isHttp ? 'd-none' : ''}">
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.command')}</label>
                                <input type="text" class="form-control" id="f-command" value="${App.esc(server.command || '')}" placeholder="npx">
                                <div class="form-text">${i18n('mcp.commandHint')}</div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.args')}</label>
                                <textarea class="form-control font-monospace" id="f-args" rows="4" spellcheck="false"
                                          placeholder="-y&#10;@modelcontextprotocol/server-filesystem&#10;/tmp">${App.esc((server.args || []).join('\n'))}</textarea>
                                <div class="form-text">${i18n('mcp.argsHint')}</div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.env')}</label>
                                <textarea class="form-control font-monospace" id="f-env" rows="3" spellcheck="false"
                                          placeholder="API_TOKEN=...">${App.esc(this.kvText(server.env, '='))}</textarea>
                                <div class="form-text">${i18n('mcp.envHint')}</div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.cwd')}</label>
                                <input type="text" class="form-control" id="f-cwd" value="${App.esc(server.cwd || '')}">
                                <div class="form-text">${i18n('mcp.cwdHint')}</div>
                            </div>
                        </div>

                        <div id="block-http" class="${isHttp ? '' : 'd-none'}">
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.url')}</label>
                                <input type="text" class="form-control" id="f-url" value="${App.esc(server.url || '')}" placeholder="http://127.0.0.1:3001/mcp">
                                <div class="form-text">${i18n('mcp.urlHint')}</div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.bearer')}</label>
                                <input type="password" class="form-control" id="f-bearer" value="${App.esc(server.bearer || '')}" autocomplete="off">
                                <div class="form-text">${i18n('mcp.bearerHint')}</div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${i18n('mcp.headers')}</label>
                                <textarea class="form-control font-monospace" id="f-headers" rows="3" spellcheck="false"
                                          placeholder="X-Api-Key: ...">${App.esc(this.kvText(server.headers, ': '))}</textarea>
                                <div class="form-text">${i18n('mcp.headersHint')}</div>
                            </div>
                        </div>

                        <div class="row g-3 mb-3">
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('mcp.connectTimeout')}</label>
                                <input type="number" class="form-control" id="f-connect-timeout" value="${server.connect_timeout ?? 20}" min="5" max="300">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('mcp.timeout')}</label>
                                <input type="number" class="form-control" id="f-timeout" value="${server.timeout ?? 60}" min="1" max="600">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('mcp.maxOutput')}</label>
                                <input type="number" class="form-control" id="f-maxout" value="${server.max_output ?? 10000}" min="500" max="200000">
                            </div>
                            <div class="col-6 col-md-3">
                                <label class="form-label">${i18n('mcp.maxTools')}</label>
                                <input type="number" class="form-control" id="f-maxtools" value="${server.max_tools ?? 32}" min="1" max="200">
                            </div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('mcp.allowTools')}</label>
                            <input type="text" class="form-control font-monospace" id="f-allow" value="${App.esc((server.allow_tools || []).join(', '))}">
                            <div class="form-text">${i18n('mcp.allowHint')}</div>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">${i18n('mcp.denyTools')}</label>
                            <input type="text" class="form-control font-monospace" id="f-deny" value="${App.esc((server.deny_tools || []).join(', '))}">
                            <div class="form-text">${i18n('mcp.denyHint')}</div>
                        </div>
                        <div class="mb-3">
                            <div class="form-check">
                                <input class="form-check-input" type="checkbox" id="f-enabled" ${server.enabled !== false ? 'checked' : ''}>
                                <label class="form-check-label" for="f-enabled">${i18n('mcp.enabled')}</label>
                                <div class="form-text">${i18n('mcp.enabledHint')}</div>
                            </div>
                        </div>

                        <div class="d-flex flex-wrap gap-2">
                            <button type="submit" class="btn btn-primary">${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                            <a href="#/tools/mcp" class="btn btn-secondary">${i18n('common.cancel')}</a>
                            <button type="button" class="btn btn-outline-info" id="btn-test"><i class="bi bi-plug"></i> ${i18n('mcp.test')}</button>
                            ${isEdit ? `<button type="button" class="btn btn-danger ms-auto" id="btn-delete">${i18n('common.delete')}</button>` : ''}
                        </div>
                        <div class="form-text mt-2">${i18n('mcp.testHint')}</div>
                        <div id="test-result" class="mt-3"></div>
                    </form>
                </div>
            </div>`;

        if (!isEdit) App.autoId('f-name', 'f-id');

        const syncTransport = () => {
            const http = document.getElementById('f-tr-http').checked;
            document.getElementById('block-http').classList.toggle('d-none', !http);
            document.getElementById('block-stdio').classList.toggle('d-none', http);
            document.getElementById('transport-hint').textContent =
                i18n(http ? 'mcp.transportHttpHint' : 'mcp.transportStdioHint');
        };
        document.getElementById('f-tr-stdio').onchange = syncTransport;
        document.getElementById('f-tr-http').onchange = syncTransport;
        syncTransport();

        document.getElementById('btn-test').onclick = () => this.testConnection();

        document.getElementById('mcp-form').onsubmit = async (e) => {
            e.preventDefault();
            const data = this.readForm();
            try {
                if (isEdit) await App.api('PUT', `/mcp/${serverId}`, data);
                else await App.api('POST', '/mcp', data);
                App.toast(i18n('mcp.saved'));
                location.hash = '#/tools/mcp';
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };

        const del = document.getElementById('btn-delete');
        if (del) del.onclick = async () => {
            if (!confirm(i18n('mcp.confirmDelete'))) return;
            try {
                await App.api('DELETE', `/mcp/${serverId}`);
                App.toast(i18n('mcp.deleted'));
                location.hash = '#/tools/mcp';
            } catch (err) {
                App.toast(err.message, 'danger');
            }
        };
    },

    readForm() {
        const http = document.getElementById('f-tr-http').checked;
        const val = (id) => (document.getElementById(id).value || '').trim();
        const list = (id) => val(id).split(',').map(s => s.trim()).filter(Boolean);
        const data = {
            id: val('f-id'),
            name: val('f-name'),
            transport: http ? 'http' : 'stdio',
            enabled: document.getElementById('f-enabled').checked,
            connect_timeout: parseInt(val('f-connect-timeout')) || 20,
            timeout: parseInt(val('f-timeout')) || 60,
            max_output: parseInt(val('f-maxout')) || 10000,
            max_tools: parseInt(val('f-maxtools')) || 32,
            allow_tools: list('f-allow'),
            deny_tools: list('f-deny'),
        };
        if (http) {
            data.url = val('f-url');
            data.bearer = document.getElementById('f-bearer').value;
            data.headers = this.parseKv(val('f-headers'), ':');
        } else {
            data.command = val('f-command');
            data.args = val('f-args').split('\n').map(s => s.trim()).filter(Boolean);
            data.env = this.parseKv(val('f-env'), '=');
            data.cwd = val('f-cwd');
        }
        return data;
    },

    async testConnection() {
        const btn = document.getElementById('btn-test');
        const out = document.getElementById('test-result');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${i18n('mcp.testing')}`;
        out.innerHTML = '';
        try {
            const res = await App.api('POST', '/mcp/test', this.readForm());
            out.innerHTML = res.ok ? this.testOkPanel(res) : this.testFailPanel(res);
        } catch (err) {
            out.innerHTML = `<div class="alert alert-danger mb-0">${App.esc(err.message)}</div>`;
        }
        btn.disabled = false;
        btn.innerHTML = original;
    },

    testOkPanel(res) {
        const info = res.server_info || {};
        const many = (res.tool_count || 0) > 10;
        return `
            <div class="alert alert-success mb-2">
                <strong>${i18n('mcp.testOk', { n: res.tool_count || 0 })}</strong>
                <div class="small">
                    ${App.esc(info.name || '')} ${App.esc(info.version || '')} ·
                    MCP ${App.esc(res.protocol_version || '')} · ${res.elapsed_ms || 0} ms
                </div>
            </div>
            ${many ? `<div class="alert alert-warning py-2 small">${i18n('mcp.manyTools', { n: res.tool_count })}</div>` : ''}
            <div class="table-responsive" style="max-height:18rem;overflow-y:auto">
                <table class="table table-sm table-hover mb-0">
                    <thead><tr><th>${i18n('mcp.colTool')}</th><th>${i18n('mcp.colToolId')}</th><th>${i18n('mcp.colToolParams')}</th></tr></thead>
                    <tbody>${(res.tools || []).map(t => `
                        <tr>
                            <td>${App.esc(t.remote_name)}<div class="small text-secondary">${App.esc(t.description || '')}</div></td>
                            <td><code class="small">${App.esc(t.id)}</code></td>
                            <td class="small">${App.esc((t.parameters || []).join(', '))}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>
            ${this.skippedPanel(res.skipped)}
            ${this.stderrPanel(res.stderr_tail)}`;
    },

    testFailPanel(res) {
        return `
            <div class="alert alert-danger mb-2">
                <strong>${i18n('mcp.testFail')}</strong>
                <div class="small font-monospace">${App.esc(res.error || '')}</div>
            </div>
            ${this.stderrPanel(res.stderr_tail)}`;
    },

    skippedPanel(skipped) {
        if (!skipped || !skipped.length) return '';
        return `<div class="alert alert-warning py-2 mt-2 mb-0 small">
            <strong>${i18n('mcp.skipped')}</strong>
            <ul class="mb-0">${skipped.map(s =>
                `<li><code>${App.esc(s.tool)}</code>: ${App.esc(s.reason)}</li>`).join('')}</ul>
        </div>`;
    },

    stderrPanel(lines) {
        if (!lines || !lines.length) return '';
        return `<details class="mt-2">
            <summary class="small text-secondary">${i18n('mcp.stderr')}</summary>
            <pre class="small bg-body-tertiary p-2 mb-0" style="max-height:12rem;overflow:auto">${App.esc(lines.join('\n'))}</pre>
        </details>`;
    },

    // ------------------------------------------------------------------
    // key/value textarea helpers
    // ------------------------------------------------------------------

    kvText(obj, sep) {
        return Object.entries(obj || {}).map(([k, v]) => `${k}${sep}${v}`).join('\n');
    },

    parseKv(text, sep) {
        const out = {};
        (text || '').split('\n').forEach(line => {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('#')) return;
            const at = trimmed.indexOf(sep);
            if (at <= 0) return;
            out[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim();
        });
        return out;
    },
};
