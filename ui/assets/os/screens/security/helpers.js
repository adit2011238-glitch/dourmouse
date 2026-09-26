/* Pure helpers for SECURITY, DOM-free so node can test them. Nothing here
   invents a number: each figure comes from /api/security_dashboard, the
   lockdown status or the analyst read, and a missing read says so. */

import { agoLabel, clock, plural } from '../../kit/format.js';

export const SEV_TAG = { high: 'bad', med: 'warn', low: '' };
export const SEV_WORD = { high: 'high', med: 'medium', low: 'low' };
export const RATING = {
  good: { tag: 'ok', word: 'good' },
  attention: { tag: 'warn', word: 'attention' },
  at_risk: { tag: 'bad', word: 'at risk' },
  unknown: { tag: '', word: 'unknown' },
};

const COLLECTORS = 9;

export function findings(d) {
  return d && Array.isArray(d.findings) ? d.findings : [];
}

/* The eight boxes of "How a finding is made". The description of each stage is
   fixed text; the second half of every sub line is a real read. The first four
   stages are detection: sentry.py and mac_detectors.py import no model client. */
export function flowBoxes(d, analyst, now = Date.now()) {
  const scanned = Boolean(d && d.scanned);
  const fs = findings(d);
  const tele = (d && d.telemetry_available) || {};
  const answered = Object.values(tele).filter(Boolean).length;
  const asked = Object.keys(tele).length;
  const corr = fs.filter((f) => String(f.kind || '').startsWith('correlated')).length;
  const inc = (d && d.incidents_by_status) || {};
  const withAction = fs.filter((f) => f.recommended_action).length;
  const a = analyst && analyst.analysis;
  return [
    { key: 'telemetry', tone: 'det', title: 'OS + network telemetry', sub: scanned ? plural(answered, 'collector') + ' answered of ' + (asked || COLLECTORS) : 'ss  lsof  arp  dns  firewall: read on each scan' },
    { key: 'analyzers', tone: 'det', title: 'Deterministic analyzers', sub: 'pure code, no model' },
    { key: 'baseline', tone: 'det', title: 'Baseline engine', sub: d ? plural(d.known_device_count || 0, 'known device') + ' remembered' : 'reading' },
    { key: 'events', tone: 'det', title: 'Event engine', sub: scanned ? plural(fs.length, 'finding') + ' on the last scan' : 'no scan has finished yet' },
    { key: 'analyst', tone: 'ai', title: 'AI analyst (optional)', sub: a && a.at ? 'last analysis ' + agoLabel(a.at, now) + ' (cloud model)' : 'no analysis yet (cloud model, off unless enabled)' },
    { key: 'correlation', tone: 'ai', title: 'Correlation', sub: scanned ? plural(corr, 'correlated finding') : 'weak signals seen together' },
    { key: 'dashboard', tone: 'ui', title: 'Dashboard', sub: d ? (inc.OPEN || 0) + ' open, ' + (inc.INVESTIGATING || 0) + ' investigating incidents' : 'reading' },
    { key: 'response', tone: 'act', title: 'Response', sub: scanned ? plural(withAction, 'suggestion') + ', then it asks you' : 'suggest, then ask you' },
  ];
}

export function flowLegend(d) {
  const c = (d && d.findings_by_severity) || {};
  return {
    title: 'severity on the last scan',
    items: [
      { label: 'high ' + (c.high || 0), color: 'var(--dm-error)' },
      { label: 'medium ' + (c.med || 0), color: 'var(--os-warn)' },
      { label: 'low ' + (c.low || 0), color: 'var(--dm-ok)' },
      { label: 'not checked: see posture', color: 'var(--dm-fg-dim)' },
    ],
  };
}

/* The "This Mac" card. Firewall and exposure are read off the findings, and a
   collector that did not answer reads "not checked", never "fine". */
