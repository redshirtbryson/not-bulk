import type { Pool } from "pg";

const CLAIM_SQL =
  "UPDATE jobs SET status='running', locked_at=now(), locked_by=$1, " +
  "attempts=attempts+1, updated_at=now() " +
  "WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now() AND type='export' " +
  "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) " +
  "RETURNING id, payload";

const COMPLETE_SQL = "UPDATE jobs SET status='done', updated_at=now() WHERE id=$1";

const FAIL_DEAD_SQL =
  "UPDATE jobs SET status='failed', last_error=$1, locked_at=NULL, " +
  "locked_by=NULL, updated_at=now() WHERE id=$2 RETURNING status";

export async function claimExportJob(
  pool: Pool,
  workerId: string,
): Promise<{ id: string; payload: { export_id: string } } | null> {
  const { rows } = await pool.query(CLAIM_SQL, [workerId]);
  const row = rows[0];
  if (!row) return null;
  const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
  return { id: String(row.id), payload };
}

export async function completeJob(pool: Pool, jobId: string): Promise<void> {
  await pool.query(COMPLETE_SQL, [jobId]);
}

// The export worker always fails dead (no auto-retry; a rerun is a new export).
// `dead` is accepted for parity with the Python fail() signature.
export async function failJob(
  pool: Pool,
  jobId: string,
  error: string,
  _dead: boolean,
): Promise<string> {
  const { rows } = await pool.query(FAIL_DEAD_SQL, [error, jobId]);
  return (rows[0]?.status as string) ?? "failed";
}
