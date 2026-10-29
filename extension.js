import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';

const REFRESH_SECONDS = 300;
const PROJECT_ROWS = 10;
const BAR_WIDTH = 156;
const BAR_HEIGHT = 6;
const QUOTA_HEIGHT = 8;

function prefersMyriad() {
    const lang = [
        GLib.getenv('LC_ALL'),
        GLib.getenv('LC_MESSAGES'),
        GLib.getenv('LANG'),
    ].filter(Boolean).join(':').toLowerCase();
    return lang.includes('zh');
}

function trimNumber(value) {
    return String(Math.round(value * 10) / 10).replace(/\.0$/, '');
}

function formatTokens(value) {
    const number = Math.max(0, Math.round(Number(value) || 0));
    if (number < 10000)
        return String(number);
    if (prefersMyriad()) {
        if (number < 100000000) {
            const scaled = number / 10000;
            const text = scaled < 100 ? trimNumber(scaled) : String(Math.round(scaled));
            return `${text}万`;
        }
        return `${trimNumber(number / 100000000)}亿`;
    }
    if (number < 1000000)
        return `${trimNumber(number / 1000)}K`;
    if (number < 1000000000)
        return `${trimNumber(number / 1000000)}M`;
    return `${trimNumber(number / 1000000000)}B`;
}

function formatRemain(iso) {
    const stamp = Date.parse(iso || '');
    if (!Number.isFinite(stamp))
        return '';
    let seconds = Math.round((stamp - Date.now()) / 1000);
    if (seconds <= 0)
        return _('Resets soon');
    const days = Math.floor(seconds / 86400);
    seconds -= days * 86400;
    const hours = Math.floor(seconds / 3600);
    seconds -= hours * 3600;
    const minutes = Math.floor(seconds / 60);
    if (days > 0 && hours > 0)
        return _('%d days %d hours').replace('%d', String(days)).replace('%d', String(hours));
    if (days > 0)
        return _('%d days').replace('%d', String(days));
    if (hours > 0 && minutes > 0)
        return _('%d hours %d minutes').replace('%d', String(hours)).replace('%d', String(minutes));
    if (hours > 0)
        return _('%d hours').replace('%d', String(hours));
    return _('%d minutes').replace('%d', String(Math.max(1, minutes)));
}

function formatAge(iso) {
    const stamp = Date.parse(iso || '');
    if (!Number.isFinite(stamp))
        return '';
    const minutes = Math.max(0, Math.round((Date.now() - stamp) / 60000));
    if (minutes < 1)
        return _('Just updated');
    if (minutes < 60)
        return _('%d minutes ago').replace('%d', String(minutes));
    return _('%d hours ago').replace('%d', String(Math.round(minutes / 60)));
}

function safeText(value) {
    return String(value || '').replace(/sk-[A-Za-z0-9_-]{6,}/g, 'sk-[redacted]').slice(0, 240);
}

function shortModel(name) {
    const parts = String(name || '').split('/').filter(Boolean);
    if (parts.length >= 3)
        return parts.slice(-2).join('/');
    return parts.join('/') || _('Unknown model');
}

function displayName(name) {
    if (name === 'subagent' || name === '子代理')
        return _('Subagent');
    if (name === 'unattributed' || name === '未归类')
        return _('Unattributed');
    return name;
}

function totalTokens(snapshot) {
    return (snapshot?.agents || []).reduce((sum, agent) => sum + (agent.totalTokens || 0), 0);
}

function collectedLimits(snapshot) {
    const rows = [];
    for (const agent of snapshot?.agents || []) {
        for (const limit of agent.limits || [])
            rows.push({agent, limit});
    }
    return rows;
}

function windowTitle(limit) {
    const seconds = Number(limit?.windowSeconds) || 0;
    const raw = String(limit?.label || '');
    let kind = '';
    if (seconds >= 20 * 86400 || raw.includes('月'))
        kind = _('Monthly limit');
    else if (seconds >= 6 * 86400 || raw.includes('周'))
        kind = _('Weekly limit');
    else if ((seconds > 0 && seconds <= 12 * 3600) || raw.includes('小时') || raw.includes('会话'))
        kind = _('5-hour limit');
    if (!kind)
        return '';
    const prefix = raw
        .replace(/（[^）]*）/g, '')
        .replace(/每周|每月|会话|5\s*小时|小时/g, '')
        .trim();
    return prefix ? `${prefix} ${kind}` : kind;
}

