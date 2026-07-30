const AgentsPage = {
    async render(params) {
        if (params[0] === 'new') return this.renderForm();
        if (params[0]) return this.renderForm(params[0]);
        return this.renderList();
    },

    async renderList() {
        // Independent GETs: fetch them together, not one latency after the
        // other. The tool catalog is NOT fetched here — the preview modal
        // lazy-loads it on first open (ensurePreviewData).
        const [agents, native, autoStatus] = await Promise.all([
            App.api('GET', '/agents').catch(() => []),
            App.api('GET', '/agents/native').catch(() => []),   // no catalog
            App.api('GET', '/autonomy/status').catch(() => ({})),
        ]);
        // Cache agents so the preview tree can resolve delegation without refetching.
        this._allAgents = agents;

        // One grid, one place to act. The bundled catalog is not a separate
        // table: an agent shipped with the app that isn't installed (deleted,
        // or new in this app version) keeps its card — dimmed, with an Import
        // button — and one you edited offers "reset to original" in its footer.
        const nativeById = {};
        native.forEach(n => { nativeById[n.id] = n; });
        const cards = agents
            .map(a => this.agentCard(a, nativeById[a.id], autoStatus[a.id]))
            .concat(native.filter(n => !n.installed).map(n => this.missingCard(n)));

        App.container.innerHTML = `
            <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
                <h3 class="mb-0"><i class="bi bi-cpu"></i> ${i18n('agents.title')}</h3>
                <a href="#/agents/new" class="btn btn-primary"><i class="bi bi-plus-lg"></i> ${i18n('agents.new')}</a>
            </div>
            <div class="row g-3">
                ${cards.length === 0 ? `<div class="col-12 text-secondary">${i18n('agents.empty')}</div>` : ''}
                ${cards.join('')}
            </div>
            ${this.previewModalMarkup()}`;

        this.wireNative();
        App.container.querySelectorAll('[data-preview]').forEach(btn => {
            btn.onclick = (e) => { e.stopPropagation(); this.openPreviewById(btn.dataset.preview); };
        });
        // Start/stop = flip the persisted `live` flag: the scheduler notices
        // within one scan, and a started agent survives server restarts.
        App.container.querySelectorAll('[data-live-toggle]').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const agent = agents.find(x => x.id === btn.dataset.liveToggle);
                if (!agent) return;
                try {
                    await App.api('PUT', `/agents/${agent.id}`, { ...agent, live: !agent.live });
                    App.toast(i18n(agent.live ? 'agents.liveStopped' : 'agents.liveStarted'));
                    this.renderList();
                } catch (err) {
                    App.toast(err.message, 'danger');
                }
            };
        });
    },

    /** Translated label of an autonomy state (the API's raw enum values were
     *  reaching the screen untranslated). */
    stateLabel(state) {
        const keys = { running: 'agents.stateRunning', idle: 'agents.stateIdle',
                       paused: 'agents.statePaused', error: 'agents.stateError',
                       rate_limited: 'agents.stateRateLimited',
                       disabled: 'agents.stateDisabled' };
        return keys[state] ? i18n(keys[state]) : state;
    },

    /** The "last wake / next wake / pending" parts, shared by the card badge
     *  tooltip and the form's wake-status line (the two copies had already
     *  diverged on the '—' fallback). */
    wakeSummary(status) {
        if (!status) return [];
        const bits = [];
        if (status.last_wake) {
            bits.push(`${i18n('agents.lastWake')}: ${status.last_wake} (${status.last_result || '—'})`);
        }
        if (status.next_wake) bits.push(`${i18n('agents.nextWake')}: ${status.next_wake}`);
        if (status.tasks) {
            bits.push(i18n('agents.scheduledTasks', { n: status.tasks }));
        }
        return bits;
    },

    liveBadge(agent, status) {
        if (!agent.live) return '';
        const state = (status && status.state) || 'idle';
        const color = { running: 'bg-primary', idle: 'bg-success', paused: 'bg-danger',
                        error: 'bg-danger', rate_limited: 'bg-warning text-dark' }[state] || 'bg-secondary';
        const tip = this.wakeSummary(status).join(' · ');
        const label = i18n('agents.liveBadge');
        return `<span class="badge ${color}" title="${App.escAttr(tip)}">` +
               `<i class="bi bi-broadcast"></i> ${label}${state !== 'idle' ? ': ' + this.stateLabel(state) : ''}</span>`;
    },

    /** An installed agent. `nat` is its entry in the bundled catalog when it has
     *  one: only the exceptional state is drawn, so "modified" (and the reset
     *  that undoes it) appears for an edited native agent and a pristine one
     *  says nothing. */
    agentCard(a, nat, status) {
        const id = App.escAttr(a.id);
        const resetBtn = nat && nat.modified
            ? `<button type="button" class="btn btn-sm btn-outline-warning" data-native-reset="${id}"
                       title="${App.escAttr(i18n('agents.reset'))}">
                   <i class="bi bi-arrow-counterclockwise"></i>
               </button>`
            : '';
        return `
            <div class="col-md-6 col-lg-4 col-xl-3">
                <div class="card card-agent h-100" onclick="location.hash='#/agents/${id}'">
                    <div class="card-body">
                        <h5 class="card-title">${App.esc(a.name)}</h5>
                        <p class="card-text text-secondary small card-desc"
                           title="${App.escAttr(a.description || '')}">${App.esc(a.description || '')}</p>
                        <div class="small">
                            <span class="badge bg-secondary">${App.esc(a.model_id)}</span>
                            <span class="badge bg-info">${i18n('agents.toolsBadge', { count: (a.tools || []).length })}</span>
                            ${nat && nat.modified ? `<span class="badge bg-warning text-dark">${i18n('agents.statusModified')}</span>` : ''}
                            ${this.liveBadge(a, status)}
                        </div>
                    </div>
                    <div class="card-footer d-flex flex-wrap gap-2 align-items-center">
                        <a href="#/chat/${id}" class="btn btn-sm btn-outline-primary" onclick="event.stopPropagation()">
                            <i class="bi bi-chat-dots"></i> ${i18n('agents.chat')}
                        </a>
                        <button type="button" class="btn btn-sm btn-outline-info" data-preview="${id}">
                            <i class="bi bi-diagram-3"></i> ${i18n('agents.preview')}
                        </button>
                        <div class="ms-auto d-flex gap-2">
                            ${resetBtn}
                            <button type="button" class="btn btn-sm ${a.live ? 'btn-danger' : 'btn-outline-success'}"
                                    data-live-toggle="${id}" title="${i18n(a.live ? 'agents.liveStop' : 'agents.liveStart')}">
                                <i class="bi ${a.live ? 'bi-stop-fill' : 'bi-play-fill'}"></i>
                            </button>
                        </div>
                    </div>
                </div>
            </div>`;
    },

    /** A native agent that is missing from the workspace: same card, dimmed and
     *  not clickable (there is nothing to open yet), with the one action that
     *  applies — bring it back. */
    missingCard(n) {
        const id = App.escAttr(n.id);
        return `
            <div class="col-md-6 col-lg-4 col-xl-3">
                <div class="card card-ghost h-100">
                    <div class="card-body">
                        <h5 class="card-title">${App.esc(n.name)}</h5>
                        <p class="card-text text-secondary small card-desc"
                           title="${App.escAttr(n.description || '')}">${App.esc(n.description || '')}</p>
                        <div class="small">
                            ${n.model_id ? `<span class="badge bg-secondary">${App.esc(n.model_id)}</span>` : ''}
                            <span class="badge bg-secondary-subtle text-secondary-emphasis"
                                  title="${App.escAttr(i18n('agents.missingHint'))}">${i18n('agents.statusNotInstalled')}</span>
                        </div>
                    </div>
                    <div class="card-footer">
                        <button type="button" class="btn btn-sm btn-primary" data-native-import="${id}">
                            <i class="bi bi-download"></i> ${i18n('agents.import')}
                        </button>
                    </div>
                </div>
            </div>`;
    },

    // Both actions live on a card, and an agent card navigates on click:
    // stop the event or importing/resetting would also open the editor.
    wireNative() {
        App.container.querySelectorAll('[data-native-import]').forEach(btn => {
            btn.onclick = (e) => { e.stopPropagation(); this.importNative(btn.dataset.nativeImport, false); };
        });
        App.container.querySelectorAll('[data-native-reset]').forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
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

    /** Tool grants the form manages on the user's behalf, and therefore does NOT
     *  offer in the picker.
     *
     *  `hideIds`/`hideCategory` say what leaves the picker; `grant` says what
     *  readForm() puts back. Both live in the same entry on purpose: a grant can
     *  never be hidden without also being derived, or the next save would
     *  silently revoke it. `state` is the partial object readForm() has already
     *  built, so the derivation reads the values that are about to be saved.
     *
     *  The rule for deciding whether a group belongs here or in RELOCATED_TOOLS:
     *  derive a group wildcard when the group is ONE user-visible concept behind
     *  ONE switch (memory — memory_tools._gate() refuses all three on the same
     *  flag, so offering them separately only ever produced a broken agent).
     *  Relocate it into explicit grants when the members are independently
     *  meaningful (autonomy — reach the user / schedule for self / flip own
     *  live: three capabilities with three different dependencies). */
    DERIVED_TOOLS: [
        {
            key: 'memory',
            hideCategory: 'memory',
            // The wildcard rather than the three ids: it is the bundled seed's
            // own idiom (server/config/agents/master.json) and it does not
            // hardcode a backend folder listing in the frontend. Both grant
            // shapes resolve to identical definition lists server-side.
            grant: (s) => (s.memory_enabled ? ['memory/*'] : []),
        },
        {
            key: 'delegate',
            // Flat internal tool — no category wildcard covers it. The executor
            // injects the agents directory only when it is granted, and
            // internal.py's call_agent handler is the real gate, so delegation
            // must be granted explicitly and can never be merely implied.
            hideIds: ['call_agent'],
            grant: (s) => (s.delegate ? ['call_agent'] : []),
        },
    ],

    /** Tool grants MOVED out of the picker to a tab where each gets its own
     *  explanation. NOT derived: they stay explicit `.tool-check` checkboxes
     *  with the same value/id shape toolPicker's row() produces, so readForm()
     *  needs no special case. `expand` normalizes a stored group wildcard into
     *  per-tool state on load, because the three tools mean three different
     *  things and collapsing them back would flatten that. */
    RELOCATED_TOOLS: [
        { category: 'autonomy', expand: 'autonomy/*' },
    ],

    /** AutonomousConfig defaults — mirrors server/app/models.py.
     *
     *  ONE table drives three things that used to be three separate copies: the
     *  value each field renders, the fallback readForm() uses when a field is
     *  emptied, and the all-defaults test that decides `autonomous: null` (the
     *  server's own "None = all defaults"). Declared in the model's field order
     *  so the emitted JSON keeps its key order and saving an unchanged agent
     *  produces no diff. `group` is where the field is laid out, not what it
     *  means. */
    // Label/min/step of the numeric ones live in numSpecs() (RangeField), the
    // single authority — stale copies here once looked authoritative and were not.
    AUTO_FIELDS: [
        { key: 'max_wakes_per_hour',     el: 'f-auto-maxwakes',     type: 'int',  dflt: 12,   group: 'grid' },
        { key: 'max_consecutive_errors', el: 'f-auto-maxerrors',    type: 'int',  dflt: 5,    group: 'grid' },
        { key: 'wake_timeout_s',         el: 'f-auto-timeout',      type: 'int',  dflt: 600,  group: 'grid' },
        { key: 'history_messages',       el: 'f-auto-history',      type: 'int',  dflt: 0,    group: 'grid' },
        { key: 'notify_binding_id',      el: 'f-auto-binding',      type: 'text', dflt: '',   group: 'notify' },
        { key: 'notify_chat_id',         el: 'f-auto-chat',         type: 'text', dflt: '',   group: 'notify' },
    ],

    /** Slider specs for the agent's numeric fields, keyed by input id.
     *
     *  A function, not a const: every string goes through i18n(), and switching
     *  language re-renders the page — the same reason ModelsPage.optionSpecs() is
     *  a function.
     *
     *  `steps` turns the slider into an index picker over hand-picked values, the
     *  only way 0 -> 86400 fits under one thumb; fields without it are linear.
     *  `describe(v, ctx)` is the live sentence under the slider and may return
     *  {text, warn} to flag a value that will bite. `deps` lists the other fields
     *  a sentence mentions, so both refresh together. */
    numSpecs() {
        const F = RangeField.fmt;
        const K = 'agents.f.';
        return {
            'f-maxiter': {
                id: 'f-maxiter', int: true, dflt: 10, min: 1, max: 50,
                steps: [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50],
                label: i18n('agents.maxIterations'),
                help: i18n(K + 'maxiter.help'),
                fmt: (v) => F.int(v),
                describe: (v) => v <= 1 ? { text: i18n(K + 'maxiter.b.one'), warn: true }
                    : v <= 3 ? i18n(K + 'maxiter.b.low', { n: F.int(v) })
                    : v <= 10 ? i18n(K + 'maxiter.b.mid', { n: F.int(v) })
                    : i18n(K + 'maxiter.b.high', { n: F.int(v) }),
            },
            'f-maxtools': {
                id: 'f-maxtools', int: true, dflt: 5, min: 1, max: 50,
                steps: [1, 2, 3, 5, 8, 12, 20, 30, 50],
                label: i18n('agents.maxToolCalls'),
                help: i18n(K + 'maxtools.help'),
                fmt: (v) => F.int(v),
                describe: (v) => v <= 1 ? i18n(K + 'maxtools.b.one')
                    : v <= 5 ? i18n(K + 'maxtools.b.low', { n: F.int(v) })
                    : v <= 12 ? i18n(K + 'maxtools.b.mid', { n: F.int(v) })
                    : i18n(K + 'maxtools.b.high', { n: F.int(v) }),
            },
            'f-temp': {
                id: 'f-temp', dflt: 0.7, min: 0, max: 2, step: 0.05,
                label: i18n('agents.temperature'), hint: i18n('agents.temperatureHint'),
                help: i18n(K + 'temp.help'),
                fmt: (v) => F.num(v),
                describe: (v) => v === 0 ? i18n(K + 'temp.b.zero')
                    : v <= 0.3 ? i18n(K + 'temp.b.low', { v: F.num(v) })
                    : v <= 0.8 ? i18n(K + 'temp.b.mid', { v: F.num(v) })
                    : v <= 1.2 ? i18n(K + 'temp.b.high', { v: F.num(v) })
                    : { text: i18n(K + 'temp.b.wild', { v: F.num(v) }), warn: true },
            },
            'f-resp-temp': {
                // null = inherit temperature, then ModelConfig.options.temperature,
                // then the provider default. A slider cannot express that, so the
                // checkbox owns the third state and the slider only exists once the
                // user has opted into a distinct value.
                id: 'f-resp-temp', dflt: 0.4, fallback: 0.4, nullable: true,
                min: 0, max: 2, step: 0.05,
                label: i18n('agents.respTemperature'), hint: i18n('agents.respTemperatureHint'),
                toggle: i18n(K + 'respTemp.toggle'),
                help: i18n(K + 'respTemp.help'),
                deps: ['f-temp', 'f-model'],
                fmt: (v) => F.num(v),
                fmtOff: (c) => i18n(K + 'respTemp.badgeOff', { v: F.num(c.temp) }),
                describeOff: (c) => ({ text: i18n(K + 'respTemp.b.off', { v: F.num(c.temp) }), muted: true }),
                describe: (v, c) => {
                    const band = v <= 0.3 ? i18n(K + 'respTemp.b.low', { v: F.num(v) })
                        : v <= 0.7 ? i18n(K + 'respTemp.b.mid', { v: F.num(v) })
                        : i18n(K + 'respTemp.b.high', { v: F.num(v) });
                    // Where it actually applies depends on the provider: llama.cpp
                    // is forced into text-mode tool calling, so it drives every
                    // reply after the first tool results; a native tool caller only
                    // sees it in the forced final-answer pass.
                    const note = !c.model ? ''
                        : ' ' + i18n(K + (c.model.provider === 'llamacpp' ? 'respTemp.noteText'
                                                                         : 'respTemp.noteNative'),
                                     { model: c.model.name });
                    return band + note;
                },
            },
            'f-memthreshold': {
                id: 'f-memthreshold', int: true, dflt: 4000, min: 200, max: 64000,
                steps: [1000, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000, 48000, 64000],
                label: i18n('agents.memoryThreshold'),
                help: i18n(K + 'memthreshold.help'),
                deps: ['f-memory'],
                mutedWhen: (c) => !c.memoryOn,
                fmt: (v) => F.tokens(v),
                describe: (v, c) => {
                    if (!c.memoryOn) return { text: i18n(K + 'memthreshold.b.memoryOff'), muted: true };
                    const half = F.tokens(Math.max(1, Math.floor(v / 2)));
                    return v <= 2000 ? i18n(K + 'memthreshold.b.low', { v: F.tokens(v), half })
                        : v <= 8000 ? i18n(K + 'memthreshold.b.mid', { v: F.tokens(v), half })
                        : v <= 24000 ? i18n(K + 'memthreshold.b.high', { v: F.tokens(v), half })
                        : { text: i18n(K + 'memthreshold.b.huge', { v: F.tokens(v), half }), warn: true };
                },
            },
            'f-auto-maxwakes': {
                id: 'f-auto-maxwakes', int: true, dflt: 12, min: 1, max: 120,
                steps: [1, 2, 3, 4, 6, 12, 20, 30, 60, 120],
                label: i18n('agents.autoMaxWakes'),
                help: i18n(K + 'maxwakes.help'),
                fmt: (v) => F.perHour(v),
                // The pace itself is the task list's business now; what this
                // knob still decides is the ceiling, i.e. the shortest interval
                // a recurring task can actually achieve.
                describe: (v) => i18n(K + 'maxwakes.b.cap',
                                      { n: F.int(v), r: F.seconds(Math.ceil(3600 / v)) }),
            },
            'f-auto-maxerrors': {
                // 0 is a footgun, not "unlimited" (1 >= 0 pauses on the first
                // failure), so the slider starts at 1. A stored 0 is still shown —
                // spliced in — with a sentence that says what it really does.
                id: 'f-auto-maxerrors', int: true, dflt: 5, min: 1, max: 20,
                steps: [1, 2, 3, 5, 8, 10, 20],
                label: i18n('agents.autoMaxErrors'),
                help: i18n(K + 'maxerrors.help'),
                fmt: (v) => F.int(v),
                describe: (v) => v <= 0 ? { text: i18n(K + 'maxerrors.b.zero'), warn: true }
                    : v === 1 ? i18n(K + 'maxerrors.b.one')
                    : v <= 5 ? i18n(K + 'maxerrors.b.mid', { n: F.int(v) })
                    : i18n(K + 'maxerrors.b.high', { n: F.int(v) }),
            },
            'f-auto-history': {
                // 0 is a real, useful setting here (rely on memory alone), not a
                // footgun — so unlike max-errors the scale starts at it.
                id: 'f-auto-history', int: true, dflt: 0, min: 0, max: 40, sentinel: 0,
                steps: [0, 2, 4, 8, 16, 40],
                label: i18n('agents.autoHistory'),
                help: i18n(K + 'history.help'),
                deps: ['f-memory'],
                minLabel: i18n(K + 'history.endOff'),
                fmt: (v) => (v === 0 ? i18n(K + 'history.endOff') : i18n('field.msgs', { n: F.int(v) })),
                describe: (v, c) => {
                    if (v === 0) {
                        return c.memoryOn
                            ? i18n(K + 'history.b.offMemory')
                            : { text: i18n(K + 'history.b.offNoMemory'), warn: true };
                    }
                    return v <= 4 ? i18n(K + 'history.b.low', { n: F.int(v) })
                        : v <= 16 ? i18n(K + 'history.b.mid', { n: F.int(v) })
                        : { text: i18n(K + 'history.b.high', { n: F.int(v) }), warn: true };
                },
            },
            'f-auto-timeout': {
                id: 'f-auto-timeout', int: true, dflt: 600, min: 30, max: 3600,
                steps: [60, 120, 300, 600, 900, 1800, 3600],
                label: i18n('agents.autoTimeout'),
                help: i18n(K + 'timeout.help'),
                deps: ['f-maxiter', 'f-auto-maxerrors'],
                fmt: (v) => F.seconds(v),
                describe: (v, c) => v < 600
                    ? { text: i18n(K + 'timeout.b.low', { v: F.seconds(v), it: F.int(c.maxIter) }), warn: true }
                    : v <= 1800 ? i18n(K + 'timeout.b.mid', { v: F.seconds(v) })
                    : i18n(K + 'timeout.b.high', { v: F.seconds(v), e: F.int(c.maxErrors) }),
            },
        };
    },

    /** Which tab the form reopens on, remembered per agent id: the real loop is
     *  save -> list -> reopen, so landing where you left off helps. A DIFFERENT
     *  agent (or a new one) always starts on General — landing on Autonomy for
     *  an agent you have never seen is disorienting. Also survives a locale
     *  switch, which re-renders the page through App.route(). */
    _formTab: { id: null, tab: 'general' },

    /** { ids, categories } — everything the picker must not render. Relocated
     *  groups are hidden from the picker too, but they are rendered elsewhere,
     *  so they are NOT derived. */
    hiddenToolSet() {
        const ids = new Set(), categories = new Set();
        this.DERIVED_TOOLS.forEach(d => {
            (d.hideIds || []).forEach(i => ids.add(i));
            if (d.hideCategory) categories.add(d.hideCategory);
        });
        this.RELOCATED_TOOLS.forEach(r => categories.add(r.category));
        return { ids, categories };
    },

    /** Tool grants implied by `state`, in table order. */
    derivedGrants(state) {
        return this.DERIVED_TOOLS.flatMap(d => d.grant(state));
    },

    /** Label / explanation for a relocated tool. I18n.t() returns the KEY when a
     *  string is missing, so a tool dropped into the group later falls back to
     *  its own catalog metadata instead of printing "agents.toolHint.x". */
    toolText(prefix, tool, fallback) {
        const key = `agents.${prefix}.${tool.id}`;
        const text = i18n(key);
        return text === key ? (fallback || '') : text;
    },

    /** The notify target picker.
     *
     *  Bots are configured by the connectors plugin (GET /api/connectors/bindings,
     *  managed at #/connectors). When the list is available this is a real
     *  <select> — the set is closed (you can only send through a configured
     *  binding) and the NAME is what the user knows, not the id.
     *
     *  When it is not available — the plugin isn't installed, or simply not used
     *  by this install — it falls back to a free-text input. A <select> would
     *  make the field impossible to fill in that state, which is worse than typing.
     *  Same reason a stored id missing from the list is kept as its own option
     *  rather than silently reset (as with a deleted model). */
    bindingField(bindings, current) {
        if (!bindings.length) {
            return `<input type="text" class="form-control form-control-sm" id="f-auto-binding"
                           value="${App.escAttr(current)}">`;
        }
        const known = bindings.some(b => b.id === current);
        return `
            <select class="form-select form-select-sm" id="f-auto-binding">
                <option value="" ${!current ? 'selected' : ''}>${i18n('agents.autoBindingNone')}</option>
                ${bindings.map(b => `
                    <option value="${App.escAttr(b.id)}" ${b.id === current ? 'selected' : ''}
                            data-allowed="${App.escAttr((b.allowed_ids || []).join(','))}">
                        ${App.esc(b.name || b.id)} (${App.esc(b.id)})${b.enabled === false ? ' — ' + i18n('mcp.stateDisabled') : ''}
                    </option>`).join('')}
                ${current && !known ? `<option value="${App.escAttr(current)}" selected>${App.esc(current)} — ${i18n('agents.bindingMissing')}</option>` : ''}
            </select>`;
    },

    /** The four numeric autonomy knobs, from AUTO_FIELDS. Two-up from md up: each
     *  half is wide enough for a usable track plus its number box, and phones get
     *  one per row. */
    autoGrid(auto, specs) {
        return this.AUTO_FIELDS.filter(f => f.group === 'grid').map(f => `
            <div class="col-12 col-md-6">
                ${RangeField.render(specs[f.el], auto[f.key] ?? f.dflt)}
            </div>`).join('');
    },

    /** This agent's schedule, read-only, with links to the Tasks page.
     *
     *  Deliberately NOT an editable list: tasks are their own resource with
     *  their own save cycle, and nesting a second CRUD inside this form would
     *  make "Save agent" ambiguous. What it must not do is hide them — Live on
     *  with no task is the one configuration that looks right and does nothing.
     *  Rendering is delegated to TasksPage so the wording matches that page. */
    taskBox(agentId, tasks) {
        if (!agentId) {
            return `<div class="form-text mb-3">${i18n('agents.tasksSaveFirst')}</div>`;
        }
        const rows = (tasks || []).map(t => `
            <div class="d-flex flex-wrap gap-2 align-items-baseline small ${t.enabled ? '' : 'opacity-50'}">
                <a href="#/tasks/${App.escAttr(t.id)}" class="text-decoration-none">${App.esc(t.prompt)}</a>
                <span class="text-secondary">${App.esc(TasksPage.describe(t))}</span>
                <span class="text-secondary ms-auto">${App.esc(t.enabled ? TasksPage.when(t.next_at) : i18n('tasks.disabled'))}</span>
            </div>`).join('');
        return `
            <div class="border rounded p-2 mb-3">
                <label class="form-label small mb-1">${i18n('agents.tasksTitle')}</label>
                ${rows || `<div class="form-text text-warning-emphasis">
                    <i class="bi bi-exclamation-triangle"></i> ${i18n('agents.tasksEmpty')}</div>`}
                <div class="form-text">${i18n('agents.tasksHint')}</div>
                <div class="d-flex gap-2 mt-2">
                    <a href="#/tasks/new/${App.escAttr(agentId)}" class="btn btn-sm btn-outline-primary">
                        <i class="bi bi-plus-lg"></i> ${i18n('agents.tasksAdd')}</a>
                    <a href="#/tasks" class="btn btn-sm btn-outline-secondary">${i18n('agents.tasksManage')}</a>
                </div>
            </div>`;
    },

    /** The autonomous block as the server wants it: the object, or null when
     *  every field is at its default (models.py: `autonomous: None` == all
     *  defaults, and the object is only stored once customized). */
    readAutoConfig() {
        const cfg = {};
        let allDefault = true;
        this.AUTO_FIELDS.forEach(f => {
            // Numbers go through RangeField.read (slider + number pair, number
            // authoritative); the two text fields are plain inputs.
            const v = f.type === 'int'
                ? (RangeField.read(f.el) ?? f.dflt)
                : document.getElementById(f.el).value.trim();
            cfg[f.key] = v;
            if (v !== f.dflt) allDefault = false;
        });
        return allDefault ? null : cfg;
    },

    /** Show the tab pane containing `el`, then run `after` once it is visible.
     *  bootstrap.Tab.show() is asynchronous, so anything that needs the element
     *  to be focusable has to wait for shown.bs.tab. */
    activateTabFor(el, after) {
        const pane = el.closest('.tab-pane');
        const trigger = pane && document.querySelector(`[data-bs-target="#${pane.id}"]`);
        if (!pane || !trigger || pane.classList.contains('active')) { if (after) after(); return; }
        if (after) trigger.addEventListener('shown.bs.tab', after, { once: true });
        bootstrap.Tab.getOrCreateInstance(trigger).show();
    },

    /** HTML5 validation across tabs.
     *
     *  A `required` control inside a hidden pane is NOT focusable: the browser
     *  refuses the submit and tells the user nothing (console only), so the form
     *  looks dead. The form therefore carries `novalidate` and we drive
     *  validation ourselves — find the first invalid control, ACTIVATE ITS TAB,
     *  mark that tab, and only then let the browser draw its own bubble on a
     *  control that is now visible.
     *
     *  Returns true when the form may be submitted. */
    validateAcrossTabs(form) {
        form.querySelectorAll('.nav-link.tab-error').forEach(t => t.classList.remove('tab-error'));
        if (form.checkValidity()) return true;
        const bad = form.querySelector(':invalid');
        if (!bad) return false;
        const pane = bad.closest('.tab-pane');
        const trigger = pane && document.querySelector(`[data-bs-target="#${pane.id}"]`);
        if (trigger) {
            trigger.classList.add('tab-error');
            trigger.title = i18n('agents.tabInvalid');
        }
        this.activateTabFor(bad, () => bad.reportValidity());
        return false;
    },

    /** Tab labels carry the state of the pane they hide, so nothing important is
     *  invisible: the granted-tool count, a dot when persistent memory is on
     *  (amber when saving would widen the grant), and the same broadcast icon
     *  the list card uses when the agent is live.
     *
     *  `d` is the object readForm() would send — ONE source of truth, so a badge
     *  can never disagree with what actually gets saved. */
    setTabIndicators(d, notes = {}) {
        const set = (tabId, html) => {
            const slot = document.querySelector(`#${tabId} .tab-state`);
            if (slot) slot.innerHTML = html;
        };
        set('tab-tools', `<span class="badge text-bg-secondary" title="${App.escAttr(i18n('agents.tabToolsCount', { n: d.tools.length }))}">${d.tools.length}</span>`);
        set('tab-memory', d.memory_enabled
            ? `<span class="tab-dot${notes.memoryWidening ? ' tab-dot-warn' : ''}" title="${App.escAttr(i18n('agents.tabMemoryOn'))}"></span>`
            : '');
        set('tab-autonomy', d.live
            ? `<i class="bi bi-broadcast" title="${App.escAttr(i18n('agents.live'))}"></i>`
            : '');
    },

    /** Tool checkbox list: ungrouped folder tools first, then one group per
     * tool category (folder group under tools/), then one group per MCP server.
     * Category and MCP groups behave the same way: a master checkbox grants the
     * whole group as a wildcard (`<category>/*` / `mcp:<server>/*`), or the
     * tools are selectable one by one.
     *
     * Every input carries class `tool-check` and its value is exactly what goes
     * into agent.tools, so readForm() needs no special cases. Ids the agent holds
     * but that no longer exist anywhere are rendered checked+disabled: readForm()
     * reads checked inputs (disabled included), so opening and saving an agent
     * while a server is down cannot silently drop its selections.
     *
     * `hidden` ({ ids, categories }, from hiddenToolSet()) excludes the grants the
     * form manages elsewhere — derived ones (memory, call_agent) and relocated
     * ones (autonomy). It filters the rows ONLY: the orphan set below is still
     * built from the unfiltered catalog, see the comment there. */
    toolPicker(tools, agentTools, mcpStatus, hidden) {
        const local = tools.filter(t => t.source !== 'mcp');
        const mcp = tools.filter(t => t.source === 'mcp');
        const status = mcpStatus || {};
        const hide = hidden || { ids: new Set(), categories: new Set() };
        const visible = local.filter(t => !hide.ids.has(t.id) && !hide.categories.has(t.category));

        const row = (value, label, checked, extra = '', cls = '', attrs = '') => `
            <div class="form-check">
                <input class="form-check-input tool-check ${cls}" type="checkbox" value="${App.escAttr(value)}"
                       id="tool-${App.escAttr(value)}" ${checked ? 'checked' : ''} ${attrs}>
                <label class="form-check-label" for="tool-${App.escAttr(value)}">${label}</label>
                ${extra}
            </div>`;

        // One collapsible-looking block per group: master wildcard row + the
        // individual tools indented under it. `key` must be unique across group
        // kinds ("cat:<name>" / "mcp:<sid>"), it only wires master <-> children.
        const groupBlock = (key, wildcard, head, tools_, emptyText) => `
            <div class="mt-2 pt-2 border-top">
                ${row(wildcard, head, agentTools.includes(wildcard),
                    '', 'group-all', `data-group-all="${App.escAttr(key)}"`)}
                ${tools_.length > 10 ? `<div class="form-text text-warning ms-4">${i18n('agents.mcpManyTools', { n: tools_.length })}</div>` : ''}
                <div class="ms-4" data-group-box="${App.escAttr(key)}">
                    ${tools_.length === 0 ? `<div class="form-text text-secondary">${emptyText}</div>` : ''}
                    ${tools_.map(t => row(
                        t.id,
                        `${App.esc(t.name)} <small class="text-secondary">(${App.esc(t.id)})</small>`,
                        agentTools.includes(t.id),
                        '',
                        'group-tool',
                    )).join('')}
                </div>
            </div>`;

        const flat = visible.filter(t => !t.category);
        let html = flat.map(t => row(
            t.id,
            `<strong>${App.esc(t.name)}</strong> <small class="text-secondary">(${App.esc(t.id)})</small>`,
            agentTools.includes(t.id),
        )).join('');

        // Category groups (folder groups under tools/), selectable wholesale
        // via the `<category>/*` wildcard — the analogue of mcp:<server>/*.
        const cats = new Map();
        visible.filter(t => t.category).forEach(t => {
            if (!cats.has(t.category)) cats.set(t.category, []);
            cats.get(t.category).push(t);
        });
        [...cats.keys()].sort().forEach(cat => {
            html += groupBlock(`cat:${cat}`, `${cat}/*`,
                `<i class="bi bi-folder2-open"></i> <strong>${App.esc(cat)}</strong>
                 <small class="text-secondary">${i18n('agents.catAllTools')}</small>`,
                cats.get(cat), '');
        });

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
            // The state belongs on the server, not on each tool: right after a
            // restart nothing is connected yet and the tools still work (the
            // first turn connects them), so only a real error is worth flagging.
            const state = (status[group.sid] || {}).state;
            const badge = state === 'error'
                ? `<span class="badge bg-danger ms-1" title="${App.escAttr((status[group.sid] || {}).last_error || '')}">${i18n('mcp.stateError')}</span>`
                : state === 'disabled'
                    ? `<span class="badge bg-secondary ms-1">${i18n('mcp.stateDisabled')}</span>` : '';
            html += groupBlock(`mcp:${group.sid}`, `mcp:${group.sid}/*`,
                `<i class="bi bi-plugin"></i> <strong>${App.esc(group.sid)}</strong>
                 <small class="text-secondary">${i18n('agents.mcpAllTools')}</small> ${badge}`,
                group.tools, i18n('agents.mcpNoTools'));
        });

        // Ids the agent references that no longer exist in any source. Built from
        // the UNFILTERED tool list and the full category set on purpose: a hidden
        // grant (`memory/*`, the memory tool ids, `call_agent`, `autonomy/*`)
        // still EXISTS, it is merely not offered here. Listing one as "no longer
        // available" would render it as a checked+disabled row that readForm()
        // then re-emits ALONGSIDE the derived grant — two grants for one tool,
        // and a scary UI. So `cats` (filtered, drives the rows) is deliberately
        // not reused here.
        const allCats = new Set(local.filter(t => t.category).map(t => t.category));
        const known = new Set([
            ...tools.map(t => t.id),
            ...[...allCats].map(c => `${c}/*`),
            ...servers.map(g => `mcp:${g.sid}/*`),
        ]);
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
        // callable_agents defaults to [] (no delegation) for NEW agents only:
        // the user opts in explicitly. Existing agents keep their stored value.
        let agent = { id: '', name: '', description: '', model_id: '', system_prompt: '', tools: [], max_iterations: 10, max_tool_calls: 5, temperature: 0.7, enabled: true, callable: true, callable_agents: [], memory_enabled: false, memory_threshold: 4000, live: false, autonomous: null };
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

        // Seven independent GETs (two of them — connectors proxy and MCP status —
        // can be slow): fetched together instead of paying the sum of their
        // latencies on every form open. Each degrades on its own.
        const [models, tools, agents, autoStatus, bindingsRes, mcpStatus, agentTasks] =
            await Promise.all([
                App.api('GET', '/models').catch(() => []),
                App.api('GET', '/tools').catch(() => []),
                App.api('GET', '/agents').catch(() => []),
                // Autonomy status, so "wake now" has something to report into.
                App.api('GET', '/autonomy/status').catch(() => ({})),
                // Notify targets, owned by the connectors plugin. Asked for only
                // when the plugin is actually loaded: without it the route does
                // not exist, and calling it anyway would log a 404 in the console
                // on every form open of a perfectly healthy install. The probe is
                // cached per page load, so this costs no extra request.
                (async () => (await App.plugin('connectors'))?.loaded
                    ? App.api('GET', '/connectors/bindings').catch(() => null)
                    : null)(),
                // Per-server state: a broken MCP server shows in the tool picker.
                App.api('GET', '/mcp/status').catch(() => ({})),
                // This agent's schedule, read-only here: WHEN it acts is the
                // Tasks page's business, but hiding it from the Autonomy tab
                // would leave "Live" looking like the whole story.
                agentId ? App.api('GET', `/tasks?agent_id=${encodeURIComponent(agentId)}`)
                    .catch(() => []) : [],
            ]);
        // The plugin answers with a plain array; a failed fetch gives null, and
        // that IS the "not available" signal — no separate flag needed.
        const bindings = Array.isArray(bindingsRes) ? bindingsRes : [];
        const bindingsAvailable = Array.isArray(bindingsRes);
        // Cache for the preview tree (opened from this form with unsaved values).
        this._allAgents = agents;
        this._allTools = tools;

        const agentTools = agent.tools || [];
        const callableList = agent.callable_agents || ['*'];
        const callableAll = callableList.includes('*');
        const otherAgents = agents.filter(a => a.id !== agent.id);
        const auto = agent.autonomous || {};
        const hidden = this.hiddenToolSet();

        // Whether the STORED agent already held a memory grant. Turning the flag
        // on is a WIDENING for the ones that didn't (an agent can sit with
        // memory_enabled: true and no memory tool: it pays for compaction and
        // gets `## Memory` injected, but has no way to look anything up), and a
        // widening is worth saying out loud rather than discovering later.
        const memToolIds = tools.filter(t => t.category === 'memory').map(t => t.id);
        const memToolNames = memToolIds.join(', ');
        const hadMemTools = agentTools.some(t => t === 'memory/*' || memToolIds.includes(t));

        // Relocated groups: rendered in their own tab instead of the picker. A
        // stored group wildcard is expanded into per-tool state here, since the
        // members are independently meaningful (see RELOCATED_TOOLS).
        const relocated = this.RELOCATED_TOOLS.map(r => ({
            ...r,
            tools: tools.filter(t => t.category === r.category),
            all: agentTools.includes(r.expand),
        }));
        const isRelocatedOn = (group, id) => group.all || agentTools.includes(id);

        // An unset model_id and the "default" sentinel mean the same thing to the
        // executor, so they collapse onto one option. A model_id that matches
        // nothing is kept as its own selected option instead: the reference is
        // dangling, and silently rewriting it to "default" on the next save would
        // hide that (same reasoning as the preserved orphan tool rows).
        const modelMissing = !!agent.model_id && agent.model_id !== 'default'
            && !models.some(m => m.id === agent.model_id);
        const modelIsDefault = !agent.model_id || agent.model_id === 'default';

        // Delegation is the call_agent grant itself, read straight off the stored
        // tools. NOT inferred from callable_agents: that field defaults to ["*"]
        // server-side and GET returns the raw stored dict, so an agent file
        // omitting the key (manage_agents writes them that way) would look like
        // "delegates to everyone" and get the grant on its first save.
        const canDelegate = agentTools.includes('call_agent');

        const specs = this.numSpecs();
        const modelById = {};
        models.forEach(m => { modelById[m.id] = m; });
        // Everything a slider's sentence needs from OUTSIDE its own field, in one
        // visible place. Called on EVERY refresh, so nothing goes stale — hence a
        // function, not an object.
        const rfCtx = () => ({
            temp: RangeField.read('f-temp') ?? 0.7,
            maxIter: RangeField.read('f-maxiter') ?? 10,
            maxWakes: RangeField.read('f-auto-maxwakes') ?? 12,
            maxErrors: RangeField.read('f-auto-maxerrors') ?? 5,
            memoryOn: document.getElementById('f-memory')?.checked ?? false,
            model: modelById[document.getElementById('f-model')?.value] || null,
        });
        // Render-time context: the DOM does not exist yet, so read from the agent.
        // Only the end labels use it; wireAll() repaints everything right after.
        const rfCtx0 = { temp: agent.temperature ?? 0.7, model: modelById[agent.model_id] || null };

        const activeTab = (this._formTab.id === (agentId || '')) ? this._formTab.tab : 'general';
        this._formTab = { id: agentId || '', tab: activeTab };
        const tab = (key, icon, label) => `
            <li class="nav-item" role="presentation">
                <button class="nav-link${activeTab === key ? ' active' : ''}" id="tab-${key}" type="button" role="tab"
                        data-bs-toggle="tab" data-bs-target="#pane-${key}" aria-controls="pane-${key}"
                        aria-selected="${activeTab === key}">
                    <i class="bi ${icon}"></i> <span class="tab-label">${label}</span>
                    <span class="tab-state"></span>
                </button>
            </li>`;
        const pane = (key, body) => `
            <div class="tab-pane${activeTab === key ? ' show active' : ''}" id="pane-${key}"
                 role="tabpanel" aria-labelledby="tab-${key}" tabindex="0">${body}</div>`;

        App.container.innerHTML = `
            <div class="row">
                <div class="col-lg-8 mx-auto">
                    <h3>${isEdit ? i18n('agents.editTitle') : i18n('agents.newTitle')}</h3>
                    <!-- novalidate: a required control inside a hidden tab pane is not
                         focusable, so the browser would refuse the submit and tell the
                         user NOTHING. validateAcrossTabs() drives validation instead. -->
                    <form id="agent-form" novalidate>
                        <ul class="nav nav-tabs agent-tabs mb-3" id="agent-tabs" role="tablist">
                            ${tab('general', 'bi-info-circle', i18n('agents.tabGeneral'))}
                            ${tab('tools', 'bi-tools', i18n('agents.tools'))}
                            ${tab('memory', 'bi-journal-text', i18n('agents.memory'))}
                            ${tab('autonomy', 'bi-broadcast', i18n('agents.autonomy'))}
                            ${tab('advanced', 'bi-sliders', i18n('agents.tabAdvanced'))}
                        </ul>
                        <!-- EVERY pane is rendered here, in one pass, ALWAYS. Bootstrap only
                             sets display:none on the inactive ones, and readForm() reaches
                             into all of them by id and by .tool-check:checked. Rendering a
                             pane lazily (on first shown.bs.tab) would silently truncate the
                             tools array, lose the autonomy knobs and throw in readForm().
                             Do not "optimize" this. -->
                        <div class="tab-content" id="agent-panes">
                            ${pane('general', `
                                <div class="mb-3">
                                    <label class="form-label" for="f-id">${i18n('common.id')}</label>
                                    <!-- The hyphen MUST be escaped: browsers compile the pattern
                                         attribute with the RegExp "v" flag, where a trailing bare "-"
                                         in a character class is a syntax error. An uncompilable
                                         pattern is silently ignored (the id was unconstrained), and
                                         checkValidity() logs a SyntaxError on every submit. -->
                                    <input type="text" class="form-control" id="f-id" value="${App.escAttr(agent.id)}" ${isEdit ? 'readonly' : ''} required
                                           pattern="[a-z0-9_\\-]+" title="${i18n('agents.idHint')}">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label" for="f-name">${i18n('common.name')}</label>
                                    <input type="text" class="form-control" id="f-name" value="${App.escAttr(agent.name)}" required>
                                </div>
                                <div class="mb-3">
                                    <label class="form-label" for="f-desc">${i18n('common.description')}</label>
                                    <input type="text" class="form-control" id="f-desc" value="${App.escAttr(agent.description || '')}">
                                </div>
                                <div class="mb-3">
                                    <label class="form-label" for="f-model">${i18n('agents.model')}</label>
                                    <!-- No empty "select a model" option: the executor treats an unset
                                         model_id and the "default" sentinel identically (both fall back
                                         to settings.default_model_id), so an empty choice was a second
                                         name for Default, not a state of its own. Hence no "required"
                                         either — there is nothing invalid left to pick. -->
                                    <select class="form-select" id="f-model">
                                        <option value="default" ${modelIsDefault ? 'selected' : ''}>${i18n('agents.defaultModel')}</option>
                                        ${models.map(m => `<option value="${App.escAttr(m.id)}" ${m.id === agent.model_id ? 'selected' : ''}>${App.esc(m.name)} (${App.esc(m.provider)})</option>`).join('')}
                                        ${modelMissing ? `<option value="${App.escAttr(agent.model_id)}" selected>${App.esc(agent.model_id)} — ${i18n('agents.modelMissing')}</option>` : ''}
                                    </select>
                                    ${modelMissing ? `<div class="form-text text-warning-emphasis"><i class="bi bi-exclamation-triangle"></i> ${i18n('agents.modelMissingHint')}</div>` : ''}
                                </div>
                                <div class="mb-3">
                                    <label class="form-label" for="f-prompt">${i18n('agents.systemPrompt')}</label>
                                    <textarea class="form-control system-prompt-textarea" id="f-prompt" rows="8">${App.esc(agent.system_prompt)}</textarea>
                                </div>`)}
                            ${pane('tools', `
                                <div class="mb-3">
                                    <label class="form-label">${i18n('agents.tools')}</label>
                                    <div class="border rounded p-2" style="max-height:min(60vh,420px);overflow-y:auto">
                                        ${this.toolPicker(tools, agentTools, mcpStatus, hidden)}
                                    </div>
                                    <div class="form-text"><i class="bi bi-magic"></i> ${i18n('agents.toolsManagedElsewhere')}</div>
                                </div>
                                <div class="mb-3">
                                    <label class="form-label">${i18n('agents.delegation')}</label>
                                    <div class="border rounded p-2">
                                        <div class="form-check form-switch">
                                            <input class="form-check-input" type="checkbox" id="f-delegate" ${canDelegate ? 'checked' : ''}>
                                            <label class="form-check-label" for="f-delegate"><strong>${i18n('agents.canDelegate')}</strong></label>
                                            <div class="form-text">${i18n('agents.canDelegateHelp')}</div>
                                        </div>
                                        <hr class="my-2">
                                        <div id="delegation-scope">
                                            <label class="form-label small mb-1">${i18n('agents.callableAgents')}</label>
                                            <div class="form-check">
                                                <input class="form-check-input" type="checkbox" id="f-callable-all" ${callableAll ? 'checked' : ''}>
                                                <label class="form-check-label" for="f-callable-all">${i18n('agents.callableAll')}</label>
                                            </div>
                                            <div id="callable-list" class="ms-3 mt-1" style="max-height:160px;overflow-y:auto">
                                                ${otherAgents.map(a => `
                                                    <div class="form-check">
                                                        <input class="form-check-input agent-check" type="checkbox" value="${App.escAttr(a.id)}" id="ca-${App.escAttr(a.id)}"
                                                            ${callableAll || callableList.includes(a.id) ? 'checked' : ''}>
                                                        <label class="form-check-label" for="ca-${App.escAttr(a.id)}">
                                                            <strong>${App.esc(a.name)}</strong> <small class="text-secondary">(${App.esc(a.id)})</small>
                                                        </label>
                                                    </div>
                                                `).join('')}
                                                ${otherAgents.length === 0 ? `<div class="text-secondary small">${i18n('agents.empty')}</div>` : ''}
                                            </div>
                                            <div class="form-text">${i18n('agents.delegateAutoOn')}</div>
                                        </div>
                                        <div id="delegate-warn" class="form-text text-warning-emphasis d-none">
                                            <i class="bi bi-exclamation-triangle"></i> ${i18n('agents.delegateNoTargets')}
                                        </div>
                                        <hr class="my-2">
                                        <div class="form-check">
                                            <input class="form-check-input" type="checkbox" id="f-callable" ${agent.callable !== false ? 'checked' : ''}>
                                            <label class="form-check-label" for="f-callable">${i18n('agents.callable')}</label>
                                            <div class="form-text">${i18n('agents.callableHelp')}</div>
                                        </div>
                                        <div class="form-text">${i18n('agents.callableHint')}</div>
                                    </div>
                                </div>`)}
                            ${pane('memory', `
                                <div class="form-check form-switch mb-2">
                                    <input class="form-check-input" type="checkbox" id="f-memory" ${agent.memory_enabled ? 'checked' : ''}>
                                    <label class="form-check-label" for="f-memory"><strong>${i18n('agents.memoryEnabled')}</strong></label>
                                    <div class="form-text">${i18n('agents.memoryHelp')}</div>
                                </div>
                                <div id="memory-note" class="mb-3"></div>
                                <div class="row">
                                    <div class="col-12 col-md-6">
                                        ${RangeField.render(specs['f-memthreshold'], agent.memory_threshold ?? 4000)}
                                    </div>
                                </div>`)}
                            ${pane('autonomy', `
                                <div class="form-check form-switch mb-2">
                                    <input class="form-check-input" type="checkbox" id="f-live" ${agent.live ? 'checked' : ''}>
                                    <label class="form-check-label" for="f-live"><strong>${i18n('agents.live')}</strong></label>
                                    <div class="form-text">${i18n('agents.liveHelp')}</div>
                                </div>
                                ${isEdit ? `
                                <div class="border rounded p-2 mb-3">
                                    <div class="d-flex flex-wrap gap-2 align-items-center">
                                        <button type="button" class="btn btn-sm btn-outline-primary" id="btn-wake">
                                            <i class="bi bi-lightning-charge"></i> ${i18n('agents.wakeNow')}
                                        </button>
                                        <span class="small text-secondary" id="wake-status"></span>
                                    </div>
                                    <div class="form-text">${i18n('agents.wakeNowHint')}</div>
                                </div>` : ''}
                                ${this.taskBox(agentId, agentTasks)}
                                ${relocated.map(group => group.tools.length === 0 ? '' : `
                                    <div class="border rounded p-2 mb-3">
                                        <label class="form-label small mb-1">${i18n('agents.autoTools')}</label>
                                        ${group.tools.map(t => `
                                            <div class="form-check">
                                                <input class="form-check-input tool-check" type="checkbox" value="${App.escAttr(t.id)}"
                                                       id="tool-${App.escAttr(t.id)}" ${isRelocatedOn(group, t.id) ? 'checked' : ''}>
                                                <label class="form-check-label" for="tool-${App.escAttr(t.id)}">
                                                    ${App.esc(this.toolText('toolLabel', t, t.name))}
                                                </label>
                                                <div class="form-text">${App.esc(this.toolText('toolHint', t, t.description))}</div>
                                            </div>`).join('')}
                                        <div class="form-text">${i18n('agents.autoToolsHint')}</div>
                                        <div id="live-warn" class="form-text text-warning-emphasis d-none">
                                            <i class="bi bi-exclamation-triangle"></i> ${i18n('agents.liveNoNotify')}
                                        </div>
                                    </div>`).join('')}
                                <h6 class="small text-secondary">${i18n('agents.autonomySettings')}</h6>
                                <div class="row g-3">
                                    ${this.autoGrid(auto, specs)}
                                </div>
                                <div class="row g-2 mt-1">
                                    <div class="col-6">
                                        <label class="form-label small mb-1" for="f-auto-binding">${i18n('agents.autoBinding')}</label>
                                        ${this.bindingField(bindings, auto.notify_binding_id || '')}
                                    </div>
                                    <div class="col-6">
                                        <label class="form-label small mb-1" for="f-auto-chat">${i18n('agents.autoChat')}</label>
                                        <!-- Stays free text on purpose: allowed_ids only covers private
                                             chats (there the user id IS the chat id), never groups. So
                                             the ids are offered as suggestions, not as a closed set. -->
                                        <input type="text" class="form-control form-control-sm" id="f-auto-chat"
                                               list="chat-id-options" autocomplete="off"
                                               value="${App.escAttr(auto.notify_chat_id || '')}">
                                        <datalist id="chat-id-options"></datalist>
                                    </div>
                                </div>
                                <div class="form-text">${i18n('agents.autoNotifyHint')}
                                    <a href="#/connectors">${i18n('agents.autoBindingManage')}</a></div>
                                <!-- Filled by syncNotifyTargets(): either the chat-id
                                     suggestions or the "plugin not active" warning. It is
                                     written with textContent, so the link above cannot
                                     live in here. -->
                                <div class="form-text" id="notify-hint"></div>`)}
                            ${pane('advanced', `
                                <div class="row g-3">
                                    <div class="col-12 col-lg-6">${RangeField.render(specs['f-maxiter'], agent.max_iterations ?? 10)}</div>
                                    <div class="col-12 col-lg-6">${RangeField.render(specs['f-maxtools'], agent.max_tool_calls ?? 5)}</div>
                                    <div class="col-12 col-lg-6">${RangeField.render(specs['f-temp'], agent.temperature ?? 0.7)}</div>
                                    <div class="col-12 col-lg-6">${RangeField.render(specs['f-resp-temp'], agent.response_temperature ?? null, rfCtx0)}</div>
                                </div>`)}
                        </div>
                        <div class="d-flex gap-2 mt-3">
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

        // Sliders: listeners, cross-field dependencies and the first paint of every
        // value badge and explanation. No extra wiring is needed for f-memory or
        // f-model — they are listed in `deps` and wireAll attaches to them itself.
        RangeField.wireAll(specs, rfCtx);

        const delegateEl = document.getElementById('f-delegate');
        const callableAllEl = document.getElementById('f-callable-all');
        const liveEl = document.getElementById('f-live');

        // Read the current (possibly unsaved) form values into an agent object.
        // Declared BEFORE the wiring below, because refreshState() calls it.
        const readForm = () => {
            // Everything the picker offers, from EVERY pane — the relocated
            // autonomy tools live in #pane-autonomy, which is why no pane may be
            // rendered lazily.
            const picked = [...document.querySelectorAll('.tool-check:checked')].map(c => c.value);
            const state = {
                memory_enabled: document.getElementById('f-memory').checked,
                delegate: delegateEl.checked,
                live: liveEl.checked,
            };
            return {
                id: document.getElementById('f-id').value.trim(),
                name: document.getElementById('f-name').value.trim(),
                description: document.getElementById('f-desc').value.trim(),
                model_id: document.getElementById('f-model').value,
                system_prompt: document.getElementById('f-prompt').value,
                // Derived grants come AFTER the picked ones so a hidden id can
                // never be contributed twice (it is not rendered, so it cannot be
                // picked — the Set is insurance, not the mechanism).
                tools: [...new Set([...picked, ...this.derivedGrants(state)])],
                // RangeField.read, not `parseInt(...) || dflt`: 0 is a legitimate
                // value here (temperature 0 = greedy decoding) and the old idiom
                // silently turned it into the default.
                max_iterations: RangeField.read('f-maxiter'),
                max_tool_calls: RangeField.read('f-maxtools'),
                temperature: RangeField.read('f-temp'),
                response_temperature: RangeField.read('f-resp-temp'),   // null = inherit
                enabled: true,
                callable: document.getElementById('f-callable').checked,
                callable_agents: callableAllEl.checked
                    ? ['*']
                    : [...document.querySelectorAll('.agent-check:checked')].map(c => c.value),
                memory_enabled: state.memory_enabled,
                memory_threshold: RangeField.read('f-memthreshold'),
                live: state.live,
                // null = all defaults; the object is only stored when customized.
                autonomous: this.readAutoConfig(),
            };
        };

        // Everything that reflects form state is computed here, from the object
        // readForm() is about to send: the tab badges, the derived-grant note and
        // the two "this cannot work" warnings. One source of truth means the Tools
        // badge can never disagree with the tools actually saved.
        const refreshState = () => {
            const d = readForm();
            const widening = d.memory_enabled && !hadMemTools;

            const note = document.getElementById('memory-note');
            if (!d.memory_enabled) {
                note.className = 'mb-3 form-text text-secondary';
                note.textContent = i18n('agents.memoryToolsOff');
            } else if (widening) {
                note.className = 'mb-3 form-text text-warning-emphasis';
                note.innerHTML = `<i class="bi bi-exclamation-triangle"></i> `
                    + App.esc(i18n('agents.memoryToolsAdded', { tools: memToolNames }));
            } else {
                note.className = 'mb-3 form-text';
                note.innerHTML = `<i class="bi bi-magic"></i> `
                    + App.esc(i18n('agents.memoryToolsDerived', { tools: memToolNames }));
            }

            // Delegation granted with nothing reachable: every call_agent call is
            // refused server-side and the iterations are burned. Surfaced, not
            // silently mutated. Read from the switch, not from `d`: the payload
            // carries the resulting grant, not the control's state.
            const noTargets = delegateEl.checked && !callableAllEl.checked
                && document.querySelectorAll('.agent-check:checked').length === 0;
            document.getElementById('delegate-warn').classList.toggle('d-none', !noTargets);

            // A live agent's reply text goes nowhere (the wake prompt says so), so
            // live without notify_user is an agent that can never reach the user.
            const notify = document.getElementById('tool-notify_user');
            const liveWarn = document.getElementById('live-warn');
            if (liveWarn) liveWarn.classList.toggle('d-none', !(d.live && notify && !notify.checked));

            this.setTabIndicators(d, { memoryWidening: widening });
        };

        // One function owns the delegation block's enabled state: the master
        // switch IS the call_agent grant, and inside it "All agents (*)"
        // supersedes the per-agent list.
        //
        // The per-agent checkboxes are deliberately NOT disabled when delegation
        // is off — clicking one is how you turn delegation on. They are only
        // dimmed; at 0.6 opacity they still take clicks.
        const syncDelegation = () => {
            const all = callableAllEl.checked;
            document.querySelectorAll('.agent-check').forEach(c => { c.disabled = all; });
            document.getElementById('callable-list').style.opacity = all ? '0.5' : '1';
            document.getElementById('delegation-scope').style.opacity = delegateEl.checked ? '1' : '0.6';
            refreshState();
        };
        delegateEl.onchange = syncDelegation;
        callableAllEl.onchange = syncDelegation;

        // Picking a target implies delegation, closing the inverse footgun (a
        // non-empty allowlist the agent has no tool to use — silently impossible
        // today). ONLY on a real user change: `f-callable-all` can arrive
        // pre-checked from an agent file that simply omits callable_agents, and
        // inferring a capability from a materialized default is exactly the trap
        // this design avoids.
        const implyDelegate = () => {
            if (!delegateEl.checked) { delegateEl.checked = true; }
            syncDelegation();
        };
        callableAllEl.addEventListener('change', (e) => { if (e.target.checked) implyDelegate(); });
        document.querySelectorAll('.agent-check').forEach(c => {
            c.addEventListener('change', () => { if (c.checked) implyDelegate(); });
        });

        // First time Live is switched on, hand the agent the two tools it needs to
        // be useful unattended: notify_user is its ONLY way out, and manage_tasks
        // lets it carry work forward. autonomy_control is NOT auto-granted — it
        // lets the agent flip its own live flag, a deliberate choice, and it is
        // just as useful with Live off.
        liveEl.onchange = () => {
            if (liveEl.checked) {
                ['notify_user', 'manage_tasks'].forEach(id => {
                    const el = document.getElementById(`tool-${id}`);
                    if (el && !el.disabled) el.checked = true;
                });
            }
            refreshState();
        };

        // Force a wake now — the main way to test an autonomous agent without
        // waiting for its next scheduled task. Works even when Live is off, which is why it
        // is not hidden behind the switch: it is how you try an agent BEFORE
        // committing to letting it run by itself.
        if (isEdit) {
            const wakeBtn = document.getElementById('btn-wake');
            const wakeOut = document.getElementById('wake-status');
            const showStatus = (st) => {
                if (!st) { wakeOut.textContent = ''; return; }
                const bits = [this.stateLabel(st.state), ...this.wakeSummary(st)];
                wakeOut.textContent = bits.filter(Boolean).join(' · ');
            };
            showStatus(autoStatus[agentId]);
            wakeBtn.onclick = async () => {
                wakeBtn.disabled = true;
                // The scheduler runs the turn in the background and answers at
                // once, so this reports "started", not "finished" — the status
                // line below is where the outcome shows up.
                try {
                    await App.api('POST', `/autonomy/${agentId}/wake`);
                    App.toast(i18n('agents.wakeTriggered'));
                    wakeOut.textContent = i18n('agents.wakeRunning');
                } catch (err) {
                    App.toast(err.message, 'danger');
                    wakeBtn.disabled = false;
                    return;
                }
                // Poll until the wake lands, then show what it did.
                const started = Date.now();
                const tick = async () => {
                    let st = null;
                    try { st = (await App.api('GET', '/autonomy/status'))[agentId]; } catch (e) { /* keep polling */ }
                    if (st && st.state !== 'running') { showStatus(st); wakeBtn.disabled = false; return; }
                    if (Date.now() - started > 15 * 60 * 1000) { wakeBtn.disabled = false; return; }
                    setTimeout(tick, 3000);
                };
                setTimeout(tick, 2000);
            };
        }

        // Chat-id suggestions follow the chosen binding. For a Telegram private
        // chat the user id IS the chat id, so the binding's allowlist is the only
        // real source we have — but it never contains group chat ids, hence a
        // <datalist> (suggestions) rather than a <select>, plus a hint saying so.
        const bindingEl = document.getElementById('f-auto-binding');
        const syncNotifyTargets = () => {
            const opt = bindingEl.selectedOptions ? bindingEl.selectedOptions[0] : null;
            const ids = (opt?.dataset.allowed || '').split(',').filter(Boolean);
            document.getElementById('chat-id-options').innerHTML =
                ids.map(id => `<option value="${App.escAttr(id)}"></option>`).join('');
            const hint = document.getElementById('notify-hint');
            hint.className = 'form-text' + (bindingsAvailable ? '' : ' text-warning-emphasis');
            hint.textContent = !bindingsAvailable
                ? i18n('agents.autoBindingUnavailable')
                : (ids.length ? i18n('agents.autoChatSuggest', { n: ids.length }) : '');
        };
        bindingEl.addEventListener('change', syncNotifyTargets);
        syncNotifyTargets();

        // A group's "all tools" entry (tool category or MCP server) supersedes
        // its individual tools: uncheck them as well as disabling them, since
        // readForm() reads checked inputs regardless of the disabled state
        // (both grants would be sent).
        document.querySelectorAll('[data-group-all]').forEach(master => {
            const group = document.querySelector(`[data-group-box="${CSS.escape(master.dataset.groupAll)}"]`);
            const sync = () => {
                group.querySelectorAll('.group-tool').forEach(c => {
                    if (master.checked) c.checked = false;
                    c.disabled = master.checked;
                });
                group.style.opacity = master.checked ? '0.5' : '1';
                refreshState();
            };
            master.onchange = sync;
            sync();
        });

        // `change` bubbles, so this fires AFTER each control's own handler (the
        // group-all sync has already run). Programmatic `.checked = x` does NOT
        // fire it, which is why the handlers above call refreshState() themselves
        // and why it recomputes from the DOM instead of tracking deltas.
        document.getElementById('agent-form').addEventListener('change', refreshState);

        // Remember the tab so reopening the SAME agent lands where you left off,
        // and keep the active trigger on screen on a narrow, scrolling strip.
        document.getElementById('agent-tabs').addEventListener('shown.bs.tab', (e) => {
            this._formTab = { id: agentId || '', tab: e.target.dataset.bsTarget.replace('#pane-', '') };
            e.target.scrollIntoView({ inline: 'nearest', block: 'nearest' });
        });

        syncDelegation();
        document.querySelector('#agent-tabs .nav-link.active')
            ?.scrollIntoView({ inline: 'nearest', block: 'nearest' });

        document.getElementById('btn-preview').onclick = () => {
            const draft = readForm();
            draft.id = draft.id || i18n('agents.previewNewId');
            draft.name = draft.name || draft.id;
            this.openPreview(draft);
        };

        document.getElementById('agent-form').onsubmit = async (e) => {
            e.preventDefault();
            // The form is `novalidate`: an invalid control on a hidden tab would
            // otherwise block the submit with no visible feedback at all.
            if (!this.validateAcrossTabs(e.currentTarget)) return;
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
            const catw = /^([A-Za-z0-9][A-Za-z0-9._-]*)\/\*$/.exec(tid);
            if (catw) {
                return `<li><i class="bi bi-folder2-open"></i> <strong>${App.esc(catw[1])}</strong>
                        <small class="text-secondary">${i18n('agents.catAllTools')}</small></li>`;
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
