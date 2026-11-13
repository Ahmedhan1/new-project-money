// Minimal Cloudflare D1 stand-in over node:sqlite, for tests only (prepare/bind/first/all/run/batch).
import { DatabaseSync } from "node:sqlite";
import { SCHEMA } from "../src/schema.js";

export function makeD1() {
  const db = new DatabaseSync(":memory:");
  for (const s of SCHEMA) db.exec(s);
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    first: async () => db.prepare(sql).get(...args) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...args) }),
    run: async () => db.prepare(sql).run(...args),
  });
  return { prepare: (sql) => stmt(sql), batch: async (list) => Promise.all(list.map((s) => s.run())), raw: db };
}
