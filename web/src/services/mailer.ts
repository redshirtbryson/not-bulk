// Placeholder mailer interface. A later task (magic-link auth) provides the
// real SMTP-backed implementation; app.ts only needs the type for its
// optional DI seam today.
export interface Mailer {
  send(to: string, subject: string, body: string): Promise<void>;
}
