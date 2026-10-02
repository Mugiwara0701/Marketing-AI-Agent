// Resend webhook: delivery / open / bounce events and inbound replies. Verifies the Svix signature, then
// records each event exactly once (svix-id is the dedupe key) and updates the emails / replies tables.
// Inbound mail only lands here as a `replies` row with status 'received'; the daily agent classifies it.
import { db, emailHash } from "../_shared/db.ts";

const enc = new TextEncoder();
const MAX_BODY = 20_000;

// Svix: signature = base64(HMAC-SHA256(base64decode(secret), `${id}.${timestamp}.${body}`)), header may
// carry several space separated `v1,<sig>` entries (key rotation).
async function verify(req: Request, body: string): Promise<boolean> {
  const id = req.headers.get("svix-id") ?? "";
  const ts = req.headers.get("svix-timestamp") ?? "";
  const sigs = (req.headers.get("svix-signature") ?? "").split(" ");
  if (!id || !ts || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false; // older than 5 min
  const raw = (Deno.env.get("RESEND_WEBHOOK_SECRET") ?? "").replace(/^whsec_/, "");
  const key = await crypto.subtle.importKey(
    "raw",
    Uint8Array.from(atob(raw), (c) => c.charCodeAt(0)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`${id}.${ts}.${body}`));
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));
  return sigs.some((s) => {
    const got = s.startsWith("v1,") ? s.slice(3) : "";
    if (got.length !== expected.length) return false;
    let diff = 0; // constant-time compare
    for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ got.charCodeAt(i);
    return diff === 0;
  });
}

// deno-lint-ignore no-explicit-any
type Json = any;

// "Name <a@b.io>" or "a@b.io" -> "a@b.io"
const bareAddress = (s: string) => (s.match(/<([^>]+)>/)?.[1] ?? s).trim().toLowerCase();

// Headers may arrive as an object or as [{name, value}].
function header(data: Json, name: string): string | undefined {
  const h = data.headers;
  if (Array.isArray(h)) {
    return h.find((x: Json) => String(x?.name).toLowerCase() === name)?.value;
  }
  if (h && typeof h === "object") {
    const k = Object.keys(h).find((x) => x.toLowerCase() === name);
    return k ? String(h[k]) : undefined;
  }
  return undefined;
}

async function emailIdFor(providerId: string): Promise<string | null> {
  const { data } = await db.from("emails").select("id").eq("provider_id", providerId).maybeSingle();
  return data?.id ?? null;
}

// Stop the sequence for a contact: skip anything still queued.
async function stopContact(contactId: string) {
  await db.from("emails").update({ status: "skipped" }).eq("contact_id", contactId)
    .in("status", ["drafted", "approved"]);
}

async function onBounce(data: Json, reason: "bounce" | "complaint"): Promise<string | null> {
  const emailId = await emailIdFor(String(data.email_id));
  if (!emailId) return null;
  await db.from("emails").update({ status: "bounced", bounced_at: new Date().toISOString() })
    .eq("id", emailId).eq("status", "sent");
  const { data: row } = await db.from("emails").select("contact_id, contacts(email)").eq("id", emailId)
    .maybeSingle();
  const addr = (row as Json)?.contacts?.email as string | undefined;
  if (addr) {
    await db.from("suppression_list").upsert({ email_hash: await emailHash(addr), reason }, {
      onConflict: "email_hash",
    });
    await stopContact((row as Json).contact_id);
  }
  return emailId;
}

async function onOpened(data: Json): Promise<string | null> {
  const emailId = await emailIdFor(String(data.email_id));
  if (!emailId) return null;
  const { data: row } = await db.from("emails").select("opened_at, open_count").eq("id", emailId)
    .single();
  await db.from("emails").update({
    opened_at: row?.opened_at ?? new Date().toISOString(),
    open_count: (row?.open_count ?? 0) + 1,
  }).eq("id", emailId);
  return emailId;
}

