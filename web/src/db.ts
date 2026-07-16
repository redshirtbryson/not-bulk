import pg from "pg";

let pool: pg.Pool | null = null;

export function getPool(): pg.Pool {
  if (pool) return pool;
  const dsn = process.env.DATABASE_URL;
  if (!dsn) {
    throw new Error(
      "DATABASE_URL is not set; run the command under `bws run` so Bitwarden injects the connection string",
    );
  }
  pool = new pg.Pool({ connectionString: dsn, max: 10 });
  return pool;
}
