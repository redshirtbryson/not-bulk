import nodemailer, { type Transporter } from "nodemailer";
import type { Config } from "../config.js";

export interface Mailer {
  sendMagicLink(email: string, url: string): Promise<void>;
}

export function smtpMailer(cfg: Config): Mailer {
  const transport: Transporter = nodemailer.createTransport({
    host: cfg.mail.smtp_host,
    port: cfg.mail.smtp_port,
    secure: false,
  });
  return {
    async sendMagicLink(email, url) {
      await transport.sendMail({
        from: cfg.mail.from,
        to: email,
        subject: "Your NotBulk sign-in link",
        text:
          "Click to sign in to NotBulk:\n\n" +
          url +
          "\n\nThis link is single-use and expires in 15 minutes. " +
          "If you didn't request it, ignore this email.\n",
      });
    },
  };
}
