import { STUDIO_SCHEMA } from "./studio.js";

// SQLite schema (Durable Object storage). Applied idempotently on the first request of each isolate (no manual migration step needed).
const BASE = [
  "CREATE TABLE IF NOT EXISTS settings (\n  k TEXT PRIMARY KEY,\n  v TEXT NOT NULL,\n  updated INTEGER NOT NULL\n)",
  "CREATE TABLE IF NOT EXISTS jobs (\n  id TEXT PRIMARY KEY,\n  task TEXT NOT NULL,\n  title TEXT NOT NULL DEFAULT '',\n  params TEXT NOT NULL,\n  gpu INTEGER NOT NULL DEFAULT 1,\n  max_runtime_s INTEGER NOT NULL,\n  status TEXT NOT NULL,\n  attempt INTEGER NOT NULL DEFAULT 1,\n  kernel_ref TEXT,\n  kernel_url TEXT,\n  version INTEGER,\n  remote_status TEXT,\n  error TEXT,\n  result TEXT,\n  files TEXT,\n  created INTEGER NOT NULL,\n  updated INTEGER NOT NULL,\n  submitted INTEGER,\n  finished INTEGER\n)",
  "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)",
  "CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created)",
  "CREATE TABLE IF NOT EXISTS login_failures (\n  ip TEXT NOT NULL,\n  ts INTEGER NOT NULL\n)",
  "CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures(ip, ts)"
];
export const SCHEMA = [...BASE, ...STUDIO_SCHEMA];

const ready = new WeakMap();

export function ensureSchema(db) {
  if (!ready.has(db)) ready.set(db, db.batch(SCHEMA.map((s) => db.prepare(s))).catch((e) => { ready.delete(db); throw e; }));
  return ready.get(db);
}
