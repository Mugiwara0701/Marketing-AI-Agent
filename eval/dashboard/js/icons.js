const s = (d, w = 20) => `<svg width="${w}" height="${w}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

const logo = (d, w = 18) => `<svg width="${w}" height="${w}" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;

export const icon = {
  mail: s('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m3.5 7 8.5 6 8.5-6"/>'),
  doc: s('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>'),
  reply: s('<path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/>'),
  users: s('<path d="M16 20v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1"/><circle cx="9.5" cy="8" r="3.5"/><path d="M21 20v-1a4 4 0 0 0-3-3.9M15.5 4.2a3.5 3.5 0 0 1 0 7.6"/>'),
  clock: s('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  check: s('<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>'),
  bolt: s('<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>'),
  linkedin: logo('<rect width="24" height="24" rx="5" fill="#0a66c2"/><path fill="#fff" d="M6.9 9.5h2.4V17H6.9zM8.1 5.9a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8zM10.9 9.5h2.3v1c.4-.7 1.2-1.2 2.4-1.2 2.3 0 2.8 1.5 2.8 3.5V17h-2.4v-3.7c0-.9 0-2-1.2-2s-1.4.9-1.4 1.9V17h-2.4z"/>'),
  x: logo('<path fill="currentColor" d="M18.2 2.25h3.3l-7.2 8.26 8.5 11.24h-6.65l-5.2-6.82-5.97 6.82H1.68l7.73-8.84L1.25 2.25h6.83l4.71 6.23zm-1.16 17.52h1.83L7.08 4.13H5.12z"/>'),
  other: logo('<circle cx="12" cy="12" r="5" fill="#b8bccb"/>'),
  WordPress: logo('<circle cx="12" cy="12" r="11" fill="#21759b"/><text x="12" y="16.5" text-anchor="middle" font-size="12" font-weight="700" font-family="Georgia,serif" fill="#fff">W</text>'),
  Medium: logo('<circle cx="7.5" cy="12" r="5.5" fill="currentColor"/><ellipse cx="16.5" cy="12" rx="2.8" ry="5.2" fill="currentColor"/><ellipse cx="21.5" cy="12" rx="1" ry="4.6" fill="currentColor"/>'),
  'Dev.to': logo('<rect width="24" height="24" rx="5" fill="currentColor"/><text x="12" y="15" text-anchor="middle" font-size="8" font-weight="800" font-family="Inter,sans-serif" style="fill:var(--card)">DEV</text>'),
  sun: s('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>', 18),
  moon: s('<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>', 18),
  eye: s('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>', 18),
  chevL: s('<path d="m15 6-6 6 6 6"/>', 16),
  chevR: s('<path d="m9 6 6 6-6 6"/>', 16),
  send: s('<path d="M21 3 10 14M21 3l-7 18-4-7-7-4z"/>'),
  chat: s('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>'),
  play: s('<path d="M7 5v14l12-7z" fill="currentColor"/>', 16),
  pause: s('<path d="M8 5v14M16 5v14" stroke-width="2.6"/>', 16),
  arrow: s('<path d="M5 12h14M13 6l6 6-6 6"/>', 16),
  left: s('<path d="M19 12H5M11 6l-6 6 6 6"/>', 16),
  close: s('<path d="M6 6l12 12M18 6 6 18"/>'),
  ext: s('<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>', 16),
  alert: s('<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>', 24),
};
