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
        // One list, one source: native tools are always present (served from
        // the app bundle, no install step) and each entry already carries its
        // overlay state (origin / modified). Ungrouped tools first (by id),
        // then the groups (categories) as labeled sections.
        this._rows = tools.filter(t => t.source !== 'mcp').sort((a, b) =>
            (a.category || '').localeCompare(b.category || '') || a.id.localeCompare(b.id));

        App.container.innerHTML = `
            ${this.tabs('tools')}
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-tools"></i> ${i18n('tools.title')}</h3>
                <a href="#/tools/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('tools.new')}</a>
            </div>
            <div class="mb-3">
                <input type="text" id="tools-filter" class="form-control"
                       placeholder="${i18n('tools.filterPlaceholder')}" autocomplete="off">
            </div>
            <div id="tools-sections"></div>
            ${mcpCount ? `<p class="text-secondary"><i class="bi bi-plugin"></i>
                ${i18n('tools.mcpCount', { n: mcpCount })}
                <a href="#/tools/mcp">${i18n('tools.mcpManage')}</a></p>` : ''}`;

        this.renderSections('');
        const filter = document.getElementById('tools-filter');
        filter.oninput = () => this.renderSections(filter.value);
    },

    /** Which section a tool belongs to. What you can DO with a tool is what
     *  separates the sections, because that is the question this page answers:
     *  system tools run inside the server and are untouchable, built-in ones
     *  can be edited (your copy shadows the shipped version), personal ones
     *  are yours to change or delete. */
    sectionOf(t) {
        if (t.internal) return 'system';
        return t.origin === 'native' ? 'builtin' : 'custom';
    },

    /** One row. `cols` declares which optional columns this section renders,
     *  so each table's header and cells can't drift apart.
     *
     *  Only the exceptional state is drawn: a "modified" badge appears next to
     *  the name of a tool you edited, while a pristine one says nothing — a
     *  badge repeated on every row would be noise, not information. */
    rowFor(t, cols) {
        const id = App.esc(t.id);
        const paramCount = t.parameters?.properties
            ? Object.keys(t.parameters.properties).length : '—';
        const btn = (attr, style, icon, labelKey) =>
            `<button type="button" class="btn btn-sm btn-outline-${style}" ${attr}="${id}"
                     title="${App.escAttr(i18n(labelKey))}"><i class="bi bi-${icon}"></i></button>`;
        const actions = [];
        if (cols.edit) {
            actions.push(`<a href="#/tools/${id}" class="btn btn-sm btn-outline-primary"
                             title="${App.escAttr(i18n('common.edit'))}"><i class="bi bi-pencil"></i></a>`);
        }
        // Reset appears whenever a local copy exists — even a byte-identical
        // one (left behind by an older version): removing it puts the tool
        // back on the shipped file, so it follows app upgrades again.
        if (cols.reset && t.has_override) {
            actions.push(btn('data-tool-reset', 'warning', 'arrow-counterclockwise', 'tools.reset'));
        }
        if (cols.delete) actions.push(btn('data-tool-delete', 'danger', 'trash', 'common.delete'));

        return `<tr>
            <td>
                <div>${App.esc(t.name)} <code class="small text-secondary">${id}</code>
                    ${t.language ? `<span class="badge fw-normal bg-secondary-subtle text-secondary-emphasis"
                         title="${App.escAttr(i18n('tools.languageHint'))}">${App.esc(t.language)}</span>` : ''}
                    ${t.modified ? `<span class="badge bg-warning text-dark">${i18n('tools.statusModified')}</span>` : ''}
                </div>
                ${t.description ? `<div class="small text-secondary tool-desc"
                     title="${App.escAttr(t.description)}">${App.esc(t.description)}</div>` : ''}
            </td>
            <td class="text-center">${paramCount}</td>
            ${cols.edit || cols.delete
                ? `<td class="text-end"><div class="d-inline-flex gap-1">${actions.join('')}</div></td>` : ''}
        </tr>`;
    },

    /** A section's table. Rows arrive sorted ungrouped-first then by category,
     *  so a subheader is emitted whenever the category changes — it also shows
     *  the `<group>/*` wildcard that grants the whole group to an agent. */
    table(rows, cols) {
        const span = 2 + (cols.edit || cols.delete ? 1 : 0);
        let lastCat = null;
        const body = rows.map(t => {
            const cat = t.category || '';
            const head = cat !== lastCat && cat
                ? `<tr class="table-active"><td colspan="${span}">
                       <i class="bi bi-folder2-open"></i> <strong>${App.esc(cat)}</strong>
                       <code class="small text-secondary ms-2">${App.esc(cat)}/*</code>
                       <span class="small text-secondary">${i18n('tools.groupGrantHint')}</span>
                   </td></tr>`
                : '';
            lastCat = cat;
            return head + this.rowFor(t, cols);
        }).join('');

        // Fixed widths on the trailing columns: the three sections are separate
        // tables, and without them each would size its columns differently and
        // the page would look ragged.
        return `<div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
                <thead>
                    <tr>
                        <th>${i18n('tools.colTool')}</th>
                        <th class="text-center" style="width:6rem">${i18n('tools.colParameters')}</th>
                        ${cols.edit || cols.delete ? '<th style="width:7rem"></th>' : ''}
                    </tr>
                </thead>
                <tbody>${body}</tbody>
            </table>
        </div>`;
    },

    /** One card per section: title + one line saying what the user can do with
     *  these tools, then the table. `collapsible` renders a <details> (system
     *  tools are reference material, folded away unless searched for). */
    section(s) {
        if (!s.rows.length && !s.emptyText) return '';
        const head = `<i class="bi bi-${s.icon}"></i> <strong>${s.title}</strong>
            <span class="badge bg-secondary ms-1">${s.rows.length}</span>`;
        // When empty, the empty state already explains the section: showing the
        // hint too would say the same thing twice.
        const body = s.rows.length
            ? `<div class="card-body py-2 text-secondary small border-bottom">${s.hint}</div>
               ${this.table(s.rows, s.cols)}`
            : `<div class="card-body text-secondary small">${s.emptyText}</div>`;

        return s.collapsible
            ? `<details class="card mb-3 tool-section" ${s.open ? 'open' : ''}>
                   <summary class="card-header">${head}</summary>${body}
               </details>`
            : `<div class="card mb-3">
                   <div class="card-header">${head}</div>${body}
               </div>`;
    },

    renderSections(query) {
        const box = document.getElementById('tools-sections');
        if (!box) return;
        const q = (query || '').trim().toLowerCase();
        const buckets = { custom: [], builtin: [], system: [] };
        (this._rows || [])
            .filter(t => !q || [t.id, t.name, t.description, t.category, t.language]
                .join(' ').toLowerCase().includes(q))
            .forEach(t => buckets[this.sectionOf(t)].push(t));

        if (!buckets.custom.length && !buckets.builtin.length && !buckets.system.length) {
            box.innerHTML = `<p class="text-secondary text-center py-4">${i18n('tools.noMatch')}</p>`;
            return;
        }

        box.innerHTML =
            // Yours first: it is the part of the page you act on. Kept visible
            // when empty (unless filtering) — its empty state is where the
            // "edit a built-in tool and your copy lands here" model is stated.
            this.section({
                icon: 'person-gear', title: i18n('tools.sectionCustom'),
                hint: i18n('tools.sectionCustomHint'), rows: buckets.custom,
                cols: { edit: true, delete: true },
                emptyText: q ? null : i18n('tools.sectionCustomEmpty'),
            })
            + this.section({
                icon: 'box-seam', title: i18n('tools.sectionBuiltin'),
                hint: i18n('tools.sectionBuiltinHint'), rows: buckets.builtin,
                cols: { edit: true, reset: true },
            })
            + this.section({
                icon: 'cpu', title: i18n('tools.sectionSystem'),
                hint: i18n('tools.sectionSystemHint'), rows: buckets.system,
                cols: {}, collapsible: true, open: !!q,
            });

        box.querySelectorAll('[data-tool-reset]').forEach(btn => {
            btn.onclick = () => this.resetTool(btn.dataset.toolReset);
        });
        box.querySelectorAll('[data-tool-delete]').forEach(btn => {
            btn.onclick = () => this.deleteTool(btn.dataset.toolDelete);
        });
    },

    /** Confirm + delete. `after` lets the form navigate away instead of
     *  re-rendering the list (the form handlers used to re-implement all of
     *  this inline, and the two copies had started to drift). */
    async deleteTool(id, after) {
        if (!confirm(i18n('tools.confirmDelete'))) return;
        try {
            await App.api('DELETE', `/tools/${id}`);
            App.toast(i18n('tools.deleted'));
            (after || (() => this.renderList()))();
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    /** Discard the local copy of a native tool: the shipped original shows
     *  through again (no re-import — it was never uninstalled). */
    async resetTool(id, after) {
        if (!confirm(i18n('tools.confirmReset'))) return;
        try {
            await App.api('POST', `/tools/${id}/reset`);
            App.toast(i18n('tools.resetDone'));
            (after || (() => this.renderList()))();
        } catch (err) {
            App.toast(err.message, 'danger');
        }
    },

    async renderForm(toolId) {
        let tool = { id: '', name: '', description: '', parameters: {type:'object',properties:{},required:[]}, enabled: true };
        let isEdit = false;
        let script = '#!/bin/bash\n# Read JSON from stdin\nINPUT=$(cat)\necho "Hello from tool"\n';

        // Existing group names, offered as suggestions for the category field —
        // which only exists when CREATING (edit mode renders no datalist, so
        // fetching the whole catalog there was download-and-discard).
        let categories = [];
        if (!toolId) {
            try {
                const all = await App.api('GET', '/tools');
                categories = [...new Set(all.map(t => t.category).filter(Boolean))].sort();
            } catch (e) { /* empty */ }
        }

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
                    ${isEdit && tool.origin === 'native' ? `
                        <div class="alert ${tool.modified ? 'alert-warning' : 'alert-secondary'} py-2">
                            <i class="bi bi-info-circle"></i>
                            ${i18n(tool.modified ? 'tools.nativeModifiedNotice' : 'tools.nativeNotice')}
                        </div>` : ''}
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
                            <label class="form-label">${i18n('tools.categoryLabel')} <small class="text-secondary">${i18n('tools.categoryHint')}</small></label>
                            <input type="text" class="form-control" id="f-category" value="${App.escAttr(tool.category || '')}"
                                   ${isEdit ? 'readonly' : 'list="category-list" pattern="[A-Za-z0-9][A-Za-z0-9._-]*"'}>
                            ${isEdit ? '' : `<datalist id="category-list">${categories.map(c => `<option value="${App.escAttr(c)}">`).join('')}</datalist>`}
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
                            ${isEdit && tool.has_override ? `<button type="button" class="btn btn-outline-warning ms-auto" id="btn-reset">
                                <i class="bi bi-arrow-counterclockwise"></i> ${i18n('tools.reset')}</button>` : ''}
                            ${isEdit && tool.origin !== 'native' ? `<button type="button" class="btn btn-danger ${tool.has_override ? '' : 'ms-auto'}" id="btn-delete">${i18n('common.delete')}</button>` : ''}
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
            // The group is a folder location, fixed at creation (PUT edits in place).
            if (!isEdit) data.category = document.getElementById('f-category').value.trim() || null;
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

        const backToList = () => { location.hash = '#/tools'; };
        const delBtn = document.getElementById('btn-delete');
        if (delBtn) {
            delBtn.onclick = () => this.deleteTool(toolId, backToList);
        }
        // Native tool with local changes: drop the copy and go back to the
        // list, which then shows the shipped version.
        const resetBtn = document.getElementById('btn-reset');
        if (resetBtn) {
            resetBtn.onclick = () => this.resetTool(toolId, backToList);
        }
    },
};
