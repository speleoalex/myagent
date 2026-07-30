/* Scheduled tasks: the list of everything the agents will do unattended.
 *
 * A task is an agent + a prompt + a schedule (a cron expression, or a one-off
 * ISO timestamp). The form never asks the user to write cron: PRESETS builds
 * the expression from a friendly control, and describe()/detect() read one back
 * so an existing task reopens in the preset it was created with. Anything that
 * doesn't match a preset (hand-written, or written by an agent) falls back to
 * the raw cron field instead of being silently rewritten.
 *
 * The next-runs preview comes from GET /tasks/preview: the server owns the one
 * cron implementation, so the preview can never disagree with the scheduler.
 */
const TasksPage = {
    // Preset id -> how to build a cron expression and how to recognize one.
    // `detect` returns the control values when the expression is that preset.
    PRESETS: {
        minutes: {
            build: (v) => `*/${v.n} * * * *`,
            re: /^\*\/(\d+) \* \* \* \*$/,
            read: (m) => ({ n: +m[1] }),
        },
        hours: {
            build: (v) => `${v.m} */${v.n} * * *`,
            re: /^(\d+) \*\/(\d+) \* \* \*$/,
            read: (m) => ({ m: +m[1], n: +m[2] }),
        },
        daily: {
            build: (v) => `${v.mm} ${v.hh} * * *`,
            re: /^(\d+) (\d+) \* \* \*$/,
            read: (m) => ({ mm: +m[1], hh: +m[2] }),
        },
        weekly: {
            build: (v) => `${v.mm} ${v.hh} * * ${v.days.join(',')}`,
            re: /^(\d+) (\d+) \* \* ([0-6](?:,[0-6])*)$/,
            read: (m) => ({ mm: +m[1], hh: +m[2], days: m[3].split(',').map(Number) }),
        },
    },

    /** Control values when `cron` is this preset, null otherwise. */
    match(kind, cron) {
        const p = this.PRESETS[kind];
        const m = (cron || '').match(p.re);
        return m ? p.read(m) : null;
    },

    async render(params) {
        if (params[0] === 'new') return this.renderForm(null, params[1] || '');
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    // ---------------------------------------------------------------- helpers

    /** A schedule in words. The list must be readable without decoding cron. */
    describe(task) {
        const cron = task.cron || '';
        if (!cron) return i18n('tasks.once');
        let v;
        if ((v = this.match('minutes', cron))) return i18n('tasks.everyNMin', { n: v.n });
        if ((v = this.match('hours', cron))) return i18n('tasks.everyNHours', { n: v.n });
        if ((v = this.match('daily', cron))) return i18n('tasks.daily', { time: this.hhmm(v.hh, v.mm) });
        if ((v = this.match('weekly', cron))) {
            return i18n('tasks.weekly', {
                days: v.days.map(d => i18n(`tasks.day.${d}`)).join(', '),
                time: this.hhmm(v.hh, v.mm),
            });
        }
        return `cron ${cron}`;
    },

    hhmm(h, m) { return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`; },

    /** "today 14:30" / "Mon 4 Aug 09:00" — an ISO string is not a date a human
     *  reads at a glance, and the whole point of the page is when things run. */
    when(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (isNaN(d)) return iso;
        const loc = I18n.getDateLocale();
        const today = new Date();
        const sameDay = d.toDateString() === today.toDateString();
        const time = d.toLocaleTimeString(loc, { hour: '2-digit', minute: '2-digit' });
        if (sameDay) return `${i18n('tasks.today')} ${time}`;
        return `${d.toLocaleDateString(loc, { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
    },

    resultBadge(task) {
        if (!task.last_result) return '';
        const cls = { acted: 'success', noop: 'secondary', error: 'danger',
                      timeout: 'danger', stopped: 'warning' }[task.last_result] || 'secondary';
        const title = `${this.when(task.last_run)}${task.last_reply ? ' — ' + task.last_reply : ''}`;
        return `<span class="badge text-bg-${cls}" title="${App.escAttr(title)}">${App.esc(task.last_result)}</span>`;
    },

    // ------------------------------------------------------------------- list

    async renderList() {
        let tasks = [], agents = [];
        try {
            [tasks, agents] = await Promise.all([
                App.api('GET', '/tasks'),
                App.api('GET', '/agents').catch(() => []),
            ]);
        } catch (e) { /* empty */ }
        const byId = {};
        agents.forEach(a => { byId[a.id] = a; });

        const row = (t) => {
            const agent = byId[t.agent_id];
            // An agent that isn't live can hold tasks; nothing will run them.
            const asleep = agent && !(agent.live && agent.enabled !== false);
            return `
            <tr class="${t.enabled ? '' : 'opacity-50'}">
                <td>
                    <a href="#/agents/${App.escAttr(t.agent_id)}">${App.esc(agent ? agent.name : t.agent_id)}</a>
                    ${asleep ? `<i class="bi bi-pause-circle text-warning ms-1" title="${App.escAttr(i18n('tasks.agentNotLive'))}"></i>` : ''}
                </td>
                <td class="small">${App.esc(t.prompt)}</td>
                <td class="text-nowrap small">${App.esc(this.describe(t))}</td>
                <td class="text-nowrap small">${t.enabled ? App.esc(this.when(t.next_at)) : `<span class="text-secondary">${i18n('tasks.disabled')}</span>`}</td>
                <td>${this.resultBadge(t)}</td>
                <td class="text-nowrap">
                    <button class="btn btn-sm btn-outline-secondary" data-run="${App.escAttr(t.id)}" title="${App.escAttr(i18n('tasks.runNow'))}"><i class="bi bi-play"></i></button>
                    <a href="#/tasks/${App.escAttr(t.id)}" class="btn btn-sm btn-outline-primary">${i18n('common.edit')}</a>
                    <button class="btn btn-sm btn-outline-danger" data-del="${App.escAttr(t.id)}"><i class="bi bi-trash"></i></button>
                </td>
            </tr>`;
        };

        App.container.innerHTML = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-alarm"></i> ${i18n('tasks.title')}</h3>
                <a href="#/tasks/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('tasks.add')}</a>
            </div>
            <p class="text-secondary small">${i18n('tasks.intro')}</p>
            <div class="table-responsive">
                <table class="table table-hover align-middle">
                    <thead><tr>
                        <th>${i18n('tasks.colAgent')}</th>
                        <th>${i18n('tasks.colPrompt')}</th>
                        <th>${i18n('tasks.colSchedule')}</th>
                        <th>${i18n('tasks.colNext')}</th>
                        <th>${i18n('tasks.colLast')}</th>
                        <th></th>
                    </tr></thead>
                    <tbody>
                        ${tasks.map(row).join('')}
                        ${tasks.length === 0 ? `<tr><td colspan="6" class="text-secondary">${i18n('tasks.empty')}</td></tr>` : ''}
                    </tbody>
                </table>
            </div>`;

        App.container.querySelectorAll('[data-del]').forEach(b => {
            b.onclick = () => this.remove(b.dataset.del, () => this.renderList());
        });
        App.container.querySelectorAll('[data-run]').forEach(b => {
            b.onclick = () => this.runNow(b.dataset.run);
        });
    },

    async remove(id, after) {
        if (!confirm(i18n('tasks.confirmDelete'))) return;
        try {
            await App.api('DELETE', `/tasks/${id}`);
            App.toast(i18n('tasks.deleted'));
            if (after) after();
        } catch (e) { App.toast(e.message, 'danger'); }
    },

    async runNow(id) {
        try {
            const res = await App.api('POST', `/tasks/${id}/run`);
            App.toast(res.live ? i18n('tasks.runQueued') : i18n('tasks.runButNotLive'),
                      res.live ? 'success' : 'warning');
            this.renderList();
        } catch (e) { App.toast(e.message, 'danger'); }
    },

    // ------------------------------------------------------------------- form

    async renderForm(taskId, presetAgent = '') {
        const isEdit = !!taskId;
        let task = { id: '', agent_id: presetAgent, prompt: '', cron: '', at: '',
                     enabled: true, source: 'user' };
        let agents = [];
        try {
            agents = await App.api('GET', '/agents');
        } catch (e) { /* empty */ }
        if (isEdit) {
            try { task = await App.api('GET', `/tasks/${taskId}`); }
            catch (e) { App.toast(e.message, 'danger'); location.hash = '#/tasks'; return; }
        }

        const mode = this.detectMode(task);
        const days = mode.kind === 'weekly' ? mode.values.days : [1];
        const dayBoxes = [1, 2, 3, 4, 5, 6, 0].map(d => `
            <input type="checkbox" class="btn-check" id="day-${d}" value="${d}" ${days.includes(d) ? 'checked' : ''}>
            <label class="btn btn-sm btn-outline-secondary" for="day-${d}">${i18n(`tasks.day.${d}`)}</label>`).join('');

        const opt = (v, label, sel) => `<option value="${v}" ${sel === v ? 'selected' : ''}>${label}</option>`;

        App.container.innerHTML = `
        <div class="row"><div class="col-lg-8 mx-auto">
            <h3 class="mb-3"><i class="bi bi-alarm"></i> ${isEdit ? i18n('tasks.editTitle') : i18n('tasks.newTitle')}</h3>
            <form id="task-form" novalidate>
                <div class="mb-3">
                    <label class="form-label" for="f-agent">${i18n('tasks.fieldAgent')}</label>
                    <select class="form-select" id="f-agent" required>
                        <option value="">${i18n('tasks.pickAgent')}</option>
                        ${agents.map(a => opt(App.escAttr(a.id), App.esc(a.name), task.agent_id)).join('')}
                    </select>
                    <div class="form-text" id="agent-live-note"></div>
                </div>
                <div class="mb-3">
                    <label class="form-label" for="f-prompt">${i18n('tasks.fieldPrompt')}</label>
                    <textarea class="form-control" id="f-prompt" rows="3" required
                              placeholder="${App.escAttr(i18n('tasks.promptPlaceholder'))}">${App.esc(task.prompt)}</textarea>
                    <div class="form-text">${i18n('tasks.promptHelp')}</div>
                </div>

                <div class="card mb-3"><div class="card-body">
                    <label class="form-label" for="f-mode">${i18n('tasks.fieldWhen')}</label>
                    <select class="form-select mb-3" id="f-mode">
                        ${opt('once', i18n('tasks.modeOnce'), mode.kind)}
                        ${opt('minutes', i18n('tasks.modeMinutes'), mode.kind)}
                        ${opt('hours', i18n('tasks.modeHours'), mode.kind)}
                        ${opt('daily', i18n('tasks.modeDaily'), mode.kind)}
                        ${opt('weekly', i18n('tasks.modeWeekly'), mode.kind)}
                        ${opt('cron', i18n('tasks.modeCron'), mode.kind)}
                    </select>

                    <div class="mode-pane" data-mode="once">
                        <input type="datetime-local" class="form-control" id="f-at"
                               value="${App.escAttr((task.at || '').slice(0, 16))}">
                        <div class="form-text">${i18n('tasks.onceHelp')}</div>
                    </div>
                    <div class="mode-pane" data-mode="minutes">
                        <div class="input-group">
                            <span class="input-group-text">${i18n('tasks.every')}</span>
                            <input type="number" class="form-control" id="f-min-n" min="1" max="59"
                                   value="${mode.kind === 'minutes' ? mode.values.n : 20}">
                            <span class="input-group-text">${i18n('tasks.minutes')}</span>
                        </div>
                    </div>
                    <div class="mode-pane" data-mode="hours">
                        <div class="input-group">
                            <span class="input-group-text">${i18n('tasks.every')}</span>
                            <input type="number" class="form-control" id="f-hr-n" min="1" max="23"
                                   value="${mode.kind === 'hours' ? mode.values.n : 6}">
                            <span class="input-group-text">${i18n('tasks.hoursAtMin')}</span>
                            <input type="number" class="form-control" id="f-hr-m" min="0" max="59"
                                   value="${mode.kind === 'hours' ? mode.values.m : 0}">
                        </div>
                    </div>
                    <div class="mode-pane" data-mode="daily">
                        <input type="time" class="form-control" id="f-daily-time"
                               value="${mode.kind === 'daily' ? this.hhmm(mode.values.hh, mode.values.mm) : '09:00'}">
                    </div>
                    <div class="mode-pane" data-mode="weekly">
                        <div class="btn-group flex-wrap mb-2" role="group">${dayBoxes}</div>
                        <input type="time" class="form-control" id="f-weekly-time"
                               value="${mode.kind === 'weekly' ? this.hhmm(mode.values.hh, mode.values.mm) : '09:00'}">
                    </div>
                    <div class="mode-pane" data-mode="cron">
                        <input type="text" class="form-control font-monospace" id="f-cron"
                               value="${App.escAttr(task.cron || '')}" placeholder="*/20 * * * *">
                        <div class="form-text">${i18n('tasks.cronHelp')}</div>
                    </div>

                    <div class="mt-3 small" id="preview"></div>
                </div></div>

                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" id="f-enabled" ${task.enabled ? 'checked' : ''}>
                    <label class="form-check-label" for="f-enabled">${i18n('tasks.fieldEnabled')}</label>
                </div>

                <div class="d-flex gap-2">
                    <button type="submit" class="btn btn-primary">${isEdit ? i18n('common.save') : i18n('common.create')}</button>
                    <a href="#/tasks" class="btn btn-secondary">${i18n('common.cancel')}</a>
                    ${isEdit ? `<button type="button" class="btn btn-outline-danger ms-auto" id="btn-del">${i18n('common.delete')}</button>` : ''}
                </div>
            </form>
        </div></div>`;

        const showPanes = () => {
            const kind = document.getElementById('f-mode').value;
            App.container.querySelectorAll('.mode-pane').forEach(p => {
                p.style.display = p.dataset.mode === kind ? '' : 'none';
            });
            this.refreshPreview();
        };
        document.getElementById('f-mode').onchange = showPanes;
        App.container.querySelectorAll('.mode-pane input').forEach(el => {
            el.oninput = () => this.refreshPreview();
            el.onchange = () => this.refreshPreview();
        });
        const noteLive = () => {
            const a = agents.find(x => x.id === document.getElementById('f-agent').value);
            document.getElementById('agent-live-note').innerHTML =
                a && !(a.live && a.enabled !== false)
                    ? `<span class="text-warning"><i class="bi bi-exclamation-triangle"></i> ${i18n('tasks.agentNotLive')}</span>`
                    : '';
        };
        document.getElementById('f-agent').onchange = noteLive;
        noteLive();
        showPanes();

        if (isEdit) {
            document.getElementById('btn-del').onclick =
                () => this.remove(taskId, () => { location.hash = '#/tasks'; });
        }
        document.getElementById('task-form').onsubmit = async (e) => {
            e.preventDefault();
            const form = e.target;
            if (!form.checkValidity()) return form.reportValidity();
            const schedule = this.readSchedule();
            if (schedule.error) return App.toast(schedule.error, 'danger');
            const data = {
                id: taskId || `task-${Date.now().toString(36)}`,
                agent_id: document.getElementById('f-agent').value,
                prompt: document.getElementById('f-prompt').value.trim(),
                enabled: document.getElementById('f-enabled').checked,
                source: task.source || 'user',
                ...schedule,
            };
            try {
                if (isEdit) await App.api('PUT', `/tasks/${taskId}`, data);
                else await App.api('POST', '/tasks', data);
                App.toast(i18n('tasks.saved'));
                location.hash = '#/tasks';
            } catch (err) { App.toast(err.message, 'danger'); }
        };
    },

    /** Which preset an existing task was made with. Unrecognized expressions
     *  open in the raw cron field: never rewrite what we cannot round-trip. */
    detectMode(task) {
        if (!task.cron) return { kind: 'once', values: {} };
        for (const kind of ['minutes', 'hours', 'daily', 'weekly']) {
            const values = this.match(kind, task.cron);
            if (values) return { kind, values };
        }
        return { kind: 'cron', values: {} };
    },

    /** The form's schedule as {cron, at}. Returns {error} when unusable. */
    readSchedule() {
        const val = (id) => document.getElementById(id).value;
        const kind = val('f-mode');
        const split = (id) => val(id).split(':').map(Number);
        if (kind === 'once') {
            return { at: val('f-at'), cron: '' };
        }
        if (kind === 'cron') {
            const cron = val('f-cron').trim();
            if (!cron) return { error: i18n('tasks.errNoCron') };
            return { cron, at: '' };
        }
        if (kind === 'minutes') {
            return { cron: this.PRESETS.minutes.build({ n: +val('f-min-n') || 20 }), at: '' };
        }
        if (kind === 'hours') {
            return { cron: this.PRESETS.hours.build({ n: +val('f-hr-n') || 6, m: +val('f-hr-m') || 0 }), at: '' };
        }
        if (kind === 'daily') {
            const [hh, mm] = split('f-daily-time');
            return { cron: this.PRESETS.daily.build({ hh, mm }), at: '' };
        }
        const days = [...App.container.querySelectorAll('.btn-check:checked')].map(c => +c.value);
        if (!days.length) return { error: i18n('tasks.errNoDays') };
        const [hh, mm] = split('f-weekly-time');
        return { cron: this.PRESETS.weekly.build({ hh, mm, days: days.sort() }), at: '' };
    },

    /** Next runs, straight from the server's own cron implementation. */
    async refreshPreview() {
        const box = document.getElementById('preview');
        if (!box) return;
        const schedule = this.readSchedule();
        if (schedule.error) return void (box.innerHTML = `<span class="text-danger">${App.esc(schedule.error)}</span>`);
        if (!schedule.cron) {
            box.innerHTML = schedule.at
                ? `${i18n('tasks.willRun')} <strong>${App.esc(this.when(schedule.at))}</strong>`
                : `<span class="text-secondary">${i18n('tasks.willRunNow')}</span>`;
            return;
        }
        try {
            const res = await App.api('GET', `/tasks/preview?cron=${encodeURIComponent(schedule.cron)}`);
            box.innerHTML = `<code>${App.esc(schedule.cron)}</code> — ${i18n('tasks.nextRuns')}: `
                + res.next.map(d => `<strong>${App.esc(this.when(d))}</strong>`).join(', ');
        } catch (e) {
            box.innerHTML = `<span class="text-danger">${App.esc(e.message)}</span>`;
        }
    },
};
