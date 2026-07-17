import express, { type Express, type RequestHandler } from "express";
import type { Pool } from "pg";
import { Client } from "pg";
import nunjucks from "nunjucks";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Config } from "./config.js";
import type { Mailer } from "./services/mailer.js";
import { Storage } from "./services/storage.js";
import type { gateImage } from "./services/imagegate.js";
import type { verifyTurnstile } from "./services/turnstile.js";
import { csp } from "./middleware/csp.js";
import { notFound, errorHandler } from "./middleware/errors.js";
import { requireUser } from "./middleware/session.js";
import { imagesRouter } from "./routes/images.js";
import { batchesRouter } from "./routes/batches.js";
import { progressRouter } from "./routes/progress.js";
import type { PgLikeClient } from "./services/progressbus.js";

const here = dirname(fileURLToPath(import.meta.url)); // .../web/src
const webRoot = dirname(here); // .../web

export interface AppDeps {
  cfg: Config;
  pool: Pool;
  mailer?: Mailer;
  storage?: Storage;
  gateImage?: typeof gateImage;
  verifyTurnstile?: typeof verifyTurnstile;
  sessionMiddleware?: RequestHandler;
  listenClientFactory?: () => Promise<PgLikeClient>;
}

export function createApp(deps: AppDeps): Express {
  const { cfg, pool } = deps;
  const app = express();

  nunjucks.configure(join(webRoot, "views"), {
    autoescape: true,
    express: app,
    noCache: true,
  });
  app.set("view engine", "njk");

  app.use(csp());
  app.use(express.static(join(webRoot, "public")));
  app.use(express.urlencoded({ extended: false, limit: "1mb" }));
  app.use(express.json({ limit: "1mb" }));

  app.get("/healthz", async (_req, res, next) => {
    try {
      await pool.query("SELECT 1");
      res.json({ ok: true });
    } catch (err) {
      next(err);
    }
  });

  if (deps.sessionMiddleware) app.use(deps.sessionMiddleware);

  const storage = deps.storage ?? new Storage(cfg);
  app.use("/img", requireUser());
  app.use(imagesRouter(pool, storage));

  app.use(
    "/batches",
    requireUser(),
    batchesRouter({
      pool,
      cfg,
      storage,
      gateImage: deps.gateImage,
      verifyTurnstile: deps.verifyTurnstile,
    }),
  );

  // Dedicated LISTEN client factory: a raw pg Client (NOT from the pool — LISTEN holds
  // it open for the process lifetime). progressRouter applies its own requireUser() per
  // route since GET /batches/:id/events must return 401/redirect before any SSE headers
  // are written.
  const listenFactory: () => Promise<PgLikeClient> =
    deps.listenClientFactory ??
    (async () => {
      const c = new Client({ connectionString: process.env.DATABASE_URL });
      await c.connect();
      return c as unknown as PgLikeClient;
    });
  app.use(progressRouter(pool, cfg, listenFactory));

  app.use(notFound());
  app.use(errorHandler());

  return app;
}
