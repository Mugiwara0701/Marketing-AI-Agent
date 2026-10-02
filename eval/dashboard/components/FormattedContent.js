import { Fragment } from 'react';

// Auto-link URLs and emails inside a line of plain text.
function linkify(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(/(https?:\/\/[^\s<]+)|(?<=^|[\s(])([\w.+-]+@[\w-]+\.[\w.-]+)/g)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1]
      ? <a key={m.index} href={m[1]} target="_blank" rel="noopener noreferrer">{m[1]}</a>
      : <a key={m.index} href={`mailto:${m[2]}`}>{m[2]}</a>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const lines = (ls) => ls.map((l, i) => <Fragment key={i}>{i > 0 && <br />}{linkify(l)}</Fragment>);

// Minimal formatter for plain-text bodies: "## " / "### " headings, "- " and "1. " lists, "> " quotes,
// paragraphs, auto-linked URLs and emails.
export function FormattedContent({ text }) {
  return text.split(/\n{2,}/).map((block, i) => {
    const ls = block.split('\n');
    if (ls.every((l) => l.startsWith('- '))) return <ul key={i}>{ls.map((l, j) => <li key={j}>{linkify(l.slice(2))}</li>)}</ul>;
    if (ls.every((l) => /^\d+\. /.test(l))) return <ol key={i}>{ls.map((l, j) => <li key={j}>{linkify(l.replace(/^\d+\. /, ''))}</li>)}</ol>;
    if (ls.every((l) => l.startsWith('> '))) return <blockquote key={i}>{lines(ls.map((l) => l.slice(2)))}</blockquote>;
    if (block.startsWith('### ')) return <h5 key={i}>{block.slice(4)}</h5>;
    if (block.startsWith('## ')) return <h4 key={i}>{block.slice(3)}</h4>;
    return <p key={i}>{lines(ls)}</p>;
  });
}
