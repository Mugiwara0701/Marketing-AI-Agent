// Dummy data. Replaced by real API responses once api.js points at a backend.
// Everything below is generated deterministically so counts always add up
// (248 leads = 144 LinkedIn + 60 X + 44 Other = 152 Responded + 96 Awaiting).

export const pipelines = [
  { id: 'email', name: 'Email Outreach', description: 'Send personalized emails to new leads.', status: 'running' },
  { id: 'blog', name: 'Blog Publishing', description: 'Publish and distribute blog content.', status: 'paused' },
];

// ---------- helpers ----------
function rng(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function shuffle(arr, rand) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}
const pad3 = (n) => String(n).padStart(3, '0');
const hex3 = (prefix, n) => Math.floor(rng(n * 977 + prefix.length * 131 + prefix.charCodeAt(0))() * 4096).toString(16).padStart(3, '0');
const idOf = (prefix, n, fixed) => `${prefix}_${pad3(n)}${fixed || hex3(prefix, n)}`;
const blogId = (n) => `blog_${pad3(n)}`;
const HOUR = 3600000;
const DAY = 24 * HOUR;
const at = (s) => new Date(`${s}Z`).getTime();
const iso = (ms) => new Date(ms).toISOString();

// ---------- leads ----------
const firstNames = ['Sarah', 'Miguel', 'Tom', 'Luca', 'Emma', 'Chen', 'Olivia', 'Noah', 'Hana', 'Ravi', 'Grace',
  'Sofia', 'Elena', 'Omar', 'Julia', 'Kenji', 'Amara', 'Lucas', 'Nina', 'Ethan', 'Zara', 'Felix', 'Mei', 'Arjun', 'Clara', 'Diego', 'Lena', 'Victor', 'Maya', 'Ivan', 'Rosa', 'Theo'];
const lastNames = ['Santos', 'Nair', 'Becker', 'Romano', 'Wilson', 'Brown', 'Davis', 'Sato', 'Patel', 'Miller',
  'Garcia', 'Johnson', 'Petrov', 'Hassan', 'Novak', 'Tanaka', 'Okafor', 'Martin', 'Berg', 'Clarke', 'Ali', 'Meyer', 'Lin', 'Dubois', 'Alvarez', 'Fischer', 'Moreau', 'Reid', 'Cole', 'Ward', 'Hughes', 'Park'];
const domains = ['brightwave.io', 'northpeak.co', 'lumenlabs.com', 'harborstack.dev', 'oakridge.org', 'pixelforge.io', 'greenleaf.co', 'orbitly.com',
  'quantafy.ai', 'sunrise.agency', 'kintsugi.design', 'coreloop.io', 'bluepine.com', 'vectorly.co', 'stackwell.io', 'paperplane.app', 'fernhill.co',
  'novaworks.dev', 'cascadehq.com', 'tidalstream.io', 'ironleaf.co', 'mapleshift.com', 'zenithlabs.ai', 'copperline.io', 'skybridge.co', 'redwoodlabs.com',
  'sparkloop.io', 'driftwood.agency', 'atlasgrid.com', 'solaris.dev', 'beaconhq.io', 'quillpad.co', 'ridgeline.io', 'wavefront.ai', 'foxglove.co', 'granitepeak.com',
  'lanternworks.io', 'openfield.co', 'hexagon.studio', 'saltmarsh.dev'];
const titles = ['Head of Marketing', 'Content Manager', 'VP of Growth', 'Founder & CEO', 'Marketing Director', 'Demand Generation Lead',
  'Growth Marketer', 'Chief Marketing Officer', 'Brand Manager', 'Product Marketing Manager', 'Director of Partnerships', 'Head of Content',
  'Digital Marketing Manager', 'Co-founder'];

