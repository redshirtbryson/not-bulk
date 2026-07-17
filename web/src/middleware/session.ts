import type { Request, RequestHandler } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import { lookupSession, touchSession } from "../auth/sessions.js";

export interface AuthedRequest extends Request {
  user?: { id: string; email: string | null; tier: string };
}

function readCookie(header: string | undefined, name: string): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() === name) {
      return decodeURIComponent(part.slice(eq + 1).trim());
    }
  }
  return null;
}

export function sessionMiddleware(pool: Pool, cfg: Config): RequestHandler {
  return async (req, _res, next) => {
    try {
      const token = readCookie(req.headers.cookie, "nb_session");
      if (token) {
        const s = await lookupSession(pool, cfg, token);
        if (s) {
          (req as AuthedRequest).user = { id: s.user_id, email: s.email, tier: s.tier };
          await touchSession(pool, s.session_id); // no-op unless >1h since last touch
        }
      }
      next();
    } catch (err) {
      next(err);
    }
  };
}

/**
 * Auth-failure convention (plan §Global Constraints #3): unauthenticated page routes
 * redirect 302 → "/"; unauthenticated /api/* routes return 401 JSON instead, since API
 * callers (htmx/fetch) can't follow a redirect the way a browser navigation can.
 */
export function requireUser(): RequestHandler {
  return (req, res, next) => {
    if ((req as AuthedRequest).user) return next();
    if (req.path.startsWith("/api/")) {
      return res.status(401).json({ error: "unauthorized" });
    }
    res.redirect(302, "/");
  };
}
