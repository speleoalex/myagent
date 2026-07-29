/**
 * RangeField — a numeric form field that explains itself.
 *
 * A slider alone cannot own a value here: the server stores what the UI sends
 * verbatim (there is no ge=/le= anywhere in app/models.py), so a stored value may
 * sit outside any range this UI believes in (interval_s = 86400, temperature
 * 0.75). A pure slider would clamp it on save and destroy it. So every field is a
 * PAIR:
 *
 *   <input type="range">  -- picks a value, never holds it
 *   <input type="number"> -- holds it, keeps the field's id, is what read() reads
 *
 * The range writes into the number input on every move; nothing ever writes back
 * from the range on save. That is why an off-step or out-of-range value survives a
 * round trip even though the thumb cannot always point at it exactly.
 *
 * A second, less obvious reason for the pair: an out-of-range value fails HTML
 * constraint validation, and a form that fails validation never fires `submit` at
 * all — the Save button would silently do nothing. render() therefore widens
 * min/max (and uses step="any" on floats) to include the stored value.
 *
 * The spec table lives with the page that owns the fields (AgentsPage.numSpecs),
 * the same split as ModelsPage.optionSpecs + the generic .opt-field readback.
 * Nothing here knows about agents, so other pages can adopt it as-is — hence the
 * generic `field.*` i18n namespace for this module's own strings.
 */