async function onDelivered(data: Json): Promise<string | null> {
  const emailId = await emailIdFor(String(data.email_id));
  if (emailId) {
    await db.from("emails").update({ delivered_at: new Date().toISOString() }).eq("id", emailId);
  }
  return emailId;
}

// The webhook may carry only metadata; fetch the text from Resend when the body is missing.
async function inboundText(data: Json): Promise<string> {
  if (typeof data.text === "string" && data.text) return data.text;
  const key = Deno.env.get("RESEND_API_KEY");
  if (!key || !data.email_id) return "";
  const r = await fetch(`https://api.resend.com/emails/receiving/${encodeURIComponent(data.email_id)}`, {
    headers: { Authorization: `Bearer ${key}` },
  });
  if (!r.ok) throw new Error(`resend fetch failed: ${r.status}`);
  return String((await r.json()).text ?? "");
}

// Match the reply to our mail: In-Reply-To / References against emails.message_id first, then the sender's
// address against contacts (latest sent mail). Unmatched mail is recorded as an event and ignored.
async function onReceived(data: Json): Promise<string | null> {
  const refs = [header(data, "in-reply-to"), header(data, "references"), data.in_reply_to]
    .flatMap((v) => (v ? String(v).split(/\s+/) : []));
  let email: Json = null;
  if (refs.length) {
    ({ data: email } = await db.from("emails").select("id, contact_id, message_id").in("message_id", refs)
      .order("sent_at", { ascending: false }).limit(1).maybeSingle());
  }
  if (!email) {
    const from = bareAddress(String(data.from ?? ""));
    const { data: contacts } = await db.from("contacts").select("id").ilike("email", from);
    const ids = (contacts ?? []).map((c: Json) => c.id);
    if (ids.length) {
      ({ data: email } = await db.from("emails").select("id, contact_id, message_id")
        .in("contact_id", ids).eq("status", "sent").order("sent_at", { ascending: false }).limit(1)
        .maybeSingle());
    }
  }
  if (!email) return null;
  const body = (await inboundText(data)).slice(0, MAX_BODY);
  const { error } = await db.from("replies").insert({
    email_id: email.id,
    contact_id: email.contact_id,
    message_id: String(data.message_id ?? `resend:${data.email_id}`),
    in_reply_to: refs[0] ?? email.message_id,
    subject: data.subject ?? null,
    body,
    status: "received",
  });
  if (error && error.code !== "23505") throw error; // 23505: same message already stored
  await db.from("contacts").update({ last_engaged_at: new Date().toISOString() }).eq("id", email.contact_id);
  return email.id;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });
  const body = await req.text();
  if (!(await verify(req, body))) return new Response("invalid signature", { status: 401 });

  const eventId = req.headers.get("svix-id")!;
  const event = JSON.parse(body);
  const type = String(event.type ?? "");
  const data = event.data ?? {};

  // Claim the event first; a redelivery hits the unique key and is acknowledged without work.
  const { error: dup } = await db.from("email_events").insert({
    provider_event_id: eventId,
    type: type.replace(/^email\./, ""),
    payload: { type, email_id: data.email_id ?? null }, // ids only, no addresses or bodies
  });
  if (dup) return dup.code === "23505" ? new Response("ok") : new Response("db error", { status: 500 });

  try {
    let emailId: string | null = null;
    if (type === "email.delivered") emailId = await onDelivered(data);
    else if (type === "email.opened") emailId = await onOpened(data);
    else if (type === "email.bounced") emailId = await onBounce(data, "bounce");
    else if (type === "email.complained") emailId = await onBounce(data, "complaint");
    else if (type === "email.received") emailId = await onReceived(data);
    if (emailId) await db.from("email_events").update({ email_id: emailId }).eq("provider_event_id", eventId);
  } catch (e) {
    // Release the claim so Resend's retry is processed. Log the error text only, never the payload (PII).
    console.error("resend-webhook failed", type, e instanceof Error ? e.message : String(e));
    await db.from("email_events").delete().eq("provider_event_id", eventId);
    return new Response("error", { status: 500 });
  }
  return new Response("ok");
});
