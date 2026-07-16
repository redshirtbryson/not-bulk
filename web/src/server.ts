import { loadConfig } from "./config.js";
import { getPool } from "./db.js";
import { createApp } from "./app.js";

const cfg = loadConfig();
const pool = getPool();
const app = createApp({ cfg, pool });

app.listen(cfg.web.port, () => {
  console.log(`[web] listening on http://127.0.0.1:${cfg.web.port}`);
});
