// All authentication lives here, backed by Supabase Auth. Nothing else in the app should know how auth works.
import { supabase } from './supabase';

export async function getSession() {
  const { data } = await supabase.auth.getSession();
  return data.session;
}

export const isAuthenticated = async () => !!(await getSession());

export async function signIn(email, password) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw new Error('Invalid email or password.');
  return data.session;
}

export async function signOut() {
  await supabase.auth.signOut();
}

export async function requestPasswordReset(email) {
  if (!/^\S+@\S+\.\S+$/.test(email)) throw new Error('Enter a valid email address.');
  const { error } = await supabase.auth.resetPasswordForEmail(email);
  if (error) throw new Error('Could not send the reset link. Please try again.');
}
