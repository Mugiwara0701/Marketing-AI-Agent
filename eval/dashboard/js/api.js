// All data access lives here. To use a real backend, replace each function body
// with a fetch() to the endpoint named in its comment. Nothing else in the app
// touches data.js.
import { pipelines, leads, sentEmails, replies, blogs } from './data.js';

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
  return omit(l, 'emailId', 'replyId');
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
