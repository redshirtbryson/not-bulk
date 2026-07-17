import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  DeleteObjectCommand,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import type { Config } from "../config.js";

export class Storage {
  private client: S3Client;
  private bucket: string;
  private ttl: number;

  constructor(cfg: Config) {
    this.bucket = cfg.storage.bucket;
    this.ttl = cfg.storage.signed_url_ttl_seconds;
    this.client = new S3Client({
      endpoint: cfg.storage.endpoint,
      forcePathStyle: true, // REQUIRED for MinIO (no virtual-hosted buckets)
      region: "auto",
      credentials: {
        accessKeyId: cfg.storage.access_key,
        secretAccessKey: cfg.storage.secret_key,
      },
    });
  }

  photoKey(userId: string, batchId: string, photoId: string): string {
    return `${userId}/${batchId}/${photoId}.webp`;
  }

  cropKey(userId: string, batchId: string, cardId: string): string {
    return `${userId}/${batchId}/crops/${cardId}.webp`;
  }

  async put(key: string, body: Buffer, contentType: string): Promise<void> {
    await this.client.send(
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: key,
        Body: body,
        ContentType: contentType,
      }),
    );
  }

  async get(key: string): Promise<Buffer> {
    const out = await this.client.send(
      new GetObjectCommand({ Bucket: this.bucket, Key: key }),
    );
    const body = out.Body as unknown as AsyncIterable<Uint8Array>;
    const chunks: Buffer[] = [];
    for await (const chunk of body) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }

  async signedGetUrl(key: string): Promise<string> {
    return getSignedUrl(
      this.client,
      new GetObjectCommand({ Bucket: this.bucket, Key: key }),
      { expiresIn: this.ttl },
    );
  }

  async delete(key: string): Promise<void> {
    await this.client.send(
      new DeleteObjectCommand({ Bucket: this.bucket, Key: key }),
    );
  }
}
