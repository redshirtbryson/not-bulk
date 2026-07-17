import { Router } from 'express';
import multer from 'multer';
import { uuidv7 } from 'uuidv7';
import type { Pool } from 'pg';
import type { Config } from '../config.js';
import type { AuthedRequest } from '../middleware/session.js';
import type { Storage } from '../services/storage.js';
import { gateImage as realGateImage } from '../services/imagegate.js';
import { verifyTurnstile as realVerifyTurnstile } from '../services/turnstile.js';
import { checkAndReserve } from '../services/quotas.js';
import { enqueue } from '../services/jobs.js';

export interface BatchesDeps {
  pool: Pool;
  cfg: Config;
  storage: Storage;
  gateImage?: typeof realGateImage;
  verifyTurnstile?: typeof realVerifyTurnstile;
}

interface ParsedUrl { url: string; sourceType: 'imgur' | 'reddit' }

// Exact-hostname allowlist. Case-insensitive EXACT match — no suffix/subdomain
// tricks ('evil-imgur.com', 'imgur.com.evil.io' both fail). No network I/O here;
// the real SSRF gate is worker-side (Task 13).
function preGateUrl(raw: string, cfg: Config): ParsedUrl | { error: string } {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return { error: 'not a valid URL' };
  }
  if (u.protocol !== 'https:') return { error: 'https only' };
  const host = u.hostname.toLowerCase();
  const allowed = cfg.fetcher.allowed_hosts.map((h) => h.toLowerCase());
  if (!allowed.includes(host)) return { error: `host not allowed: ${host}` };

  // Non-empty path for album/gallery hosts (imgur.com album/gallery, reddit post).
  const pathEmpty = u.pathname === '' || u.pathname === '/';
  if ((host === 'imgur.com' || host === 'www.reddit.com') && pathEmpty) {
    return { error: 'album/gallery URL requires a path' };
  }
  const sourceType: 'imgur' | 'reddit' =
    host === 'www.reddit.com' || host === 'i.redd.it' ? 'reddit' : 'imgur';
  return { url: raw, sourceType };
}