// Hand-picked leads (these also own the first sent emails and the 5 replies).
// number -> [name, source, status, jobTitle, company, email, addedAt]
const FIXED_LEADS = {
  12: ['Priya Sharma', 'linkedin', 'Awaiting', 'Head of Content', 'CloudScale', 'priya@cloudscale.dev', '2024-04-22T09:12:00'],
  13: ['Daniel Kim', 'x', 'Awaiting', 'Product Marketing Manager', 'TechFlow Studio', 'daniel@techflowstudio.com', '2024-04-21T14:05:00'],
  14: ['Aisha Khan', 'linkedin', 'Responded', 'Growth Marketing Lead', 'North Star Media', 'aisha@northstarmedia.co', '2024-04-20T10:24:00'],
  15: ['Marcus Lee', 'other', 'Awaiting', 'Founder & CEO', 'BuildIt Labs', 'marcus@builditlabs.io', '2024-04-19T16:40:00'],
  16: ['Sophia Chen', 'linkedin', 'Responded', 'Marketing Director', 'DevHub', 'sophia@devhub.co', '2024-04-18T08:50:00'],
  17: ['Li Wei', 'x', 'Responded', 'Digital Marketing Manager', 'Spark Digital', 'li@sparkdigital.io', '2024-04-17T13:30:00'],
};
const FIXED_IDS = { 14: '9bd' };

const rand = rng(7);
const sourcePool = shuffle([...Array(144).fill('linkedin'), ...Array(60).fill('x'), ...Array(44).fill('other')], rand);
const statusPool = shuffle([...Array(152).fill('Responded'), ...Array(96).fill('Awaiting')], rand);
// Force the hand-picked leads, then repair the pools so the totals stay exact.
for (const [n, f] of Object.entries(FIXED_LEADS)) {
  const i = n - 1;
  const j = sourcePool.findIndex((s, k) => s === f[1] && !(k + 1 in FIXED_LEADS));
  if (sourcePool[i] !== f[1]) [sourcePool[i], sourcePool[j]] = [sourcePool[j], sourcePool[i]];
  const m = statusPool.findIndex((s, k) => s === f[2] && !(k + 1 in FIXED_LEADS));
  if (statusPool[i] !== f[2]) [statusPool[i], statusPool[m]] = [statusPool[m], statusPool[i]];
}

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '');
const NOTES = {
  Responded: ['Replied positively and asked for more details about pricing and timeline.', 'Interested in a short intro call. Wants to see example posts first.', 'Forwarded our email to the marketing team for review.'],
  Awaiting: ['Initial outreach sent. No reply yet. Follow up in a few days.', 'Opened our email but has not replied. Good fit for the content package.', 'Outreach queued. Waiting for a response.'],
};

export const leads = Array.from({ length: 248 }, (_, i) => {
  const n = i + 1;
  const f = FIXED_LEADS[n];
  const first = f ? f[0].split(' ')[0] : firstNames[i % 32];
  const last = f ? f[0].split(' ').slice(1).join(' ') : lastNames[(5 * (i % 32) + Math.floor(i / 32)) % 32];
  const domain = domains[(i * 7) % domains.length];
  const co = f ? f[4] : domain.split('.')[0].replace(/^./, (c) => c.toUpperCase());
  const source = sourcePool[i];
  const status = statusPool[i];
  const name = f ? f[0] : `${first} ${last}`;
  const profileUrl = { linkedin: `https://www.linkedin.com/in/${slug(first)}-${slug(last)}`, x: `https://x.com/${slug(first)}${slug(last)}`, other: `https://github.com/${slug(first)}${slug(last)}` }[source];
  // Newest first: lead 12 = Apr 22, 2024, then one lead per day going back.
  const addedAt = f ? at(f[6]) : at('2024-04-22T09:00:00') - (n - 12) * DAY + ((n * 37) % 540) * 60000;
  return {
    id: idOf('lead', n, FIXED_IDS[n]), number: n, name, title: f ? f[3] : titles[(i * 3) % titles.length], company: co,
    companyUrl: `https://${f ? f[5].split('@')[1] : domain}`,
    source, profileUrl, email: f ? f[5] : `${slug(first)}.${slug(last)}@${domain}`, status,
    addedAt: iso(addedAt), lastContactAt: null, emailId: null, replyId: null,
    notes: n === 14
      ? 'Interested in our content marketing services. Replied positively and asked for more details about pricing and timeline.'
      : NOTES[status][n % 3],
  };
});

