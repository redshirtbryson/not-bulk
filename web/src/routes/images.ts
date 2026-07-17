import { Router } from "express";
import type { Pool } from "pg";
import type { AuthedRequest } from "../middleware/session.js";
import type { Storage } from "../services/storage.js";
import type { Config } from "../config.js";
import { getOwnedPhoto } from "../queries/batches.js";
import { getOwnedCardCrop } from "../queries/cards.js";
import { ensureRefCached } from "../services/refproxy.js";

export function imagesRouter(pool: Pool, storage: Storage, cfg: Config): Router {
  const r = Router();

  // 404 (not 403) when not owned — ownership failure is indistinguishable
  // from a missing object (AC 7: no route reveals another user's ids).
  r.get("/img/photo/:id", async (req: AuthedRequest, res) => {
    const id = req.params.id as string;
    const photo = await getOwnedPhoto(pool, req.user!.id, id);
    if (!photo || !photo.storage_key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(photo.storage_key);
    return res.redirect(302, url);
  });

  r.get("/img/crop/:id", async (req: AuthedRequest, res) => {
    const id = req.params.id as string;
    const card = await getOwnedCardCrop(pool, req.user!.id, id);
    if (!card || !card.crop_storage_key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(card.crop_storage_key);
    return res.redirect(302, url);
  });

  // Reference art is GLOBAL, not owner-scoped (unlike /img/crop): card_refs is shared
  // reference data. requireUser (applied by the app-level /img mount) gates access to
  // authenticated users, but there is no per-user ownership check on reference images.
  r.get("/img/ref/:cardRefId", async (req: AuthedRequest, res) => {
    const key = await ensureRefCached(pool, storage, cfg, req.params.cardRefId as string);
    if (!key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(key);
    return res.redirect(302, url);
  });

  return r;
}
