import type { RequestHandler, ErrorRequestHandler } from "express";

export function notFound(): RequestHandler {
  return (_req, res) => {
    res.status(404).type("text/plain").send("Not Found");
  };
}

export function errorHandler(): ErrorRequestHandler {
  return (err, _req, res, _next) => {
    console.error("[web] unhandled error:", err);
    if (res.headersSent) return;
    res.status(500).type("text/plain").send("Internal Server Error");
  };
}
