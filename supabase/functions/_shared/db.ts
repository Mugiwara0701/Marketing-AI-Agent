import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// Edge Functions use their own service key, held in function secrets.
export const db = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const enc = new TextEncoder();

export async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", enc.encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Matches SQL email_hash(): sha256 of the trimmed, lowercased address.
export const emailHash = (addr: string) => sha256Hex(addr.trim().toLowerCase());

export async function dispatchGithub(eventType: string, payload: Record<string, unknown>) {
  const repo = Deno.env.get("GITHUB_REPO")!; // owner/name
  const r = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${Deno.env.get("GITHUB_DISPATCH_TOKEN")!}`, // scoped to this repo only
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: eventType, client_payload: payload }),
  });
  if (!r.ok) throw new Error(`github dispatch failed: ${r.status}`);
}
