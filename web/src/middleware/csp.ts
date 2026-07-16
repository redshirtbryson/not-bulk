import type { RequestHandler } from "express";

const CSP =
  "default-src 'self'; " +
  "img-src 'self' http://127.0.0.1:9000; " +
  "style-src 'self'; " +
  "script-src 'self' https://challenges.cloudflare.com; " +
  "frame-ancestors 'none'";

export function csp(): RequestHandler {
  return (_req, res, next) => {
    res.setHeader("Content-Security-Policy", CSP);
    res.setHeader("X-Content-Type-Options", "nosniff");
    next();
  };
}
