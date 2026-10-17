// Slack interactivity endpoint (Request URL). The logic is in handler.ts (tested in handler_test.ts); this file only
// wires it to the real database, the function's secrets and Slack's response_url. Must answer Slack within 3 seconds.
import { db } from "../_shared/db.ts";
import { handle } from "./handler.ts";

function notify(url: string, text: string): void {
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ replace_original: true, text }),
  }).catch(() => {});
}

Deno.serve((req) => handle(req, { db, env: (name) => Deno.env.get(name), notify }));