export function macFacts(d) {
  if (!d || !d.scanned) return null;
  const fs = findings(d);
  const tele = d.telemetry_available || {};
  const fwOff = fs.some((f) => f.kind === 'firewall_disabled');
  const exposed = fs.filter((f) => f.kind === 'exposed_port').length;
  return {
    firewall: fwOff ? { word: 'off', tone: 'bad' } : tele.firewall ? { word: 'no problem found', tone: 'ok' } : { word: 'not checked', tone: '' },
    exposure: tele.listening_ports ? { word: plural(exposed, 'exposed port') + ' found', tone: exposed ? 'warn' : 'ok' } : { word: 'not checked', tone: '' },
    risk: Number.isFinite(Number(d.risk_score)) ? String(Math.round(Number(d.risk_score))) : '',
    newDevices: fs.filter((f) => f.is_new).length,
    devices: d.known_device_count || 0,
  };
}

export function macTag(d) {
  if (!d || !d.scanned) return { word: 'no scan', tone: '' };
  const c = d.findings_by_severity || {};
  if (c.high) return { word: 'attention', tone: 'bad' };
  if (c.med) return { word: 'watch', tone: 'warn' };
  return { word: 'last scan clean', tone: 'ok' };
}

export function lockdownLine(ld) {
  if (!ld) return 'Lockdown state unavailable';
  const apps = (ld.apps || []).length;
  const sites = (ld.sites || []).length + (ld.urls || []).length;
  return (ld.active ? 'Lockdown is ON' : 'Lockdown is off') + ': ' + plural(apps, 'app') + ' and ' + plural(sites, 'site') + ' on the blocklist';
}

export function lockPrompt(ld) {
  const apps = ((ld && ld.apps) || []).length;
  const sites = ((ld && ld.sites) || []).length;
  if (ld && ld.active) return 'Stop lockdown? The ' + plural(apps, 'blocked app') + ' and ' + plural(sites, 'blocked site') + ' are released.';
  return 'Start lockdown? ' + plural(apps, 'app') + ' on the blocklist will be closed whenever they open, and ' + plural(sites, 'site') + ' will be blocked in the hosts file by the root helper.'
    + (ld && ld.helper_installed === false ? ' The root helper is not installed, so site blocks are not enforced until you install it.' : '');
}

/* the directive REMEDIATE hands to HOME. Only code-made identifiers go in:
   the kind and the fingerprint are fixed tokens, while a title or detail can
   carry a process name or a host name an attacker chose. The model reads the
   finding itself through its own security tool. */
export function remediationDirective(f) {
  return 'Review the Mac security finding of kind ' + String(f.kind).replace(/[^a-z0-9_]/gi, '') + ' (fingerprint ' + String(f.fingerprint).replace(/[^a-f0-9]/gi, '') + '). '
    + 'Read it with your security tools, explain in plain words what it means, and propose the safest fix. Do not change anything until I approve it.';
}

/* one live-activity line from a security_* event, or null for a shape we do not know */
export function activityLine(e, now = Date.now()) {
  const at = e.at || now;
  if (e.type === 'security_scan') {
    const c = e.counts || {};
    const n = Array.isArray(e.new) ? e.new.length : 0;
    return { at, text: 'scan (' + (e.reason || 'tick') + '): ' + (c.high || 0) + ' high, ' + (c.med || 0) + ' medium, ' + (c.low || 0) + ' low, ' + n + ' new' };
  }
  if (e.type === 'security_network_change') {
    return { at, text: 'network changed: ' + (e.from || 'unknown') + ' to ' + (e.to || 'unknown') };
  }
  if (e.type === 'security_analysis') {
    const a = e.analysis || {};
    return { at, text: a.ok ? 'analyst: ' + String(a.summary || '').slice(0, 160) : 'analyst could not run' + (a.error ? ': ' + String(a.error).slice(0, 120) : '') };
  }
  if (e.type === 'security_download') {
    const a = e.assessment || {};
    return { at, text: 'download ' + (a.name || 'file') + ': risk ' + (a.risk || 'unknown') };
  }
  return null;
}

export function activityRow(item) {
  return (clock(item.at) || '--:--:--') + '  ' + item.text;
}
