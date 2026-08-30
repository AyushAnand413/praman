/**
 * Where the backend lives, and whether we actually know.
 *
 * The old default was a bare `|| "http://localhost:8000"`. That is right in
 * development and quietly wrong in production: a deploy that forgot the env var
 * points every request at the viewer's own machine, so the console shows
 * connection errors that look like the backend is down rather than like the
 * build is misconfigured.
 *
 * So localhost stays the default only where localhost is plausible. In a
 * production build with no NEXT_PUBLIC_API_URL, `API_MISCONFIGURED` is true and
 * the UI says what is wrong instead of guessing.
 */
const RAW = (process.env.NEXT_PUBLIC_API_URL || "").trim().replace(/\/+$/, "");
const IS_PROD = process.env.NODE_ENV === "production";

export const API = RAW || (IS_PROD ? "" : "http://localhost:8000");

/** True when a production build has no API URL configured. */
export const API_MISCONFIGURED = !RAW && IS_PROD;

/** Copy for the misconfiguration state, kept here so it is stated once. */
export const API_MISCONFIGURED_MESSAGE =
  "This console has no backend URL. Set NEXT_PUBLIC_API_URL to your PRAMAN API " +
  "and redeploy.";
