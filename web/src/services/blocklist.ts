import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function loadDomains(): Set<string> {
  const here = dirname(fileURLToPath(import.meta.url)); // .../web/src/services
  const webRoot = dirname(dirname(here));               // .../web
  const path = join(webRoot, "data", "disposable-domains.txt");
  const set = new Set<string>();
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const d = line.trim().toLowerCase();
    if (d && !d.startsWith("#")) set.add(d);
  }
  return set;
}

const DOMAINS = loadDomains();

/**
 * True when the email's domain — or any parent domain — is in the blocklist.
 * e.g. "x@a.mailinator.com" → checks "a.mailinator.com", then "mailinator.com" (match).
 */
export function isDisposable(email: string): boolean {
  const at = email.lastIndexOf("@");
  if (at === -1) return false;
  const domain = email.slice(at + 1).trim().toLowerCase();
  if (!domain) return false;
  const labels = domain.split(".");
  for (let i = 0; i < labels.length - 1; i++) {
    if (DOMAINS.has(labels.slice(i).join("."))) return true;
  }
  return false;
}
