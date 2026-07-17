import express, { type Express, type RequestHandler } from "express";
import type { Pool } from "pg";
import { Client } from "pg";
import nunjucks from "nunjucks";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Config } from "./config.js";
import type { Mailer } from "./services/mailer.js";
import { smtpMailer } from "./services/mailer.js";
import { Storage } from "./services/storage.js";
import { sessionMiddleware as realSessionMiddleware } from "./middleware/session.js";
import type { gateImage } from "./services/imagegate.js";
import type { verifyTurnstile } from "./services/turnstile.js";
import { csp } from "./middleware/csp.js";
import { notFound, errorHandler } from "./middleware/errors.js";
import { requireUser } from "./middleware/session.js";
import { imagesRouter } from "./routes/images.js";
import { batchesRouter } from "./routes/batches.js";
import { progressRouter } from "./routes/progress.js";
import { validateRouter } from "./routes/validate.js";
import { searchRouter } from "./routes/search.js";
import { landingRouter } from "./routes/landing.js";
import { resultsRouter } from "./routes/results.js";
import { authRoutes } from "./auth/routes.js";
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

  // Defaults to the real DB-backed session middleware (mirrors the storage/mailer
  // deps pattern); tests inject a lightweight cookie-decoding seam. Without this
  // default the production server never authenticates any request.
  app.use(deps.sessionMiddleware ?? realSessionMiddleware(pool, cfg));

  // Landing page: GET / (authed vs anon variants), mounted at app level, no requireUser().
  app.use(landingRouter(cfg));

  // Mounted at the app level (no prefix, no requireUser()) so its internal /auth/*
  // paths are seen absolute -- these ARE the auth entry points (magic-link request,
  // verify, logout), so gating them behind requireUser() would be self-defeating.
  const mailer = deps.mailer ?? smtpMailer(cfg);
  app.use(authRoutes(pool, cfg, mailer));

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

  // Mounted at the app level (no prefix) so requireUser()'s req.path sees the full
  // path — /api/search-refs must hit the 401-JSON branch, /batches/:id/validate the
  // 302-redirect branch (both routers apply their own requireUser() per route).
  app.use(validateRouter(pool, cfg));
  app.use(resultsRouter(pool));
  app.use(searchRouter(pool));

  app.use(notFound());
  app.use(errorHandler());

  return app;
}
