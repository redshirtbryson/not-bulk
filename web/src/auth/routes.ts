import { Router } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { Mailer } from "../services/mailer.js";
import { verifyTurnstile } from "../services/turnstile.js";
import { requestMagicLink, verifyMagicLink } from "./magic.js";
import { destroySession } from "./sessions.js";

const ALWAYS_OK = { ok: true, message: "If that email is valid, a sign-in link is on its way." };

export function authRoutes(pool: Pool, cfg: Config, mailer: Mailer): Router {
  const r = Router();

  r.post("/auth/magic-link", async (req, res, next) => {
    try {
      const email = String(req.body?.email ?? "");
      const token = String(req.body?.["cf-turnstile-response"] ?? "");
      const ok = await verifyTurnstile(cfg, token, req.ip);
      if (ok) {
        await requestMagicLink(pool, cfg, mailer, email); // always resolves void
      }
      // Constant response whether or not Turnstile/email passed.
      res.status(200).json(ALWAYS_OK);
    } catch (err) {
      next(err);
    }
  });

  r.get("/auth/verify", async (req, res, next) => {
    try {
      const token = String(req.query.token ?? "");
      const cookieToken = await verifyMagicLink(pool, cfg, token);
      if (cookieToken) {
        res.cookie("nb_session", cookieToken, {
          httpOnly: true,
          sameSite: "lax",
          secure: cfg.web.secure_cookies,
          path: "/",
          maxAge: cfg.auth.session_absolute_days * 86400_000,
        });
      }
      res.redirect(302, "/");
    } catch (err) {
      next(err);
    }
  });

  r.post("/auth/logout", async (req, res, next) => {
    try {
      const raw = req.headers.cookie ?? "";
      const m = raw.split(";").map((p) => p.trim()).find((p) => p.startsWith("nb_session="));
      if (m) await destroySession(pool, decodeURIComponent(m.slice("nb_session=".length)));
      res.clearCookie("nb_session", { path: "/" });
      res.redirect(302, "/");
    } catch (err) {
      next(err);
    }
  });

  return r;
}
