// Single Supabase client for the browser. Reads the public URL + anon key from .env.local.
import { createClient } from '@supabase/supabase-js';

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

// NEXT_PUBLIC_* vars must be referenced literally (above) so Next can inline them.
if (!url || !/^https?:\/\//.test(url)) {
  throw new Error(
    'NEXT_PUBLIC_SUPABASE_URL is missing or not an http(s) URL. ' +
      'Set it in eval/dashboard/.env.local (e.g. https://<project-ref>.supabase.co) and restart `npm run dev`.',
  );
}
if (!anonKey) {
  throw new Error(
    'NEXT_PUBLIC_SUPABASE_ANON_KEY is missing. ' +
      'Set it in eval/dashboard/.env.local and restart `npm run dev`.',
  );
}

export const supabase = createClient(url, anonKey);
