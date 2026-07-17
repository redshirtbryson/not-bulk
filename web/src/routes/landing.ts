import { Router } from 'express';
import type { Config } from '../config.js';
import type { AuthedRequest } from '../middleware/session.js';

export function landingRouter(cfg: Config): Router {
  const r = Router();
  r.get('/', (req: AuthedRequest, res) => {
    res.render('landing.njk', {
      authed: Boolean(req.user),
      turnstileSiteKey: cfg.turnstile.site_key,
      devBypassTurnstile: process.env.DEV_BYPASS_TURNSTILE === '1',
    });
  });
  return r;
}
