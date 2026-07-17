import { z } from 'zod';
import { uuidv7 } from 'uuidv7';
import type { PoolClient } from 'pg';

export type JobType = 'detect' | 'identify' | 'fetch_source' | 'ingest_correction' | 'export';

export const detectPayload = z.object({ photo_id: z.string() }).strict();
export const identifyPayload = z.object({ card_id: z.string() }).strict();
export const fetchSourcePayload = z.object({ photo_id: z.string() }).strict();
export const ingestCorrectionPayload = z
  .object({ card_id: z.string(), actual_ref_id: z.string(), predicted_ref_id: z.string().nullable() })
  .strict();
export const exportPayload = z.object({ export_id: z.string() }).strict();

const SCHEMAS: Record<JobType, z.ZodType> = {
  detect: detectPayload,
  identify: identifyPayload,
  fetch_source: fetchSourcePayload,
  ingest_correction: ingestCorrectionPayload,
  export: exportPayload,
};

export interface EnqueueJob {
  type: JobType;
  payload: object;
  batchId?: string;
  userId?: string;
}

export async function enqueue(client: PoolClient, job: EnqueueJob): Promise<string> {
  const schema = SCHEMAS[job.type];
  if (!schema) throw new Error(`unknown job type: ${job.type}`);
  schema.parse(job.payload); // throws ZodError on mismatch — before any DB write

  const id = uuidv7();
  const { rows } = await client.query(
    `INSERT INTO jobs (id, type, payload, batch_id, user_id)
     VALUES ($1, $2, $3::jsonb, $4, $5)
     RETURNING id`,
    [id, job.type, JSON.stringify(job.payload), job.batchId ?? null, job.userId ?? null],
  );
  return rows[0].id;
}
