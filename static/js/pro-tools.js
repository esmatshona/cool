/* pro-tools.js — UI for the four advanced features.
 *
 * Every value rendered here comes from a real measurement taken by the backend
 * (pro_features.py). Nothing is placeholder or simulated: when a measurement is
 * unavailable the UI says so instead of showing a plausible-looking number.
 */
(function () {
  'use strict';

  const api = (p, o) => (window.STANNG && STANNG.api ? STANNG.api(p, o) : fetch(p, o).then(r => r.json()));
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const T = (k, d) => (window.STANNG && STANNG.t ? (STANNG.t(k) || d) : d);

  const GRADE_CLASS = {
    excellent: 'ok', good: 'ok', fair: 'warn', slow: 'warn',
    poor: 'bad', offline: 'bad',
  };
  const LEVEL_CLASS = {
    ok: 'ok', watch: 'warn', warning: 'warn', critical: 'bad',
    exhausted: 'bad', unlimited: 'ok', unknown: 'muted',
  };

  function badge(text, cls) {
    return `<span class="badge-pill ${cls || ''}">${esc(text)}</span>`;
  }
  function num(v, unit, digits) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toFixed(digits === undefined ? 1 : digits) + (unit || '');
  }
  function gb(bytes) {
    if (bytes === null || bytes === undefined) return '—';
    const v = Number(bytes) / 1073741824;
    if (!isFinite(v)) return '—';
    return (v >= 100 ? v.toFixed(0) : v.toFixed(2)) + ' GB';
  }
  function ago(sec) {
    if (sec === null || sec === undefined) return '—';
    sec = Math.max(0, Math.floor(sec));
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
    return Math.floor(sec / 86400) + 'd';
  }
  function dt(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return '—'; }
  }

  // ---------------------------------------------------------------- overview
  async function loadPro() {
    let d;
    try { d = await api('/api/pro/overview'); } catch (e) { return; }
    if (!d || !d.ok) return;

    const c = d.connections || {};
    if ($('proOnline')) $('proOnline').textContent = c.online_users || 0;
    if ($('proIps')) $('proIps').textContent = c.unique_ips || 0;
    if ($('proBest')) {
      const b = d.best_server;
      $('proBest').textContent = b ? (b.name || b.host) + (b.latency_ms != null ? ' · ' + b.latency_ms + 'ms' : '')
        : '—';
    }
    if ($('proAtRisk')) $('proAtRisk').textContent = (d.quota && d.quota.at_risk) ? d.quota.at_risk.length : 0;

    renderServers(d.servers || []);
    if (!window.__proInit) { window.__proInit = true; }
  }

  function renderServers(servers) {
    const tb = $('proServersBody');
    if (!tb) return;
    if (!servers.length) {
      tb.innerHTML = `<tr><td colspan="10" class="small" style="text-align:center;opacity:.6;">${T('pro_not_measured', 'هنوز سنجشی انجام نشده است.')}</td></tr>`;
      return;
    }
    tb.innerHTML = servers.map((s, i) => {
      const g = s.grade || (s.latency_ms == null ? 'offline' : '');
      const speed = s.download_mbps != null ? num(s.download_mbps, ' Mbps', 0) : '—';
      return `<tr>
        <td>${i + 1}</td>
        <td><b>${esc(s.name || '—')}</b>${s.local ? ' ' + badge('local', 'muted') : ''}</td>
        <td class="small">${esc(s.host || '—')}</td>
        <td>${g ? badge(g, GRADE_CLASS[g]) : '—'}</td>
        <td>${num(s.latency_ms, ' ms')}</td>
        <td class="small">${num(s.min_ms, '')} / ${num(s.max_ms, '')}</td>
        <td>${num(s.jitter_ms, ' ms')}</td>
        <td>${num(s.loss_percent, '%')}</td>
        <td>${speed}</td>
        <td>${s.score != null && s.score < 99999 ? num(s.score, '', 1) : '—'}</td>
      </tr>`;
    }).join('');
  }

  // ---------------------------------------------------------------- benchmark
  async function runBenchmark() {
    const btn = $('proBenchBtn');
    if (btn) { btn.disabled = true; btn.dataset.old = btn.textContent; btn.textContent = T('pro_running', 'در حال سنجش…'); }
    try {
      const samples = parseInt(($('proSamples') || {}).value || '3', 10);
      const withSpeed = !!($('proWithSpeed') && $('proWithSpeed').checked);
      const d = await api('/api/pro/servers/benchmark', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ samples: samples, include_speed: withSpeed }),
      });
      renderServers((d && d.results) || []);
      if (window.STANNG && STANNG.toast) {
        STANNG.toast(d && d.count
          ? T('pro_bench_done', 'سنجش انجام شد') + ': ' + d.count
          : T('pro_no_targets', 'نودی برای سنجش پیدا نشد (دامنه یا سرور تنظیم کنید)'));
      }
    } catch (e) {
      if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_bench_fail', 'سنجش ناموفق بود'));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.old || T('pro_run_bench', 'اجرای سنجش'); }
      loadPro();
    }
  }

  async function autoPick() {
    const d = await api('/api/pro/servers/auto-pick');
    if (!d || !d.ok) {
      if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_need_bench', 'ابتدا سنجش را اجرا کنید'));
      return;
    }
    const b = d.best || {};
    if (window.STANNG && STANNG.toast) {
      STANNG.toast(T('pro_best_is', 'بهترین نود') + ': ' + (b.name || b.host) +
        ' · ' + num(b.latency_ms, 'ms') + (d.stale ? ' (' + T('pro_stale', 'قدیمی') + ')' : ''));
    }
  }

  // ---------------------------------------------------- live connections
  async function loadConnections() {
    let d;
    try { d = await api('/api/pro/connections/live'); } catch (e) { return; }
    if (!d || !d.ok) return;

    if ($('proOnline')) $('proOnline').textContent = d.online_users || 0;
    if ($('proIps')) $('proIps').textContent = d.unique_ips || 0;

    const tb = $('proConnBody');
    if (!tb) return;
    const q = (($('proConnSearch') || {}).value || '').trim().toLowerCase();

    let users = (d.users || []).filter(u => {
      if (!q) return true;
      const hay = (u.name || '') + ' ' + (u.ips || []).map(i => i.ip).join(' ');
      return hay.toLowerCase().includes(q);
    });
    // online first, then most recent
    users.sort((a, b) => (b.online ? 1 : 0) - (a.online ? 1 : 0) || (b.last_seen || 0) - (a.last_seen || 0));

    if (!users.length) {
      tb.innerHTML = `<tr><td colspan="6" class="small" style="text-align:center;opacity:.6;">${
        d.log_available ? T('pro_no_conn', 'اتصالی ثبت نشده') : T('pro_no_log', 'لاگ دسترسی Xray در دسترس نیست')}</td></tr>`;
      return;
    }

    tb.innerHTML = users.map(u => {
      const ips = (u.ips && u.ips.length)
        ? u.ips.slice(0, 5).map(i => `
            <div class="small flex-row gap-4" style="align-items:center;justify-content:space-between;">
              <span>
                ${i.active ? '<span class="pill-dot" style="color:var(--emerald)"></span>' : '<span class="pill-dot" style="color:var(--text-faint,#888)"></span>'}
                <code>${esc(i.ip)}</code>
                <span style="opacity:.6">×${i.connections}</span>
                ${i.blocked ? badge(T('pro_blocked', 'مسدود'), 'bad') : ''}
              </span>
              <button class="btn btn-ghost btn-xs pro-ip-btn"
                      data-uid="${esc(u.uid)}" data-ip="${esc(i.ip)}"
                      data-action="${i.blocked ? 'unblock' : 'block'}">
                ${i.blocked ? T('pro_unblock', 'رفع مسدودی') : T('pro_block', 'مسدود')}
              </button>
            </div>`).join('')
        : '<span class="small" style="opacity:.5;">—</span>';

      return `<tr>
        <td><b>${esc(u.name || u.uid)}</b>${u.enabled ? '' : ' ' + badge(T('pro_disabled', 'غیرفعال'), 'muted')}</td>
        <td>${u.online ? badge(T('pro_online', 'آنلاین'), 'ok') : badge(T('pro_offline', 'آفلاین'), 'muted')}</td>
        <td>${ips}</td>
        <td>${u.connections || 0}</td>
        <td>${u.last_seen ? ago(u.last_seen_ago) + ' ' + T('pro_ago', 'پیش') : '—'}</td>
        <td class="small">${u.used_gb != null ? num(u.used_gb, ' GB', 2) + ' / ' + num(u.quota_gb, ' GB', 0) : '—'}</td>
      </tr>`;
    }).join('');

    tb.querySelectorAll('.pro-ip-btn').forEach(b => {
      b.addEventListener('click', () => toggleIp(b.dataset.uid, b.dataset.ip, b.dataset.action));
    });
  }

  async function toggleIp(uid, ip, action) {
    try {
      await api('/api/pro/connections/block', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: uid, ip: ip, action: action }),
      });
      if (window.STANNG && STANNG.toast) {
        STANNG.toast(action === 'block'
          ? T('pro_blocked_ok', 'آی‌پی مسدود شد')
          : T('pro_unblocked_ok', 'مسدودی برداشته شد'));
      }
    } catch (e) {
      if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_block_fail', 'عملیات ناموفق بود'));
    }
    loadConnections();
  }

  // ---------------------------------------------------------- speedtest
  async function runSpeedtest() {
    const btn = $('proSpeedBtn');
    if (btn) { btn.disabled = true; btn.dataset.old = btn.textContent; btn.textContent = T('pro_running', 'در حال اجرا…'); }
    try {
      const duration = parseFloat(($('proSpeedDur') || {}).value || '8');
      await api('/api/pro/speedtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: duration }),
      });
      await loadSpeedHistory();
      if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_speed_done', 'تست سرعت انجام شد'));
    } catch (e) {
      if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_speed_fail', 'تست سرعت ناموفق بود'));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.old || T('pro_run_speed', 'شروع تست'); }
    }
  }

  async function loadSpeedHistory() {
    let d;
    try { d = await api('/api/pro/speedtest/results'); } catch (e) { return; }
    const tb = $('proSpeedBody');
    if (!tb) return;
    const nodes = (d && d.nodes) || [];
    if (!nodes.length) {
      tb.innerHTML = `<tr><td colspan="7" class="small" style="text-align:center;opacity:.6;">${T('pro_no_speed', 'هنوز تست سرعتی اجرا نشده است.')}</td></tr>`;
      return;
    }
    tb.innerHTML = nodes.map((n, i) => `<tr>
      <td>${i + 1}</td>
      <td><b>${esc(n.name || '—')}</b></td>
      <td class="small">${esc(n.host || '—')}</td>
      <td>${n.latest_mbps != null ? num(n.latest_mbps, ' Mbps', 0) : '—'}</td>
      <td>${n.best_mbps != null ? num(n.best_mbps, ' Mbps', 0) : '—'}</td>
      <td>${n.runs || 0}</td>
      <td class="small">${dt(n.latest_at)}</td>
    </tr>`).join('');
  }

  // ------------------------------------------------------ quota forecast
  async function loadQuota() {
    let d;
    try { d = await api('/api/pro/quota/predictions'); } catch (e) { return; }
    const tb = $('proQuotaBody');
    if (!tb) return;
    if (!d || !d.ok) return;

    if ($('proAtRisk')) $('proAtRisk').textContent = d.at_risk || 0;

    const onlyRisk = !!($('proQuotaOnlyRisk') && $('proQuotaOnlyRisk').checked);
    let rows = d.predictions || [];
    if (onlyRisk) rows = rows.filter(p => ['critical', 'warning', 'watch'].includes(p.level));

    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8" class="small" style="text-align:center;opacity:.6;">${
        onlyRisk ? T('pro_no_risk', 'کاربر پرخطری وجود ندارد') : T('pro_no_data', 'داده‌ای موجود نیست')}</td></tr>`;
      return;
    }

    tb.innerHTML = rows.map(p => `<tr>
      <td><b>${esc(p.name || p.uid)}</b></td>
      <td>${badge(p.level, LEVEL_CLASS[p.level])}</td>
      <td>${num(p.used_percent, '%', 1)} <span class="small" style="opacity:.6">(${gb(p.used_bytes)})</span></td>
      <td>${gb(p.remaining_bytes)}</td>
      <td>${p.burn_bytes_per_day != null ? gb(p.burn_bytes_per_day) + '/' + T('pro_day', 'روز') : '—'}</td>
      <td>${p.days_left != null ? num(p.days_left, ' ' + T('pro_day', 'روز'), 1) : '—'}</td>
      <td class="small">${p.exhaust_at ? dt(p.exhaust_at) : '—'}</td>
      <td>${badge(p.confidence, p.confidence === 'high' ? 'ok' : (p.confidence === 'medium' ? 'warn' : 'muted'))}</td>
    </tr>`).join('');
  }

  async function takeSnapshot() {
    await api('/api/pro/quota/snapshot', { method: 'POST' });
    if (window.STANNG && STANNG.toast) STANNG.toast(T('pro_snap_ok', 'نمونه مصرف ثبت شد'));
    loadQuota();
  }

  // ------------------------------------------------------------------ wiring
  function init() {
    const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
    on('proBenchBtn', 'click', runBenchmark);
    on('proAutoPickBtn', 'click', autoPick);
    on('proConnRefresh', 'click', loadConnections);
    on('proConnSearch', 'input', () => { clearTimeout(window.__proSearchT); window.__proSearchT = setTimeout(loadConnections, 200); });
    on('proSpeedBtn', 'click', runSpeedtest);
    on('proQuotaRefresh', 'click', loadQuota);
    on('proQuotaSnap', 'click', takeSnapshot);
    on('proQuotaOnlyRisk', 'change', loadQuota);
  }

  window.PROTOOLS = {
    load: function () {
      loadPro();
      loadConnections();
      loadSpeedHistory();
      loadQuota();
    },
    loadPro: loadPro,
    loadConnections: loadConnections,
    loadSpeedHistory: loadSpeedHistory,
    loadQuota: loadQuota,
    runBenchmark: runBenchmark,
    runSpeedtest: runSpeedtest,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
