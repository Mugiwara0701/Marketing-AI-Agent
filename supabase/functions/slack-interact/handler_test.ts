// Tests of the Slack decision endpoint (handler.ts): signature, replay window, who may decide, decide-once.
// Run: deno test supabase/functions (CI job "edge functions (deno)"). No network, no database: a fake stands in.
import { assertEquals } from "jsr:@std/assert@1";
import { type Db, handle, MAX_AGE_S, verify } from "./handler.ts";

const SECRET = "test-signing-secret";
const NOW = 1_800_000_000_000; // ms
const EMAIL = "11111111-2222-3333-4444-555555555555";
const REPLY = "66666666-7777-8888-9999-000000000000";

type Row = Record<string, unknown>;

interface Chain {
  select(): Chain | Promise<unknown>;
  eq(col: string, value: unknown): Chain;
  in(col: string, values: unknown[]): Chain;
  update(values: Row): Chain;
  insert(row: Row): Promise<unknown>;
  maybeSingle(): Promise<unknown>;
}

// Behaves like the real database for what the handler uses: decide_email() decides once; an update matches only
// while the row is in one of the allowed states; inserts are recorded.
class FakeDb {
  emails: Record<string, string> = { [EMAIL]: "drafted" };
  replies: Record<string, string> = { [REPLY]: "classified" };
  expired = new Set<string>();
  rpcCalls: Row[] = [];
  inserts: Row[] = [];

  rpc(fn: string, args: Row) {
    this.rpcCalls.push({ fn, ...args });
    const id = String(args.p_email);
    let data = "unknown";
    if (this.expired.has(id)) {
      data = "expired";
    } else if (this.emails[id] === "drafted") {
      this.emails[id] = args.p_approve ? "approved" : "skipped";
      data = args.p_approve ? "approved" : "rejected";
    } else if (id in this.emails) {
      data = "already_decided";
    }
    return Promise.resolve({ data, error: null });
  }

  from(table: string): Chain {
    const filters: Row = {};
    let change: Row | null = null;
    const done = () => {
      if (table === "team_members") return { data: null, error: null };
      if (table === "approvals") return { error: null };
      const rows = table === "replies" ? this.replies : this.emails;
      const id = String(filters.id);
      const ok = id in rows && (filters.status as string[]).includes(rows[id]);
      if (ok && change) rows[id] = String(change.status);
      return { data: ok ? [{ id }] : [], error: null };
    };
    // `select` ends an update (`update().eq().in().select("id")`) and starts a read (`select().eq().maybeSingle()`).
    const chain: Chain = {
      select: () => (change ? Promise.resolve(done()) : chain),
      eq: (col: string, value: unknown) => {
        filters[col] = value;
        return chain;
      },
      in: (col: string, values: unknown[]) => {
        filters[col] = values;
        return chain;
      },
      update: (values: Row) => {
        change = values;
        return chain;
      },
      insert: (row: Row) => {
        this.inserts.push({ table, ...row });
        return Promise.resolve({ error: null });
      },
      maybeSingle: () => Promise.resolve(done()),
    };
    return chain;
  }
}

