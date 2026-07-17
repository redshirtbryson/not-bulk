import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { load } from "js-yaml";

export interface Config {
  web: { port: number; base_url: string; secure_cookies: boolean };
  storage: {
    endpoint: string;
    bucket: string;
    access_key: string;
    secret_key: string;
    signed_url_ttl_seconds: number;
  };
  mail: { smtp_host: string; smtp_port: number; from: string };
  auth: {
    session_absolute_days: number;
    session_idle_days: number;
    magic_link_expiry_minutes: number;
    magic_links_per_email_hour: number;
    magic_links_per_email_day: number;
  };
  quotas: {
    batches_per_day: number;
    photos_per_day: number;
    cards_per_day: number;
    fetches_per_day: number;
    photos_per_batch: number;
    anon_photos_per_batch: number;
    max_photo_bytes: number;
    max_pixels: number;
    max_cards_per_photo: number;
  };
  fetcher: { allowed_hosts: string[]; max_bytes: number; timeout_seconds: number };
  hash: { user_validated_cap_per_card: number };
  turnstile: { site_key: string; secret: string };
  refproxy: { allowed_image_host: string; cache_prefix: string; max_bytes: number };
}

function findConfigPath(): string {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (;;) {
    const candidate = join(dir, "config.yaml");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) {
      throw new Error("config.yaml not found walking up from " + dirname(fileURLToPath(import.meta.url)));
    }
    dir = parent;
  }
}

let cached: Config | null = null;

export function loadConfig(): Config {
  if (cached) return cached;
  const raw = readFileSync(findConfigPath(), "utf8");
  cached = load(raw) as Config;
  return cached;
}