export function batchesRouter(deps: BatchesDeps): Router {
  const { pool, cfg, storage } = deps;
  const gate = deps.gateImage ?? realGateImage;
  const verifyTurnstile = deps.verifyTurnstile ?? realVerifyTurnstile;

  const upload = multer({
    storage: multer.memoryStorage(),
    limits: {
      fileSize: cfg.quotas.max_photo_bytes,
      files: cfg.quotas.photos_per_batch,
    },
  });

  const r = Router();

  r.post('/', (req: AuthedRequest, res, next) => {
    upload.array('photos')(req, res, async (mErr: unknown) => {
      // multer limit errors (too many files / oversize) → 400.
      if (mErr) return res.status(400).send('upload rejected (file limit or too many files)');

      const files = (req.files as Express.Multer.File[] | undefined) ?? [];
      const urls = (req.body?.urls as string | undefined)?.trim();

      // Mixed input guard (Task 9 owns the urls branch).
      if (files.length > 0 && urls) return res.status(400).send('choose one input method');
      if (files.length === 0 && !urls) return res.status(400).send('no photos supplied');

      if (urls) {
        // Split on any whitespace/newlines; max 10 entries pre-trim (contract).
        const rawEntries = urls.split(/\s+/).map((s) => s.trim()).filter(Boolean);
        if (rawEntries.length > 10) return res.status(400).send('too many urls (max 10)');
        if (rawEntries.length === 0) return res.status(400).send('no urls supplied');

        const parsed: ParsedUrl[] = [];
        for (const entry of rawEntries) {
          const g = preGateUrl(entry, cfg);
          if ('error' in g) return res.status(400).send(g.error); // reject the whole request
          parsed.push(g);
        }

        const okUrl = await verifyTurnstile(cfg, (req.body?.['cf-turnstile-response'] as string) ?? '', req.ip);
        if (!okUrl) return res.status(400).send('turnstile verification failed');

        const userId = req.user!.id;
        const count = parsed.length;
        const client = await pool.connect();
        try {
          await client.query('BEGIN');

          const reserve = await checkAndReserve(client, cfg, userId, {
            batches: 1,
            photos: count,
            fetches: count,
          });
          if (!reserve.ok) {
            await client.query('ROLLBACK');
            return res.status(400).send(`quota exceeded: ${reserve.reason}`);
          }

          const { rows: batchRows } = await client.query(
            `INSERT INTO batches (id, user_id, status, origin_url)
             VALUES ($1, $2, 'processing', $3) RETURNING id`,
            [uuidv7(), userId, parsed[0].url],
          );
          const batchId = batchRows[0].id;

          for (const p of parsed) {
            const photoId = uuidv7();
            // status 'fetching', storage_key NULL — worker fills bytes (Task 13).
            await client.query(
              `INSERT INTO photos (id, batch_id, status, source_type, source_url)
               VALUES ($1, $2, 'fetching', $3, $4) RETURNING id`,
              [photoId, batchId, p.sourceType, p.url],
            );
            await enqueue(client, {
              type: 'fetch_source',
              payload: { photo_id: photoId },
              batchId,
              userId,
            });
          }

          await client.query(`UPDATE batches SET photo_count = $2 WHERE id = $1`, [batchId, count]);
          await client.query('COMMIT');
          await pool.query('NOTIFY jobs_wake');
          return res.redirect(302, `/batches/${batchId}`);
        } catch (err) {
          await client.query('ROLLBACK');
          return next(err);
        } finally {
          client.release();
        }
      }

      // Turnstile before any DB work.
      const ok = await verifyTurnstile(cfg, (req.body?.['cf-turnstile-response'] as string) ?? '', req.ip);
      if (!ok) return res.status(400).send('turnstile verification failed');

      const userId = req.user!.id;
      const client = await pool.connect();
      try {
        await client.query('BEGIN');

        const reserve = await checkAndReserve(client, cfg, userId, {
          batches: 1,
          photos: files.length,
        });
        if (!reserve.ok) {
          await client.query('ROLLBACK');
          return res.status(400).send(`quota exceeded: ${reserve.reason}`);
        }

        const { rows: batchRows } = await client.query(
          `INSERT INTO batches (id, user_id, status) VALUES ($1, $2, 'processing') RETURNING id`,
          [uuidv7(), userId],
        );
        const batchId = batchRows[0].id;

        const rejects: string[] = [];
        let stored = 0;
        let bytesTotal = 0;

        for (const f of files) {
          const g = await gate(f.buffer, cfg);
          if (!g.ok) {
            rejects.push(`${f.originalname}: ${g.reason}`);
            continue;
          }
          const photoId = uuidv7();
          const key = storage.photoKey(userId, batchId, photoId);
          await client.query(
            `INSERT INTO photos (id, batch_id, status, storage_key, source_type, bytes)
             VALUES ($1, $2, 'stored', $3, 'upload', $4) RETURNING id`,
            [photoId, batchId, key, g.webp.length],
          );
          // put BEFORE commit — if it throws we ROLLBACK; the never-committed
          // batchId means these keys are unreachable (no orphan rows). An object
          // may briefly exist in MinIO under a dead batch id; acceptable for M2
          // (M4 janitor sweeps). Documented tradeoff.
          await storage.put(key, g.webp, 'image/webp');
          await enqueue(client, { type: 'detect', payload: { photo_id: photoId }, batchId, userId });
          stored += 1;
          bytesTotal += g.webp.length;
        }

        if (stored === 0) {
          await client.query('ROLLBACK');
          return res.status(400).send(`all files rejected:\n${rejects.join('\n')}`);
        }

        await client.query(
          `UPDATE users SET storage_bytes_used = storage_bytes_used + $2 WHERE id = $1`,
          [userId, bytesTotal],
        );
        await client.query(`UPDATE batches SET photo_count = $2 WHERE id = $1`, [batchId, stored]);

        await client.query('COMMIT');
        // NOTIFY only after commit — the worker must not wake before rows exist.
        await pool.query('NOTIFY jobs_wake');
        return res.redirect(302, `/batches/${batchId}`);
      } catch (err) {
        await client.query('ROLLBACK');
        // Pass to Express via next(), NOT throw. This callback is invoked manually by
        // multer (not by Express's router), so a throw here becomes an unhandled promise
        // rejection in Express 5 — the error never reaches the error middleware and the
        // request hangs. `next(err)` routes it to the Task 2 error handler → 500.
        return next(err);
      } finally {
        client.release();
      }
    });
  });

  return r;
}
