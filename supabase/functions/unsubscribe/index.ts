// One-click unsubscribe (RFC 8058). GET shows a confirmation page; POST (mail clients) or a
// confirmed form submit adds the address to the suppression list. Token = HMAC of the email id.
import { db, emailHash } from "../_shared/db.ts";

const enc = new TextEncoder();
const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");

async function sign(id: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(Deno.env.get("UNSUBSCRIBE_SECRET")!), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(id)));
}

async function suppress(emailId: string, token: string): Promise<boolean> {
  if (token !== (await sign(emailId))) return false;
  const { data } = await db.from("emails").select("id, contact_id, contacts(email)").eq("id", emailId).maybeSingle();
  // deno-lint-ignore no-explicit-any
  const addr = (data as any)?.contacts?.email as string | undefined;
  if (!addr) return false;
  await db.from("suppression_list").upsert(
    { email_hash: await emailHash(addr), reason: "unsubscribe" }, { onConflict: "email_hash" },
  );
  // Stop the sequence: skip any queued emails to this contact.
  await db.from("emails").update({ status: "skipped" }).eq("contact_id", data!.contact_id)
    .in("status", ["drafted", "approved"]);
  return true;
}

const page = (msg: string, form = "") =>
  new Response(`<!doctype html><meta charset=utf-8><title>Unsubscribe</title><body style="font-family:sans-serif;max-width:32rem;margin:4rem auto"><p>${msg}</p>${form}`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } });

Deno.serve(async (req) => {
  const u = new URL(req.url);
  const id = u.searchParams.get("e") ?? "";
  const t = u.searchParams.get("t") ?? "";
  if (req.method === "GET") {
    if (t !== (await sign(id))) return page("Invalid link.");
    return page("Unsubscribe from our emails?",
      `<form method=post action="?e=${encodeURIComponent(id)}&t=${encodeURIComponent(t)}"><button>Confirm</button></form>`);
  }
  if (req.method === "POST") {
    return (await suppress(id, t)) ? page("You have been unsubscribed.") : new Response("invalid", { status: 400 });
  }
  return new Response("method not allowed", { status: 405 });
});