function formatWhen(iso) {
    const stamp = Date.parse(iso || '');
    if (!Number.isFinite(stamp))
        return '';
    const date = new Date(stamp);
    const now = new Date();
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    const clock = `${hh}:${mm}`;
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const that = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diff = Math.round((that - day) / 86400000);
    if (diff === 0)
        return _('Resets today at %s').replace('%s', clock);
    if (diff === 1)
        return _('Resets tomorrow at %s').replace('%s', clock);
    return _('Resets on %m/%d at %s')
        .replace('%m', String(date.getMonth() + 1))
        .replace('%d', String(date.getDate()))
        .replace('%s', clock);
}

function timedLimits(snapshot) {
    return collectedLimits(snapshot).filter(row => {
        return windowTitle(row.limit) && Number.isFinite(Date.parse(row.limit.resetsAt || ''));
    }).sort((a, b) => Date.parse(a.limit.resetsAt) - Date.parse(b.limit.resetsAt));
}

function barText(snapshot) {
    const row = timedLimits(snapshot)[0];
    if (!row)
        return _('Usage');
    const remain = formatRemain(row.limit.resetsAt);
    const title = windowTitle(row.limit);
    return remain ? `${title} ${remain}` : title;
}

function combine(snapshot, key) {
    const totals = new Map();
    for (const agent of snapshot?.agents || []) {
        for (const item of agent[key] || []) {
            const name = String(item.name || '');
            if (!name)
                continue;
            totals.set(name, (totals.get(name) || 0) + (item.tokens || 0));
        }
    }
    return [...totals.entries()]
        .filter(([, tokens]) => tokens > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([name, tokens]) => ({name, tokens}));
}

function vbox(styleClass) {
    return new St.BoxLayout({
        orientation: Clutter.Orientation.VERTICAL,
        style_class: styleClass || '',
        x_expand: true,
    });
}

function hbox(styleClass) {
    const box = new St.BoxLayout({
        orientation: Clutter.Orientation.HORIZONTAL,
        style_class: styleClass || '',
        x_expand: true,
        y_align: Clutter.ActorAlign.CENTER,
    });
    return box;
}

function ellipsize(label) {
    label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
    return label;
}

function pinSize(actor, width, height) {
    if (width != null) {
        actor.min_width = width;
        actor.natural_width = width;
        actor.min_width_set = true;
        actor.natural_width_set = true;
    }
    if (height != null) {
        actor.min_height = height;
        actor.natural_height = height;
        actor.min_height_set = true;
        actor.natural_height_set = true;
    }
}