const RangeField = {

    // ---- formatting -------------------------------------------------------
    fmt: {
        /** Locale-aware, so Italian reads "0,7" without duplicating any copy. */
        num(v, digits = 2) {
            return Number(v).toLocaleString(I18n.getDateLocale(),
                                            { maximumFractionDigits: digits });
        },
        int(v) { return this.num(v, 0); },

        /** Seconds in the coarsest unit that stays exact ("90 s", "30 min",
         *  "24 h"): a heartbeat should read the way people say it. */
        seconds(v) {
            const n = Math.round(Number(v));
            if (n >= 3600 && n % 3600 === 0) return i18n('field.dur.h', { n: this.int(n / 3600) });
            if (n >= 60 && n % 60 === 0) return i18n('field.dur.min', { n: this.int(n / 60) });
            if (n >= 3600) return i18n('field.dur.h', { n: this.num(n / 3600, 1) });
            return i18n('field.dur.s', { n: this.int(n) });
        },

        /** DECIMAL k, unlike a context window (read in powers of two): a
         *  compaction budget is typed in thousands. */
        tokens(v) {
            const n = Number(v);
            return n >= 1000
                ? i18n('field.tok', { n: this.num(n / 1000, 1) })
                : i18n('field.tokRaw', { n: this.int(n) });
        },

        perHour(v) { return i18n('field.perHour', { n: this.int(v) }); },
    },

    // ---- value <-> slider index -------------------------------------------
    _steps(numEl) {
        try { return JSON.parse(numEl.dataset.steps || 'null'); } catch (e) { return null; }
    },

    /** Nearest step, used while TYPING: the thumb follows along without
     *  rewriting what is being typed. render() has already spliced the stored
     *  value into the step list, so the exact value stays reachable by dragging. */
    _nearest(steps, v) {
        let best = 0;
        for (let i = 1; i < steps.length; i++) {
            if (Math.abs(steps[i] - v) < Math.abs(steps[best] - v)) best = i;
        }
        return best;
    },

    // ---- render -----------------------------------------------------------
    /** HTML for one field. `value` is the STORED value: it may be null (a nullable
     *  field left to inherit), off-step, or outside spec.min/max. */
    render(spec, value, ctx = {}) {
        const id = spec.id;
        const unset = value === null || value === undefined || value === '';
        const on = !spec.nullable || !unset;          // nullable + set = switched on
        const v = unset ? (spec.nullable ? spec.fallback : spec.dflt) : Number(value);

        let steps = spec.steps ? spec.steps.slice() : null;
        let rMin, rMax, rStep, rVal;
        if (steps) {
            // Splice, don't snap: snapping would park the thumb on a neighbour and
            // LIE about the stored value. Spliced in, the thumb lands exactly on
            // it and dragging away and back is lossless.
            if (!steps.includes(v)) { steps.push(v); steps.sort((a, b) => a - b); }
            rMin = 0; rMax = steps.length - 1; rStep = 1; rVal = steps.indexOf(v);
        } else {
            rMin = Math.min(spec.min, v); rMax = Math.max(spec.max, v);
            rStep = spec.step || (spec.int ? 1 : 0.05); rVal = v;
        }
        // The same widening on the number input, or the browser refuses to submit.
        const numMin = Math.min(spec.min, v);
        const numMax = Math.max(spec.max, v);

        const hint = spec.hint ? ` <small class="text-secondary">${spec.hint}</small>` : '';
        const toggle = spec.nullable ? `
                <div class="form-check mb-1">
                    <input class="form-check-input" type="checkbox" id="${id}-on" ${on ? 'checked' : ''}>
                    <label class="form-check-label small" for="${id}-on">${spec.toggle}</label>
                </div>` : '';

        return `
            <div class="range-field${on ? '' : ' rf-off'}" id="rf-${id}">${toggle}
                <div class="rf-head">
                    <label class="form-label mb-0" id="${id}-lab" for="${id}">${spec.label}${hint}</label>
                    <output class="badge rf-value" id="${id}-out" for="${id}"></output>
                </div>
                <div class="rf-controls">
                    <input type="range" class="form-range rf-range" id="${id}-r"
                           min="${rMin}" max="${rMax}" step="${rStep}" value="${rVal}"
                           aria-labelledby="${id}-lab" ${on ? '' : 'disabled'}>
                    <input type="number" class="form-control form-control-sm rf-num" id="${id}"
                           value="${v}" min="${numMin}" max="${numMax}"
                           step="${spec.int ? 1 : 'any'}"
                           inputmode="${spec.int ? 'numeric' : 'decimal'}"
                           data-dflt="${spec.dflt}" ${spec.int ? 'data-int="1"' : ''}
                           ${spec.nullable ? 'data-nullable="1"' : ''}
                           ${steps ? `data-steps="${App.escAttr(JSON.stringify(steps))}"` : ''}
                           aria-describedby="${id}-desc" ${on ? '' : 'disabled'}>
                </div>
                <div class="rf-ends">
                    <span>${spec.minLabel || spec.fmt(steps ? steps[0] : rMin, ctx)}</span>
                    <span>${spec.maxLabel || spec.fmt(steps ? steps[steps.length - 1] : rMax, ctx)}</span>
                </div>
                <div class="form-text rf-desc" id="${id}-desc"></div>
                ${spec.help ? `
                <details class="rf-more">
                    <summary class="small text-secondary">${i18n('field.what')}</summary>
                    <div class="form-text mt-1">${spec.help}</div>
                </details>` : ''}
            </div>`;
    },

    // ---- wire -------------------------------------------------------------
    /** Listeners for a whole spec map, plus the first paint of every badge and
     *  sentence. `ctxFn()` must return a FRESH object with the cross-field values
     *  the sentences read: it is called on every refresh, so a sentence that
     *  mentions another field can never go stale.
     *
     *  `spec.deps` lists element ids whose changes refresh this field. They need
     *  not be range fields — a checkbox (f-memory) or a select (f-model) works,
     *  which is how "memory is off, so this threshold does nothing" is expressed
     *  with no extra API. */
    wireAll(specs, ctxFn = () => ({})) {
        const list = Object.values(specs);
        const refresh = (spec) => this.refresh(spec, ctxFn());

        const dependents = {};
        list.forEach(spec => (spec.deps || []).forEach(dep => {
            (dependents[dep] = dependents[dep] || []).push(spec);
        }));
        const touched = (id) => {
            if (specs[id]) refresh(specs[id]);
            (dependents[id] || []).forEach(refresh);
        };

        list.forEach(spec => {
            const num = document.getElementById(spec.id);
            const rng = document.getElementById(spec.id + '-r');
            const on = document.getElementById(spec.id + '-on');
            if (!num || !rng) return;
            const steps = this._steps(num);

            rng.addEventListener('input', () => {
                num.value = steps ? steps[+rng.value] : rng.value;
                touched(spec.id);
            });
            num.addEventListener('input', () => {
                const v = parseFloat(num.value);
                if (Number.isFinite(v)) rng.value = steps ? this._nearest(steps, v) : v;
                touched(spec.id);
            });
            if (on) on.addEventListener('change', () => {
                // Disabled, not cleared: unchecking means "inherit", not "forget
                // the value I chose".
                num.disabled = rng.disabled = !on.checked;
                document.getElementById('rf-' + spec.id)
                        .classList.toggle('rf-off', !on.checked);
                touched(spec.id);
            });
        });

        // Dependencies that are not range fields (checkbox, select).
        const external = new Set();
        list.forEach(spec => (spec.deps || []).forEach(d => { if (!specs[d]) external.add(d); }));
        external.forEach(dep => {
            const el = document.getElementById(dep);
            if (!el) return;
            ['input', 'change'].forEach(ev => el.addEventListener(ev, () => touched(dep)));
        });

        list.forEach(refresh);
    },

    /** Badge + track fill + sentence. Idempotent, cheap, safe to spam. */
    refresh(spec, ctx = {}) {
        const num = document.getElementById(spec.id);
        const rng = document.getElementById(spec.id + '-r');
        if (!num || !rng) return;
        const v = this.read(spec.id);
        const live = v !== null;

        const raw = live ? spec.describe(v, ctx)
                         : (spec.describeOff ? spec.describeOff(ctx) : '');
        const { text, warn, muted } = (typeof raw === 'string') ? { text: raw } : (raw || {});
        const desc = document.getElementById(spec.id + '-desc');
        if (desc) {
            desc.textContent = text || '';
            desc.classList.toggle('text-warning-emphasis', !!warn);
        }

        // A field can be irrelevant without being unset (memory_threshold while
        // memory is off): dim it, but never disable it — it is still saved.
        const dim = !!(spec.mutedWhen && spec.mutedWhen(ctx));
        const wrap = document.getElementById('rf-' + spec.id);
        if (wrap) wrap.classList.toggle('rf-idle', dim);

        const out = document.getElementById(spec.id + '-out');
        if (out) {
            out.textContent = live ? spec.fmt(v, ctx)
                                   : (spec.fmtOff ? spec.fmtOff(ctx) : '');
            const plain = !live || muted || dim || v === spec.sentinel;
            out.className = 'badge rf-value ' + (plain
                ? 'bg-secondary-subtle text-secondary-emphasis'
                : 'bg-primary-subtle text-primary-emphasis');
        }

        const span = (+rng.max) - (+rng.min) || 1;
        rng.style.setProperty('--rf-pct',
            (((+rng.value) - (+rng.min)) / span * 100).toFixed(2) + '%');
    },

    // ---- read -------------------------------------------------------------
    /** The field's value, or null for a nullable field left on "inherit".
     *
     *  The ONLY readback path. Note Number.isFinite instead of `|| dflt`: 0 is a
     *  legitimate value here (interval_s = 0 is "events only", temperature 0 is
     *  greedy decoding), and `parseFloat('0') || 0.7` is exactly the bug this
     *  replaces. */
    read(id) {
        const el = document.getElementById(id);
        if (!el) return null;
        const on = document.getElementById(id + '-on');
        if (on && !on.checked) return null;
        const raw = (el.value || '').trim();
        const dflt = el.dataset.dflt === undefined ? null : Number(el.dataset.dflt);
        if (raw === '') return el.dataset.nullable ? null : dflt;
        const n = el.dataset.int ? parseInt(raw, 10) : parseFloat(raw);
        return Number.isFinite(n) ? n : dflt;
    },
};
