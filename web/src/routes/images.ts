import { Router } from "express";
import type { Pool } from "pg";
import type { AuthedRequest } from "../middleware/session.js";
import type { Storage } from "../services/storage.js";
import { getOwnedPhoto } from "../queries/batches.js";
import { getOwnedCardCrop } from "../queries/cards.js";

export function imagesRouter(pool: Pool, storage: Storage): Router {
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

  return r;
}
