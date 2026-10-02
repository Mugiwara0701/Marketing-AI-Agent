// Small formatting helpers shared by every page and the drawer.
// Reply labels (from outreach.classify_reply / replies.label) that count as a positive lead.
export const POSITIVE_LABELS = ['interested'];

export const pad = (n) => String(n).padStart(3, '0');

// mode: 'full' "Apr 20, 2024, 10:15 AM" | 'table' "Apr 20, 2024 10:15 AM" | 'date' "Apr 20, 2024"
export function fmtDate(iso, mode = 'full') {
  if (!iso) return '—';
  const d = new Date(iso);
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  if (mode === 'date') return date;
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });
  return mode === 'table' ? `${date} ${time}` : `${date}, ${time}`;
}

export const toneOf = (t) => ({ Running: 'green', Delivered: 'green', Replied: 'green', Posted: 'green', Responded: 'green', Interested: 'green', Paused: 'amber', Awaiting: 'amber', 'Not Posted': 'amber', Failed: 'red' }[t] || 'gray');

export const SOURCES = {
  linkedin: { label: 'LinkedIn', icon: 'linkedin', color: '#2f6fed' },
  x: { label: 'X', icon: 'x', color: 'var(--src-x)' },
  other: { label: 'Other', icon: 'other', color: '#b8bccb' },
};