async function sign(secret: string, ts: string, body: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${ts}:${body}`));
  return "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function body(actionId: string, value: string, user = "U-ALLOWED"): string {
  const payload = { actions: [{ action_id: actionId, value }], user: { id: user } };
  return new URLSearchParams({ payload: JSON.stringify(payload) }).toString();
}

async function call(
  db: FakeDb,
  raw: string,
  opts: { secret?: string; signWith?: string; ts?: string; method?: string } = {},
): Promise<Response> {
  const ts = opts.ts ?? String(NOW / 1000);
  const sig = await sign(opts.signWith ?? SECRET, ts, raw);
  const req = new Request("https://fn.example/slack-interact", {
    method: opts.method ?? "POST",
    headers: { "x-slack-request-timestamp": ts, "x-slack-signature": sig },
    body: opts.method === "GET" ? undefined : raw,
  });
  const env: Record<string, string | undefined> = {
    SLACK_SIGNING_SECRET: "secret" in opts ? opts.secret : SECRET,
    SLACK_ALLOWED_USERS: "U-ALLOWED, U-OTHER",
  };
  return handle(req, { db: db as unknown as Db, env: (n) => env[n], now: () => NOW });
}

Deno.test("an allowed reviewer approves an email once; a repeated click changes nothing", async () => {
  const db = new FakeDb();
  const first = await call(db, body("approve_email", `email:${EMAIL}`));
  assertEquals(first.status, 200);
  assertEquals(db.emails[EMAIL], "approved");
  assertEquals(db.rpcCalls[0].p_by, "slack:U-ALLOWED (U-ALLOWED)");
  const again = await call(db, body("approve_email", `email:${EMAIL}`));
  assertEquals((await again.json()).text, "Already decided.");
  assertEquals(db.emails[EMAIL], "approved");
});

Deno.test("a rejection is recorded as rejected, not sent", async () => {
  const db = new FakeDb();
  assertEquals((await call(db, body("skip_email", `email:${EMAIL}`))).status, 200);
  assertEquals(db.emails[EMAIL], "skipped");
});

Deno.test("a wrong signature is refused before anything is decided", async () => {
  const db = new FakeDb();
  const r = await call(db, body("approve_email", `email:${EMAIL}`), { signWith: "another-secret" });
  assertEquals(r.status, 401);
  assertEquals(db.rpcCalls.length, 0);
});

Deno.test("without a signing secret nothing passes, even a request signed with 'undefined'", async () => {
  const db = new FakeDb();
  const raw = body("approve_email", `email:${EMAIL}`);
  const r = await call(db, raw, { secret: undefined, signWith: "undefined" });
  assertEquals(r.status, 401);
  assertEquals(db.rpcCalls.length, 0);
});

Deno.test("an old or malformed timestamp is refused (replays)", async () => {
  const db = new FakeDb();
  const old = String(NOW / 1000 - MAX_AGE_S - 1);
  assertEquals((await call(db, body("approve_email", `email:${EMAIL}`), { ts: old })).status, 401);
  assertEquals((await call(db, body("approve_email", `email:${EMAIL}`), { ts: "soon" })).status, 401);
  assertEquals(db.rpcCalls.length, 0);
});

Deno.test("a person who is not allowed cannot decide", async () => {
  const db = new FakeDb();
  const r = await call(db, body("approve_email", `email:${EMAIL}`, "U-STRANGER"));
  assertEquals((await r.json()).text.startsWith("You are not allowed"), true);
  assertEquals(db.rpcCalls.length, 0);
  assertEquals(db.emails[EMAIL], "drafted");
});

Deno.test("a button for another kind of row, or a bad id, is refused", async () => {
  const db = new FakeDb();
  assertEquals((await call(db, body("approve_email", `reply:${EMAIL}`))).status, 400);
  assertEquals((await call(db, body("approve_email", "email:not-an-id"))).status, 400);
  assertEquals(db.rpcCalls.length, 0);
});

Deno.test("an expired request says so and decides nothing", async () => {
  const db = new FakeDb();
  db.expired.add(EMAIL);
  const r = await call(db, body("approve_email", `email:${EMAIL}`));
  assertEquals((await r.json()).text.startsWith("This request expired"), true);
  assertEquals(db.emails[EMAIL], "drafted");
});

Deno.test("a reply is approved once and the approver is recorded", async () => {
  const db = new FakeDb();
  assertEquals((await call(db, body("approve_reply", `reply:${REPLY}`))).status, 200);
  assertEquals(db.replies[REPLY], "acknowledged");
  assertEquals(db.inserts.length, 1);
  assertEquals(db.inserts[0].decided_by, "slack:U-ALLOWED (U-ALLOWED)");
  const again = await call(db, body("approve_reply", `reply:${REPLY}`));
  assertEquals((await again.json()).text, "Already decided.");
  assertEquals(db.inserts.length, 1);
});

Deno.test("only POST is accepted, and a broken payload is a 400", async () => {
  const db = new FakeDb();
  assertEquals((await call(db, "", { method: "GET" })).status, 405);
  assertEquals((await call(db, "payload=%7Bnot-json")).status, 400);
});

Deno.test("verify: the signature covers timestamp and body", async () => {
  const ts = String(NOW / 1000);
  const sig = await sign(SECRET, ts, "a=1");
  assertEquals(await verify(SECRET, ts, sig, "a=1", NOW / 1000), true);
  assertEquals(await verify(SECRET, ts, sig, "a=2", NOW / 1000), false);
  assertEquals(await verify(SECRET, String(NOW / 1000 + 1), sig, "a=1", NOW / 1000), false);
});