// ---------- sent emails ----------
const FROM = 'you@youragency.com';
// Hand-written first rows: [lead number, subject, sentAt]
const FIXED_EMAILS = [
  [14, 'Introducing our content marketing services', '2024-04-20T10:15:00'],
  [13, 'Quick question', '2024-04-19T15:42:00'],
  [12, 'Collaboration opportunity', '2024-04-18T11:06:00'],
  [15, 'Resources for you', '2024-04-17T16:20:00'],
  [16, "Let's connect", '2024-04-16T09:14:00'],
  [17, 'Partnership idea', '2024-04-15T14:30:00'],
];
const subjects = ['Collaboration opportunity', 'Quick question about your content strategy', 'Idea for your Q4 campaign', 'Partnership proposal',
  'Following up on my last note', 'Helping {co} grow organic traffic', 'Resources for you', "Let's connect"];
const fixedLeadNums = FIXED_EMAILS.map((e) => e[0]);
const emailLeadOrder = [...fixedLeadNums, ...leads.map((l) => l.number).filter((n) => !fixedLeadNums.includes(n))].slice(0, 120);

function emailBody(first, co) {
  return `Hi ${first},

I hope you're doing well! I came across ${co} and was really impressed by the work your team is doing.

At Youragency, we help growing brands like yours attract and convert more customers through content that actually performs. Here is what we can do for you:

- High-quality, SEO-optimized blog content
- Thought leadership and industry insights
- End-to-end content strategy and distribution

If this sounds interesting, I'd love to share a few ideas tailored to ${co}. Would you be open to a quick 15-minute chat this week?

Looking forward to hearing from you.

Best regards,
Alex
Founder & Content Strategist`;
}

export const sentEmails = emailLeadOrder.map((leadNum, i) => {
  const n = i + 1;
  const lead = leads[leadNum - 1];
  const fx = FIXED_EMAILS[i];
  const subject = fx ? fx[1] : subjects[i % subjects.length].replace('{co}', lead.company);
  const sentAt = fx ? at(fx[2]) : at('2024-04-15T14:30:00') - (n - 6) * 7 * HOUR;
  const e = {
    id: idOf('em', n, n === 1 ? 'a8f' : undefined), number: n, leadId: lead.id, from: FROM, to: lead.email, subject, sentAt: iso(sentAt),
    deliveryStatus: n % 41 === 0 ? 'Failed' : n > 6 && n % 9 === 0 ? 'Sent' : 'Delivered',
    body: emailBody(lead.name.split(' ')[0], lead.company), replyId: null,
  };
  lead.emailId = e.id;
  lead.lastContactAt = e.sentAt;
  return e;
});

// ---------- replies (5, each linked to a sent email) ----------
// [email number, receivedAt, text]
const replyDefs = [
  [1, '2024-04-22T14:15:00', 'Thanks for reaching out, this looks interesting. Could you share a couple of examples of blog posts you have published for similar teams? A call on Thursday afternoon would work for me.'],
  [5, '2024-04-18T13:20:00', 'Hi Alex, nice to connect! We are planning our Q3 content calendar right now. Are you free for a quick call tomorrow morning?'],
  [2, '2024-04-21T09:30:00', 'Hi Alex, we are already working with an agency, but I would be curious to hear how your reporting works. Can you send over a short deck?'],
  [3, '2024-04-20T17:05:00', 'Thank you for the note. Please send pricing for a content package and we will review it internally.'],
  [6, '2024-04-17T11:45:00', 'Not a fit for us at the moment, but please check back in the new year. Appreciate the detailed email.'],
];

export const replies = replyDefs.map(([emailNumber, receivedAt, text], i) => {
  const orig = sentEmails[emailNumber - 1];
  const lead = leads.find((l) => l.id === orig.leadId);
  const first = lead.name.split(' ')[0];
  const n = i + 1;
  const received = at(receivedAt);
  orig.replyId = idOf('rp', n);
  if (lead.status === 'Responded') { lead.replyId = orig.replyId; lead.lastContactAt = iso(received); }
  return {
    id: orig.replyId, number: n, senderName: lead.name, senderEmail: lead.email, to: FROM,
    subject: `Re: ${orig.subject}`, receivedAt: iso(received), inReplyToId: orig.id,
    body: `Hi Alex,

${text}

Thanks,
${first}

> On ${new Date(orig.sentAt).toUTCString().slice(0, 16)}, Alex wrote:
> ${orig.subject}`,
  };
});