const AgentUsageIndicator = GObject.registerClass(
class AgentUsageIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, _('Agent usage'), false);
        this._extension = extension;
        this._snapshot = null;
        this._resetLabels = [];
        this._layoutHooks = [];
        this._proc = null;
        this._pendingForce = false;
        this._destroyed = false;
        this._cancellable = new Gio.Cancellable();
        this._timer = 0;
        this._clock = 0;

        const bar = hbox('agent-usage-bar');
        bar.x_expand = false;
        bar.add_child(new St.Icon({
            icon_name: 'utilities-system-monitor-symbolic',
            style_class: 'system-status-icon',
        }));
        this._label = new St.Label({
            text: _('Counting…'),
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'agent-usage-text',
        });
        bar.add_child(this._label);
        this.add_child(bar);

        const section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(section);
        this._scroll = new St.ScrollView({
            style_class: 'agent-usage-scroll',
            overlay_scrollbars: true,
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
        });
        this._root = vbox('agent-usage-root');
        this._chrome = vbox('agent-chrome');
        this._body = vbox('agent-body');
        this._scroll.add_child(this._body);
        this._root.add_child(this._chrome);
        this._root.add_child(this._scroll);
        section.actor.add_child(this._root);

        this._loadCached();
        this._refresh(false);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECONDS, () => {
            this._refresh(false);
            return GLib.SOURCE_CONTINUE;
        });
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open) {
                this._relayout();
                this._startClock();
            } else {
                this._stopClock();
            }
        });
        this.connect('destroy', () => this._onCleanup());
    }

    _snapshotFile() {
        return Gio.File.new_for_path(GLib.build_filenamev([
            GLib.get_user_state_dir(),
            'gnome-agent-usage',
            'snapshot.json',
        ]));
    }

    _loadCached() {
        try {
            const [ok, bytes] = this._snapshotFile().load_contents(null);
            if (ok)
                this._apply(JSON.parse(new TextDecoder().decode(bytes)));
        } catch (_error) {
            // The first run has no snapshot yet. The collector fills it in.
        }
    }

    _refresh(force) {
        if (this._destroyed)
            return;
        if (this._proc) {
            this._pendingForce = this._pendingForce || force;
            return;
        }
        const python = GLib.find_program_in_path('python3');
        const script = GLib.build_filenamev([this._extension.path, 'collector', 'agent_usage.py']);
        if (!python || !Gio.File.new_for_path(script).query_exists(null)) {
            this._apply({agents: [], error: _('Collector script is missing'), updatedAt: new Date().toISOString(), tone: 'ok'});
            return;
        }
        const argv = [python, script, '--write'];
        if (force)
            argv.push('--force');
        try {
            this._proc = Gio.Subprocess.new(
                argv,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (error) {
            this._apply({agents: [], error: safeText(error), updatedAt: new Date().toISOString(), tone: 'ok'});
            return;
        }
        this._proc.communicate_utf8_async(null, this._cancellable, (proc, result) => {
            this._proc = null;
            if (this._destroyed)
                return;
            try {
                const [, stdout, stderr] = proc.communicate_utf8_finish(result);
                if (!proc.get_successful()) {
                    this._showError(stderr || _('Collection failed'));
                } else {
                    this._apply(JSON.parse(stdout));
                }
            } catch (error) {
                if (!error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    this._showError(error);
            }
            if (this._pendingForce && !this._destroyed) {
                this._pendingForce = false;
                this._refresh(true);
            }
        });
    }

    _showError(error) {
        const message = safeText(error);
        if (this._snapshot)
            this._snapshot.error = message;
        else
            this._snapshot = {agents: [], error: message, tone: 'ok', updatedAt: new Date().toISOString()};
        this._render();
    }

    _apply(snapshot) {
        if (!snapshot || !Array.isArray(snapshot.agents))
            return;
        this._snapshot = snapshot;
        this._render();
    }

    _render() {
        const snapshot = this._snapshot || {agents: [], tone: 'ok'};
        const soonest = timedLimits(snapshot)[0];
        this._stopClock();
        this._resetLabels = [];
        this._label.text = (snapshot.agents || []).length || !snapshot.error
            ? barText(snapshot)
            : _('Counting…');
        this._label.style_class = `agent-usage-text tone-${soonest?.limit.tone || snapshot.tone || 'ok'}`;
        const total = totalTokens(snapshot);
        this.accessible_name = soonest
            ? `${windowTitle(soonest.limit)} ${formatRemain(soonest.limit.resetsAt)} ${formatWhen(soonest.limit.resetsAt)}`
            : _('Total %s').replace('%s', formatTokens(total));
        this._layoutHooks = [];
        this._chrome.destroy_all_children();
        this._body.destroy_all_children();

        const header = hbox('agent-header');
        header.add_child(new St.Label({text: _('Agent usage'), style_class: 'agent-title', y_align: Clutter.ActorAlign.CENTER}));
        header.add_child(new St.Widget({x_expand: true}));
        const refresh = new St.Button({
            label: _('Refresh'),
            style_class: 'agent-refresh',
            can_focus: true,
        });
        refresh.connect('clicked', () => this._refresh(true));
        header.add_child(refresh);
        this._chrome.add_child(header);

        const agents = snapshot.agents || [];
        if (!agents.length) {
            const empty = new St.Label({
                text: safeText(snapshot.error) || _('No Claude, Codex, Grok, or OpenCode logs were found.'),
                style_class: 'agent-muted agent-note',
            });
            empty.clutter_text.line_wrap = true;
            this._chrome.add_child(empty);
        } else {
            const quotas = this._quotas(snapshot);
            if (quotas) {
                this._chrome.add_child(quotas);
                const rule = new St.Widget({style_class: 'agent-rule', x_expand: true});
                pinSize(rule, null, 1);
                this._chrome.add_child(rule);
            }
            const knowsProjects = agents.some(agent => Array.isArray(agent.projects));
            this._body.add_child(this._chart(_('Model usage'), combine(snapshot, 'models').map(item => ({
                name: shortModel(item.name),
                tokens: item.tokens,
            })), 0));
            this._body.add_child(this._projects(snapshot, total, knowsProjects));
        }

        if (snapshot.error && agents.length) {
            const error = new St.Label({
                text: safeText(snapshot.error),
                style_class: 'agent-muted agent-note',
            });
            error.clutter_text.line_wrap = true;
            this._body.add_child(error);
        }

        this._age = new St.Label({
            text: formatAge(snapshot.updatedAt),
            style_class: 'agent-muted agent-footer',
        });
        this._body.add_child(this._age);
        if (this.menu.isOpen) {
            this._relayout();
            this._startClock();
        }
    }

    _relayout() {
        for (const hook of this._layoutHooks)
            hook();
    }

    _quotas(snapshot) {
        const entries = timedLimits(snapshot);
        if (!entries.length)
            return null;
        const block = vbox('agent-quota-block');
        const titles = entries.map(entry => windowTitle(entry.limit));
        entries.forEach((entry, index) => {
            const {agent, limit} = entry;
            let title = titles[index];
            if (titles.filter(item => item === title).length > 1)
                title = `${agent.name} ${title}`;
            block.add_child(new St.Label({text: title, style_class: 'agent-kicker agent-quota-name'}));
            const head = hbox('agent-quota-head');
            const remain = new St.Label({
                text: formatRemain(limit.resetsAt),
                style_class: 'agent-total-value',
                y_align: Clutter.ActorAlign.END,
            });
            this._resetLabels.push([remain, agent, limit, 'remain']);
            head.add_child(remain);
            head.add_child(new St.Widget({x_expand: true}));
            head.add_child(new St.Label({
                text: `${Math.round((limit.percent || 0) * 100)}%`,
                style_class: `agent-quota-pct tone-${limit.tone || 'ok'}`,
                y_align: Clutter.ActorAlign.END,
            }));
            block.add_child(head);

            const ratio = Math.max(0, Math.min(1, limit.percent || 0));
            const track = new St.BoxLayout({
                style_class: 'agent-track agent-track-quota',
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            pinSize(track, 380, QUOTA_HEIGHT);
            const fill = new St.Widget({
                style_class: `agent-fill tone-${limit.tone || 'ok'}`,
                x_expand: false,
            });
            pinSize(fill, ratio > 0 ? Math.max(8, Math.round(380 * ratio)) : 0, QUOTA_HEIGHT);
            track.add_child(fill);
            track.add_child(new St.Widget({x_expand: true, style_class: 'agent-track-rest'}));
            const applyWidth = () => {
                const span = track.width || 380;
                pinSize(fill, ratio > 0 ? Math.max(8, Math.round(span * ratio)) : 0, QUOTA_HEIGHT);
            };
            track.connect('notify::width', applyWidth);
            this._layoutHooks.push(applyWidth);
            const barRow = hbox('agent-quota-bar');
            barRow.add_child(track);
            block.add_child(barRow);
            const when = new St.Label({
                text: this._whenText(agent, limit),
                style_class: 'agent-muted agent-quota-when',
            });
            this._resetLabels.push([when, agent, limit, 'when']);
            block.add_child(when);
        });
        return block;
    }

    _whenText(agent, limit) {
        const when = formatWhen(limit.resetsAt);
        if (agent.stale && when)
            return _('Earlier figures · %s').replace('%s', when);
        return when;
    }

    _projects(snapshot, total, knowsProjects) {
        if (!knowsProjects) {
            const block = vbox('agent-chart');
            block.add_child(new St.Label({text: _('Project usage'), style_class: 'agent-section'}));
            block.add_child(new St.Label({text: _('Organizing projects…'), style_class: 'agent-muted agent-note'}));
            return block;
        }
        const rows = combine(snapshot, 'projects');
        const listed = rows.reduce((sum, item) => sum + item.tokens, 0);
        const hidden = total - listed;
        if (hidden > Math.max(10000, total * 0.005))
            rows.push({name: 'unattributed', tokens: hidden});
        rows.sort((a, b) => b.tokens - a.tokens);
        return this._chart(_('Project usage'), rows, PROJECT_ROWS);
    }

    _chart(title, rows, limit) {
        const block = vbox('agent-chart');
        block.add_child(new St.Label({text: title, style_class: 'agent-section'}));
        if (!rows.length) {
            block.add_child(new St.Label({text: _('No records yet'), style_class: 'agent-muted agent-note'}));
            return block;
        }
        const shown = limit > 0 ? rows.slice(0, limit) : rows;
        const rest = limit > 0 ? rows.slice(limit) : [];
        const max = Math.max(...shown.map(item => item.tokens), 1);
        for (const item of shown)
            block.add_child(this._barRow(item.name, item.tokens, max));
        if (rest.length) {
            const tokens = rest.reduce((sum, item) => sum + item.tokens, 0);
            const more = title === _('Project usage')
                ? _('%d more projects · %s')
                : _('%d more models · %s');
            block.add_child(new St.Label({
                text: more.replace('%d', String(rest.length)).replace('%s', formatTokens(tokens)),
                style_class: 'agent-more',
            }));
        }
        return block;
    }

    _barRow(name, tokens, maxTokens) {
        const row = hbox('agent-row');
        row.reactive = true;
        row.track_hover = true;
        row.add_child(ellipsize(new St.Label({
            text: displayName(name),
            style_class: 'agent-row-name',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        })));
        const ratio = maxTokens > 0 ? Math.max(0, tokens / maxTokens) : 0;
        const fillWidth = tokens > 0 ? Math.max(6, Math.round(BAR_WIDTH * ratio)) : 0;
        const track = new St.BoxLayout({
            style_class: 'agent-track',
            x_expand: false,
            y_align: Clutter.ActorAlign.CENTER,
        });
        pinSize(track, BAR_WIDTH, BAR_HEIGHT);
        const fill = new St.Widget({style_class: 'agent-fill', x_expand: false});
        pinSize(fill, fillWidth, BAR_HEIGHT);
        track.add_child(fill);
        track.add_child(new St.Widget({x_expand: true, style_class: 'agent-track-rest'}));
        row.add_child(track);
        const value = new St.Label({
            text: formatTokens(tokens),
            style_class: 'agent-row-value',
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
        });
        pinSize(value, 72, null);
        row.add_child(value);
        return row;
    }

    _startClock() {
        this._stopClock();
        this._clock = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            for (const [label, agent, limit, kind] of this._resetLabels) {
                label.text = kind === 'when'
                    ? this._whenText(agent, limit)
                    : formatRemain(limit.resetsAt);
            }
            if (this._age && this._snapshot)
                this._age.text = formatAge(this._snapshot.updatedAt);
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopClock() {
        if (this._clock) {
            GLib.source_remove(this._clock);
            this._clock = 0;
        }
    }

    _onCleanup() {
        if (this._destroyed)
            return;
        this._destroyed = true;
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        this._stopClock();
        this._cancellable.cancel();
        if (this._proc) {
            this._proc.force_exit();
            this._proc = null;
        }
    }
});

export default class AgentUsageExtension extends Extension {
    enable() {
        this._indicator = new AgentUsageIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'right');
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}
