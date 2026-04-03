import { betterAuth } from "better-auth";
import { bearer } from "better-auth/plugins";
import { Kysely, SqliteDialect } from "kysely";
import Database from "better-sqlite3";
import * as path from "path";

const getDatabasePath = () => {
  if (typeof process !== "undefined" && process.env.DATABASE_PATH) {
    return process.env.DATABASE_PATH;
  }
  return path.resolve("./data/auth.db");
};

const dbPath = getDatabasePath();
const sqlite = new Database(dbPath);
const dialect = new SqliteDialect({
  database: sqlite,
});
const db = new Kysely({ dialect });

export const auth = betterAuth({
  secret: process.env.BETTER_AUTH_SECRET || "dev-secret-change-in-production",
  database: db,
  emailAndPassword: {
    enabled: true,
  },
  plugins: [bearer()],
  session: {
    expiresIn: 60 * 60 * 24 * 7, // 7 days
    updateAge: 60 * 60 * 24, // 1 day
  },
});

export type Session = typeof auth.$Infer.Session;