// ---------- blogs ----------
export const blogs = [
  {
    id: blogId(1), number: 1, title: '10 Productivity Tips for Remote Developers', status: 'Posted', site: 'WordPress',
    url: 'https://blog.northwind.io/productivity-tips-remote-developers', postedAt: '2024-03-12T09:00:00Z',
    content: `## Introduction

Working remotely gives developers freedom, but it also removes the structure an office provides. After three years of building software from home, these are the ten habits that made the biggest difference to my output and my sanity.

## The tips

1. Protect your deep-work hours. Block two to three uninterrupted hours each day and treat them like a meeting you cannot move.
2. Plan tomorrow before you log off. Write down the first task you will tackle so you can start without a warm-up.
3. Keep a dedicated workspace. Even a corner of a room helps your brain switch into work mode.
4. Default to asynchronous communication. Write clear updates so teammates are never blocked waiting for a call.
5. Batch your notifications. Check chat and email at set times instead of reacting to every ping.
6. Break work into tasks of two hours or less. Small wins build momentum and make progress visible.
7. Automate anything you do more than three times. Scripts, aliases and templates pay for themselves quickly.
8. Take real breaks away from the screen. A ten-minute walk beats another coffee and a scroll through social media.
9. Document as you go. Future you, and your teammates in other time zones, will be grateful.
10. Set a hard stop. Decide when the day ends and stick to it so work does not leak into the evening.

## Tools that help

- A simple task list, either on paper or in a plain-text file
- A focus timer such as the Pomodoro technique
- Shared documentation that everyone can search

## Conclusion

You do not need to adopt all ten tips at once. Pick two, try them for a week, and keep what works. Productivity as a remote developer is less about squeezing in more hours and more about protecting the hours that matter.`,
  },
  {
    id: blogId(2), number: 2, title: 'Building a SaaS Product in 2024', status: 'Posted', site: 'Medium',
    url: 'https://medium.com/@northwind/building-a-saas-product-in-2024', postedAt: '2024-02-27T14:30:00Z',
    content: `## Why 2024 is a good time to build

The tools available to a small team have never been better. Hosting is cheap, authentication and payments are solved problems, and AI-assisted coding has shortened the path from idea to working prototype. The hard part is no longer building, it is deciding what to build.

## Start with a narrow problem

Pick one painful, specific problem for one type of customer. Broad products are hard to explain and harder to sell. A narrow product can be described in a single sentence, and that sentence is your first piece of marketing.

## A pragmatic stack

- A mainstream web framework your team already knows
- A managed database so you are not running servers on day one
- Off-the-shelf authentication and payments
- Basic analytics from the first release, so you can see what people actually do

## Ship early, then listen

Launch a small version within weeks, not months. Talk to every early user. The first twenty conversations will tell you more than any amount of planning, and they will reshape your roadmap.

## Plan for pricing early

Do not leave pricing until the end. Decide who pays, what they pay for and how often. Even a rough subscription plan forces useful decisions about which features matter.

## Conclusion

Building a SaaS product in 2024 is mostly a question of focus. Choose a small problem, use boring technology, ship quickly and let real customers guide what comes next.`,
  },
  {
    id: blogId(3), number: 3, title: 'Lessons from My First 100 Users', status: 'Posted', site: 'Dev.to',
    url: 'https://dev.to/shivam/lessons-from-my-first-100-users', postedAt: '2024-03-12T09:30:00Z',
    content: `Reaching 100 users took far longer than I expected, and I learned more in that stretch than in the whole year of building before it. Here is what I would tell myself at the start.

The first ten users came from people I already knew. I asked them directly and personally, and their honest feedback shaped the product more than any analytics dashboard. After that, growth came from writing openly about what I was building and answering every message quickly.

Along the way I made plenty of mistakes: I posted in too many communities at once, spent weeks redesigning a landing page nobody had seen, and added features nobody asked for. Each of them taught me to stay closer to real people.

### Key Takeaways

- Focus on solving a real problem
- Talk to your users early and often
- Iterate quickly based on feedback
- Don't underestimate the power of community
- Celebrate small wins along the way

Your first 100 users are not a growth problem, they are a learning opportunity. Stay close to them and the next hundred will be easier.`,
  },
  {
    id: blogId(4), number: 4, title: 'The Future of AI in Web Development', status: 'Posted', site: 'WordPress',
    url: 'https://blog.northwind.io/future-of-ai-in-web-development', postedAt: '2024-04-04T08:00:00Z',
    content: `## Introduction

AI is already part of the everyday toolkit for web developers. Code completion, test generation and design-to-code tools are no longer experiments. The question now is how far this goes and what it means for the craft.

## What is already changing

- Assistants that write boilerplate, tests and documentation
- Natural-language search across large codebases
- Automated accessibility and performance suggestions
- Prototypes generated from a short written description

## What will change next

Expect tools that understand the context of a whole project rather than a single file. They will propose changes across components, explain legacy code and flag risky updates before they ship. Interfaces themselves will also become more adaptive, adjusting content and layout to each visitor.

## What stays the same

Good products still depend on people who understand users, trade-offs and constraints. AI can produce code quickly, but someone has to decide what is worth building and whether the result is correct, secure and accessible.

## How to prepare

- Learn the fundamentals deeply, since you will review more code than you write
- Practise writing precise prompts and clear specifications
- Build habits around testing and code review

## Conclusion

AI will not replace web developers, but developers who use it well will deliver more with less friction. The best time to start experimenting is now.`,
  },
  {
    id: blogId(5), number: 5, title: 'How to Build a Personal Brand as a Developer', status: 'Not Posted', site: null,
    url: null, postedAt: null,
    content: `## Why a personal brand matters

Your reputation travels further than your resume. A visible body of work makes it easier for employers, clients and collaborators to find you and to trust you before the first conversation.

## Start with what you know

You do not need to be an expert to share something useful. Write about problems you solved this week, tools you compared or mistakes you made. Specific, honest posts are more valuable than polished generalities.

## Build a simple home base

- A personal website with a short bio and your best projects
- A consistent name and photo across platforms
- A way for people to contact you

## Be consistent

- Publish on a schedule you can maintain, even if it is once a month
- Choose one or two platforms rather than all of them
- Reuse ideas by turning a blog post into a short thread or a talk

## Contribute to the community

Answer questions, review pull requests and help newcomers. Generosity compounds, and the people you help are often the ones who recommend you later.

## Conclusion

A personal brand is not about self-promotion. It is the sum of the useful things you share over time. Start small, stay consistent and let your work speak.`,
  },
  {
    id: blogId(6), number: 6, title: 'My Workflow for Writing Technical Blogs', status: 'Not Posted', site: null,
    url: null, postedAt: null,
    content: `## Introduction

Writing technical posts used to take me a full weekend. After refining my process, I can now go from idea to published draft in a few focused sessions. This is the workflow I use.

## Step 1: Capture ideas as they happen

Keep a running list of questions you answered, bugs you fixed and things you wished you had known earlier. Most of my best posts started as a single line in that list.

## Step 2: Outline before writing

1. State the problem the reader has
2. List the steps or ideas needed to solve it
3. Decide what the reader should be able to do afterwards

## Step 3: Write a rough draft quickly

Do not edit while you write. Get the whole structure down, including code samples, then improve it later.

## Step 4: Test every snippet

Run each code example from a clean project. Nothing damages trust faster than a sample that does not work.

## Step 5: Edit and publish

- Cut anything that does not serve the reader
- Add a clear title and short introduction
- Read the post aloud to catch awkward sentences

## Conclusion

A repeatable process turns blogging from a chore into a habit. Capture ideas, outline, draft, test and edit, and you will publish more often with less stress.`,
  },
];
