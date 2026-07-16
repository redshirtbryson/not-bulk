// Placeholder storage interface. A later task provides the real S3/MinIO
// backed implementation; app.ts only needs the type for its optional DI
// seam today.
export interface Storage {
  putObject(key: string, body: Buffer, contentType: string): Promise<void>;
  getSignedUrl(key: string, ttlSeconds: number): Promise<string>;
}
