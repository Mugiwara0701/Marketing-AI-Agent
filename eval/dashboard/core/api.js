// All data access lives here. Only getMe() talks to the backend so far; to use it for data, replace each
// function body below with an apiFetch() to the endpoint named in its comment. Nothing else in the app
// touches data.js.
import { pipelines, leads, sentEmails, replies, blogs } from './data.js';
import { POSITIVE_LABELS } from './format.js';
import { getSession } from './auth';

// ---------- Backend (eval/dashboardbackend) ----------
const API_URL = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; } // status 0 = backend unreachable
}

// Calls the Python backend with the Supabase access token as a Bearer token.
export async function apiFetch(path, options = {}) {
  const session = await getSession();
  let res;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...options,
      headers: { ...options.headers, ...(session && { Authorization: `Bearer ${session.access_token}` }) },
    });
  } catch {
    throw new ApiError('Cannot reach the server.', 0);
  }
  if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status);
  return res.json();
}

// GET /auth/me -> { id, email, role }; 401 when the token is invalid or expired
export const getMe = () => apiFetch('/auth/me');

const LATENCY = 350;
const wait = (ms = LATENCY) => new Promise((r) => setTimeout(r, ms));
const clone = (x) => JSON.parse(JSON.stringify(x));
const omit = (o, ...keys) => Object.fromEntries(Object.entries(o).filter(([k]) => !keys.includes(k)));

function find(list, id) {
  const item = list.find((x) => x.id === id);
  if (!item) throw new Error('Not found');
  return item;
}

// ---------- Pipelines ----------

// GET /pipeline
export async function getPipelines() {
  await wait();
  return clone(pipelines);
}

// POST /pipeline/start  { id }
export async function startPipeline(id) {
  await wait(200);
  find(pipelines, id).status = 'running';
  return clone(pipelines);
}

// POST /pipeline/pause  { id }
export async function pausePipeline(id) {
  await wait(200);
  find(pipelines, id).status = 'paused';
  return clone(pipelines);
}

// ---------- Leads ----------

// GET /leads?status=awaiting|responded&source=linkedin|x|other
export async function getLeads({ status, source } = {}) {
  await wait(500);
  return leads
    .filter((l) => (!status || l.status.toLowerCase() === status) && (!source || l.source === source))
    .map((l) => omit(clone(l), 'title', 'company', 'companyUrl', 'profileUrl', 'email', 'lastContactAt', 'emailId', 'replyId', 'notes'));
}

// GET /leads/stats  -> { total, awaiting, responded, bySource }
export async function getLeadStats() {
  await wait(400);
  const bySource = { linkedin: 0, x: 0, other: 0 };
  let awaiting = 0;
  let responded = 0;
  for (const l of leads) {
    bySource[l.source]++;
    if (l.status === 'Awaiting') awaiting++; else responded++;
  }
  return { total: leads.length, awaiting, responded, bySource };
}

// GET /leads/:id  -> lead with details and its linked email or reply
export async function getLead(id) {
  await wait(250);
  const l = clone(find(leads, id));
  l.conversation = l.replyId ? { type: 'reply', id: l.replyId } : l.emailId ? { type: 'sent', id: l.emailId } : null;
  l.replyLabel = replies.find((r) => r.id === l.replyId)?.label ?? null;
  return omit(l, 'emailId', 'replyId');
}

// GET /leads/positive (Supabase: replies where label = 'interested', joined to contacts, companies and emails)
export async function getPositiveLeads() {
  await wait(500);
  return replies
    .filter((r) => POSITIVE_LABELS.includes(r.label))
    .sort((a, b) => b.receivedAt.localeCompare(a.receivedAt))
    .map((r) => {
      const l = leads.find((x) => x.id === find(sentEmails, r.inReplyToId).leadId);
      const text = r.body.split('\n\n')[1]; // the reply text between the greeting and the sign-off
      return {
        id: l.id, number: l.number, name: l.name, company: l.company, source: l.source, status: l.status,
        replyId: r.id, repliedAt: r.receivedAt, label: r.label,
        preview: text.length > 100 ? `${text.slice(0, 99).trimEnd()}…` : text,
      };
    });
}

// ---------- Emails ----------

// GET /emails/sent
export async function getSentEmails() {
  await wait();
  return sentEmails.map((e) => omit(clone(e), 'body'));
}

// GET /emails/sent/:id
export async function getSentEmail(id) {
  await wait(250);
  return clone(find(sentEmails, id));
}

// GET /emails/replies
export async function getReplies() {
  await wait(500);
  return replies.map((r) => omit(clone(r), 'body'));
}

// GET /emails/replies/:id  (includes the original email it responds to)
export async function getReply(id) {
  await wait(250);
  const r = clone(find(replies, id));
  const o = find(sentEmails, r.inReplyToId);
  r.originalEmail = { id: o.id, number: o.number, to: o.to, subject: o.subject };
  return r;
}

// ---------- Blogs ----------

// GET /blogs
export async function getBlogs() {
  await wait(600);
  return blogs.map((b) => omit(clone(b), 'content'));
}

// GET /blogs/:id
export async function getBlog(id) {
  await wait(250);
  return clone(find(blogs, id));
}
